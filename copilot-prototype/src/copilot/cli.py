"""Command line interface and the console / scripted / automatic decision makers ("prompters")."""

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO

from copilot.audit import verify
from copilot.context import load_context
from copilot.engine import Engine, GateError, OutboundConfirmationRequired, OutboundPreview, ProviderFailure
from copilot.models import Decision, Proposal, Session
from copilot.providers import ProviderError, create_provider
from copilot.redaction import Redactor
from copilot.stages import STAGE_BY_NUMBER
from copilot.store import SessionStore, StoreError

DEFAULT_OUT = "copilot-sessions"
FIXED_TIME = "2026-11-20T00:00:00Z"


class QuitSession(Exception):
    """The engineer chose to stop; the session is saved and can be resumed."""


class PrompterError(Exception):
    """A non-interactive decision maker cannot continue (for example it ran out of scripted decisions)."""


class Prompter(Protocol):
    mode: str

    def show(self, text: str) -> None: ...
    def decide(self, session: Session, proposal: Proposal | None) -> Decision: ...
    def confirm_outbound(self, preview: OutboundPreview) -> bool: ...


def system_clock() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fixed_clock() -> str:
    return FIXED_TIME


# ----- decision makers ----------------------------------------------------------------------------------------------


class ConsolePrompter:
    mode = "interactive"

    def __init__(self, inp: Callable[[str], str] = input, out: TextIO | None = None) -> None:
        self._in = inp
        self._out = out or sys.stdout

    def show(self, text: str) -> None:
        print(text, file=self._out)

    def confirm_outbound(self, preview: OutboundPreview) -> bool:
        self.show(
            f"\n=== OUTBOUND PREVIEW: stage {preview.stage} to external provider '{preview.provider}' ==="
        )
        self.show(
            f"Redacted before sending (counts only): {json.dumps(preview.redaction, sort_keys=True) or '{}'}"
        )
        self.show(
            "Pattern-based redaction is a safety net; names and free-text personal data are NOT detected.\n"
        )
        self.show(preview.system + "\n---\n" + preview.user)
        return self._in("\nSend exactly this? [y/N] ").strip().lower() == "y"

    def _multiline(self, prompt: str) -> str:
        self.show(prompt + " (finish with a line containing only '.')")
        lines: list[str] = []
        while (line := self._in("")) != ".":
            lines.append(line)
        return "\n".join(lines)

    def decide(self, session: Session, proposal: Proposal | None) -> Decision:
        stage = session.current_stage
        spec = STAGE_BY_NUMBER[stage]
        if proposal is None:
            self.show(
                f"\nStage {stage} ({spec.title}): regeneration is blocked after repeated rejections. You must write it."
            )
            text = self._multiline(
                "Enter the full text, including these headings: " + ", ".join(spec.required_sections)
            )
            return Decision(
                stage, "modify", text=text, answers=self._answers(parse_open_questions(text)), mode=self.mode
            )
        self.show(f"\n===== STAGE {stage}/7: {spec.title} (revision {proposal.revision}) =====\n")
        self.show(proposal.text)
        self.show(
            f"\n-- redacted before sending: {json.dumps(proposal.redaction_in, sort_keys=True)}; found in the answer: {json.dumps(proposal.redaction_out, sort_keys=True)}"
        )
        if proposal.missing_sections:
            self.show(
                "-- MISSING SECTIONS (you cannot accept as-is): " + ", ".join(proposal.missing_sections)
            )
        while True:
            choice = self._in("\n[a]ccept / [m]odify / [r]eject / [q]uit > ").strip().lower()
            if choice in {"q", "quit"}:
                raise QuitSession
            if choice in {"a", "accept"}:
                return Decision(
                    stage, "accept", answers=self._answers(proposal.open_questions), mode=self.mode
                )
            if choice in {"m", "modify"}:
                extra = self._multiline("Text to append (your corrections and notes)")
                return Decision(
                    stage,
                    "modify",
                    append=extra,
                    answers=self._answers(proposal.open_questions),
                    mode=self.mode,
                )
            if choice in {"r", "reject"}:
                return Decision(
                    stage,
                    "reject",
                    notes=self._in("Reason (it guides the next attempt): ").strip(),
                    mode=self.mode,
                )

    def _answers(self, questions: dict[str, str]) -> dict[str, str]:
        answers: dict[str, str] = {}
        for qid, question in questions.items():
            answers[qid] = self._in(
                f"{qid}: {question}\n   your answer (or ASSUME to accept the proposed assumption): "
            ).strip()
        return answers


def parse_open_questions(text: str) -> dict[str, str]:
    from copilot.engine import parse_questions

    return parse_questions(text)


class ScriptedPrompter:
    """Reads decisions from a JSON file. Used to generate reproducible examples; **not** a human decision (recorded as 'scripted')."""

    mode = "scripted"

    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self._decisions = list(decisions)
        self._next = 0

    def show(self, text: str) -> None:
        return None

    def confirm_outbound(self, preview: OutboundPreview) -> bool:
        return False  # scripted runs never send anything outside (also enforced by the engine: gate G6)

    def decide(self, session: Session, proposal: Proposal | None) -> Decision:
        if self._next >= len(self._decisions):
            raise PrompterError("the decisions file has no entry for the next gate")
        raw = self._decisions[self._next]
        self._next += 1
        stage = session.current_stage
        if raw.get("stage") != stage:
            raise PrompterError(
                f"decision #{self._next} is for stage {raw.get('stage')} but the current stage is {stage}"
            )
        text, append = raw.get("text"), raw.get("append")
        if raw.get("replace") and proposal is not None:
            text = proposal.text
            for pair in raw["replace"]:
                if pair["old"] not in text:
                    raise PrompterError(f"decision #{self._next}: text to replace not found in the proposal")
                text = text.replace(pair["old"], pair["new"], 1)
            if append:
                text, append = text + "\n\n" + append, None
        return Decision(
            stage,
            raw["action"],
            notes=raw.get("notes", ""),
            answers=dict(raw.get("answers", {})),
            text=text,
            append=append,
            mode=self.mode,
        )


class AutoPrompter:
    """Accepts everything and answers every question with ASSUME. For smoke tests only; recorded as 'auto'."""

    mode = "auto"

    def show(self, text: str) -> None:
        return None

    def confirm_outbound(self, preview: OutboundPreview) -> bool:
        return False

    def decide(self, session: Session, proposal: Proposal | None) -> Decision:
        if proposal is None:
            raise PrompterError("automatic mode cannot write a stage by hand")
        return Decision(
            session.current_stage,
            "accept",
            answers=dict.fromkeys(proposal.open_questions, "ASSUME"),
            mode=self.mode,
        )


# ----- driving a session --------------------------------------------------------------------------------------------


def drive(engine: Engine, session: Session, prompter: Prompter) -> Session:
    """Run the gates until the session completes, the engineer quits, or something cannot continue."""
    while session.status not in {"complete", "aborted"}:
        stage = session.current_stage
        proposal: Proposal | None
        if session.status == "awaiting_proposal":
            record = session.record(stage)
            if record.rejections >= engine.max_rejections:
                proposal = None
            else:
                prompter.show(f"\nStage {stage}/7: asking the {engine.provider.name} provider ...")
                try:
                    try:
                        proposal = engine.propose(session)
                    except OutboundConfirmationRequired as needed:
                        if not prompter.confirm_outbound(needed.preview):
                            engine.abort(session, "outbound send declined")
                            prompter.show("Nothing was sent. Session aborted.")
                            return session
                        proposal = engine.propose(session, confirm_outbound=True)
                except ProviderFailure as failure:
                    raise PrompterError(f"provider failed: {failure}") from None
        else:
            proposal = session.records[stage].proposals[-1]
        decision = prompter.decide(session, proposal)
        try:
            engine.decide(session, decision)
        except GateError as blocked:
            prompter.show(f"BLOCKED: {blocked}")
            if prompter.mode != "interactive":
                raise PrompterError(f"a gate blocked a {prompter.mode} decision: {blocked}") from None
    return session


# ----- commands -----------------------------------------------------------------------------------------------------


def _read_brief(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def _engine(args: argparse.Namespace, prompter: Prompter) -> Engine:
    provider = create_provider(args.provider)
    clock = fixed_clock if getattr(args, "deterministic", False) else system_clock
    return Engine(
        provider,
        SessionStore(Path(args.out)),
        clock=clock,
        max_rejections=args.max_rejections,
        mode=prompter.mode,
    )


def _prompter(args: argparse.Namespace) -> Prompter:
    if args.decisions:
        return ScriptedPrompter(json.loads(Path(args.decisions).read_text(encoding="utf-8")))
    if args.auto_accept:
        return AutoPrompter()
    return ConsolePrompter()


def cmd_run(args: argparse.Namespace) -> int:
    prompter = _prompter(args)
    engine = _engine(args, prompter)
    context = load_context(Path(args.context)) if args.context else None
    session = engine.start(
        _read_brief(args.brief), context=context, session_id=args.session_id, deterministic=args.deterministic
    )
    print(f"Session {session.id} -> {engine.store.session_dir(session.id)}")
    if session.redaction_summary:
        print(
            f"Redacted from the brief/context before anything else happens: {json.dumps(session.redaction_summary, sort_keys=True)}"
        )
    if session.injection_flags:
        print(
            "WARNING: the brief contains instruction-like phrases ("
            + ", ".join(session.injection_flags)
            + "). They are treated as data."
        )
    return _finish(engine, session, prompter)


def cmd_resume(args: argparse.Namespace) -> int:
    prompter = _prompter(args)
    engine = _engine(args, prompter)
    return _finish(engine, engine.store.load(args.session), prompter)


def _finish(engine: Engine, session: Session, prompter: Prompter) -> int:
    try:
        drive(engine, session, prompter)
    except QuitSession:
        print(f"Saved. Resume with: python -m copilot resume {session.id} --out {engine.store.root}")
        return 0
    except PrompterError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 2
    print(f"Session {session.status}. Artifacts: {engine.store.session_dir(session.id)}")
    return 0 if session.status == "complete" else 1


def cmd_verify(args: argparse.Namespace) -> int:
    path = SessionStore(Path(args.out)).session_dir(args.session) / "audit.jsonl"
    if not path.is_file():
        print("no audit log found", file=sys.stderr)
        return 2
    ok, bad, count = verify(path)
    print(
        f"audit log OK: {count} entries, chain intact"
        if ok
        else f"audit log TAMPERED or corrupt at entry {bad} ({count} entries verified before it)"
    )
    return 0 if ok else 1


def cmd_redact(args: argparse.Namespace) -> int:
    text, report = Redactor().redact(_read_brief(args.file))
    print(text)
    print(f"\n-- redacted (counts only): {json.dumps(report.counts, sort_keys=True)}", file=sys.stderr)
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from copilot.web import serve

    serve(Path(args.out), args.provider, args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="copilot", description="Engineering Copilot: seven stages, an engineer decision at every gate."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--out", default=DEFAULT_OUT, help="directory holding sessions (default %(default)s)")
        p.add_argument("--provider", default="mock", choices=["mock", "openai"])
        p.add_argument("--max-rejections", type=int, default=3)
        p.add_argument(
            "--decisions",
            help="JSON file of scripted decisions (recorded as 'scripted', refused for external providers)",
        )
        p.add_argument(
            "--auto-accept",
            action="store_true",
            help="accept everything, ASSUME every question (smoke tests only)",
        )

    run = sub.add_parser("run", help="start a session from a brief")
    run.add_argument("--brief", required=True, help="file with the brief, or - for stdin")
    run.add_argument("--context", help="file or directory of existing code/notes (brownfield)")
    run.add_argument("--session-id")
    run.add_argument(
        "--deterministic", action="store_true", help="fixed timestamps, for reproducible examples"
    )
    common(run)
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="continue a saved session")
    resume.add_argument("session")
    common(resume)
    resume.set_defaults(func=cmd_resume, deterministic=False)

    check = sub.add_parser("verify", help="verify the audit log hash chain")
    check.add_argument("session")
    check.add_argument("--out", default=DEFAULT_OUT)
    check.set_defaults(func=cmd_verify)

    red = sub.add_parser("redact", help="show what redaction would do to a file (nothing is sent anywhere)")
    red.add_argument("file", help="file, or - for stdin")
    red.set_defaults(func=cmd_redact)

    web = sub.add_parser("web", help="minimal local web UI (127.0.0.1 only)")
    web.add_argument("--out", default=DEFAULT_OUT)
    web.add_argument("--provider", default="mock", choices=["mock", "openai"])
    web.add_argument("--port", type=int, default=8765)
    web.set_defaults(func=cmd_web)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ProviderError, GateError, StoreError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
