"""The engine: a state machine that enforces the human-in-the-loop (HITL) gates.

Gates (enforced here, so no UI can bypass them):
  G1  Stage N+1 cannot start until the engineer has accepted or modified stage N.
  G2  A proposal that lacks a required section cannot be accepted as it is; the engineer must modify it.
  G3  Every open question (Q<n>) needs an engineer answer, or the explicit word ASSUME, before the stage can close.
  G4  A rejection needs a reason; after `max_rejections` the tool stops regenerating and the engineer must write the text.
  G5  Nothing leaves the machine to an external provider without an outbound preview the engineer confirms.
  G6  Non-interactive decision modes (scripted, auto) are refused for external providers.

Secrets are redacted before anything is sent, stored, or logged (see redaction.py). The audit log records every event.
"""

import hashlib
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from copilot.analyzers import analyse_brief
from copilot.audit import AuditLog
from copilot.context import ContextBundle
from copilot.models import Decision, Proposal, Session
from copilot.prompts import build_request_parts
from copilot.providers import CompletionRequest, Provider, ProviderError
from copilot.redaction import RedactionReport, Redactor
from copilot.stages import LAST_STAGE, STAGE_BY_NUMBER, STAGES
from copilot.store import SessionStore

MAX_BRIEF_CHARS = 20_000
QUESTION_RE = re.compile(r"^\s*[-*]\s*(Q\d+)\s*[:.)]\s*(.+?)\s*$", re.M)
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)


class GateError(Exception):
    """The requested action would break a human-in-the-loop rule."""


class ProviderFailure(Exception):
    """The provider failed; the session is unchanged and the stage can be retried."""


@dataclass(frozen=True)
class OutboundPreview:
    stage: int
    provider: str
    system: str
    user: str
    redaction: dict[str, int]


class OutboundConfirmationRequired(Exception):
    def __init__(self, preview: OutboundPreview) -> None:
        super().__init__("the engineer must confirm what will be sent to the external provider")
        self.preview = preview


def missing_sections(stage: int, text: str) -> list[str]:
    present = {h.strip().lower() for h in HEADING_RE.findall(text)}
    return [name for name in STAGE_BY_NUMBER[stage].required_sections if name.lower() not in present]


def parse_questions(text: str) -> dict[str, str]:
    """Questions listed under the 'Open questions' heading as '- Q<n>: ...'."""
    match = re.search(r"^##\s+Open questions\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    if not match:
        return {}
    return dict(QUESTION_RE.findall(match.group(1)))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Engine:
    def __init__(
        self,
        provider: Provider,
        store: SessionStore,
        *,
        clock: Callable[[], str],
        max_rejections: int = 3,
        mode: str = "interactive",
    ) -> None:
        if mode != "interactive" and provider.is_external:
            raise GateError(
                "scripted and automatic decisions are not allowed with an external provider (gate G6)"
            )
        self.provider = provider
        self.store = store
        self.clock = clock
        self.max_rejections = max_rejections
        self.mode = mode
        self._redactors: dict[str, Redactor] = {}

    # ----- plumbing -------------------------------------------------------------------------------------------

    def _audit(self, session_id: str) -> AuditLog:
        return AuditLog(self.store.session_dir(session_id) / "audit.jsonl", self.clock)

    def _redactor(self, session_id: str) -> Redactor:
        # After a restart the mapping is gone (it is never persisted), so placeholder numbers may restart.
        return self._redactors.setdefault(session_id, Redactor())

    def _clean(self, session: Session, text: str) -> tuple[str, RedactionReport]:
        return self._redactor(session.id).redact(text)

    def load_context(self, session: Session) -> list[tuple[str, str]]:
        try:
            data = json.loads(self.store.read_artifact(session.id, "context.json"))
        except Exception:  # no context for this session
            return []
        return [(f["path"], f["text"]) for f in data["files"]]

    # ----- lifecycle ------------------------------------------------------------------------------------------

    def start(
        self,
        brief: str,
        *,
        context: ContextBundle | None = None,
        session_id: str | None = None,
        deterministic: bool = False,
    ) -> Session:
        if not brief.strip():
            raise ValueError("the brief is empty")
        if len(brief) > MAX_BRIEF_CHARS:
            raise ValueError(f"the brief is longer than {MAX_BRIEF_CHARS} characters")
        sid = session_id or f"{self.clock().replace(':', '').replace('-', '')[:15]}-{secrets.token_hex(3)}"
        if self.store.exists(sid):
            raise GateError(f"session {sid!r} already exists")
        redactor = self._redactors.setdefault(sid, Redactor())
        clean_brief, report = redactor.redact(brief)
        context_json: list[dict[str, str | bool]] = []
        context_meta: list[dict[str, object]] = []
        if context:
            for file in context.files:
                text, file_report = redactor.redact(file.text)
                report.merge(file_report)
                context_json.append({"path": file.path, "text": text, "truncated": file.truncated})
                context_meta.append({"path": file.path, "bytes": len(text), "truncated": file.truncated})
        analysis = analyse_brief(brief)
        session = Session(
            id=sid,
            created_at=self.clock(),
            provider=self.provider.name,
            external_provider=self.provider.is_external,
            brief=clean_brief,
            context_files=context_meta,
            context_skipped=[{"path": p, "reason": r} for p, r in (context.skipped if context else [])],
            redaction_summary=dict(report.counts),
            injection_flags=list(analysis.injection),
            deterministic=deterministic,
        )
        self.store.save(session)
        self.store.write_artifact(sid, "00_brief.md", "# Brief (redacted)\n\n" + clean_brief + "\n")
        if context_json:
            self.store.write_artifact(sid, "context.json", json.dumps({"files": context_json}, indent=2))
        self._audit(sid).append(
            "session_started",
            {
                "provider": session.provider,
                "external": session.external_provider,
                "mode": self.mode,
                "brief_sha256": _sha(clean_brief),
                "context_files": len(context_meta),
                "context_skipped": len(session.context_skipped),
                "redacted": dict(report.counts),
                "injection_flags": session.injection_flags,
            },
        )
        return session

    def preview(self, session: Session) -> OutboundPreview:
        system, user, _, report = self._prepare(session)
        merged = RedactionReport(dict(session.redaction_summary))
        merged.merge(report)
        return OutboundPreview(session.current_stage, self.provider.name, system, user, merged.counts)

    def _prepare(self, session: Session) -> tuple[str, str, dict[str, object], RedactionReport]:
        stage = session.current_stage
        previous = {n: r.final_text for n, r in session.records.items() if n < stage and r.final_text}
        system, user, inputs = build_request_parts(
            stage,
            brief=session.brief,
            context_files=self.load_context(session),
            previous=previous,
            feedback=session.record(stage).feedback,
        )
        clean_user, report = self._clean(session, user)
        return system, clean_user, inputs, report

    # ----- propose --------------------------------------------------------------------------------------------

    def propose(self, session: Session, *, confirm_outbound: bool = False) -> Proposal:
        stage = session.current_stage
        audit = self._audit(session.id)
        if session.status != "awaiting_proposal":
            raise self._blocked(audit, stage, f"cannot propose while the session is {session.status}")
        record = session.record(stage)
        if record.rejections >= self.max_rejections:
            raise self._blocked(
                audit,
                stage,
                "too many rejections: the engineer must write this stage (action 'modify' with text)",
            )
        system, user, inputs, report = self._prepare(session)
        if self.provider.is_external:
            shown = RedactionReport(
                dict(session.redaction_summary)
            )  # cumulative: includes what was removed from the brief/context
            shown.merge(report)
            preview = OutboundPreview(stage, self.provider.name, system, user, shown.counts)
            if not confirm_outbound:
                audit.append(
                    "outbound_preview",
                    {"stage": stage, "prompt_sha256": _sha(system + user), "redacted": shown.counts},
                )
                raise OutboundConfirmationRequired(preview)
            audit.append("outbound_confirmed", {"stage": stage, "prompt_sha256": _sha(system + user)})
        try:
            response = self.provider.complete(CompletionRequest(stage, system, user, inputs))
        except ProviderError as exc:
            audit.append("provider_error", {"stage": stage, "error": str(exc)})
            raise ProviderFailure(str(exc)) from None
        text, out_report = self._clean(session, response.text)
        revision = len(record.proposals) + 1
        proposal = Proposal(
            stage=stage,
            revision=revision,
            text=text,
            provider=self.provider.name,
            model=response.model,
            prompt_sha256=_sha(system + user),
            response_sha256=_sha(text),
            redaction_in=report.counts,
            redaction_out=out_report.counts,
            missing_sections=missing_sections(stage, text),
            open_questions=parse_questions(text),
            created_at=self.clock(),
        )
        record.proposals.append(proposal)
        session.status = "awaiting_decision"
        name = f"stage{stage}_rev{revision}"
        self.store.write_artifact(
            session.id, f"{name}_prompt.md", f"# System\n\n{system}\n\n# User\n\n{user}\n", subdir="prompts"
        )
        self.store.write_artifact(session.id, f"{name}_response.md", text + "\n", subdir="prompts")
        self.store.save(session)
        audit.append(
            "proposal",
            {
                "stage": stage,
                "revision": revision,
                "provider": proposal.provider,
                "model": proposal.model,
                "prompt_sha256": proposal.prompt_sha256,
                "response_sha256": proposal.response_sha256,
                "redacted_in": report.counts,
                "redacted_out": out_report.counts,
                "missing_sections": proposal.missing_sections,
                "open_questions": sorted(proposal.open_questions),
            },
        )
        return proposal

    # ----- decide ---------------------------------------------------------------------------------------------

    def decide(self, session: Session, decision: Decision) -> Session:
        stage = session.current_stage
        audit = self._audit(session.id)
        record = session.record(stage)
        if decision.stage != stage:
            raise self._blocked(
                audit,
                stage,
                f"decision is for stage {decision.stage} but the current stage is {stage} (gate G1)",
            )
        manual = (
            session.status == "awaiting_proposal"
            and record.rejections >= self.max_rejections
            and decision.action == "modify"
            and bool(decision.text)
        )
        if session.status != "awaiting_decision" and not manual:
            raise self._blocked(
                audit, stage, f"no proposal is waiting for a decision (status {session.status})"
            )

        clean = self._clean_decision(session, decision)
        if clean.action == "reject":
            if not clean.notes.strip():
                raise self._blocked(audit, stage, "a rejection needs a reason (gate G4)")
            record.rejections += 1
            record.feedback.append(clean.notes)
            session.status = "awaiting_proposal"
            self.store.save(session)
            audit.append(
                "decision",
                {
                    "stage": stage,
                    "action": "reject",
                    "mode": clean.mode,
                    "notes_sha256": _sha(clean.notes),
                    "rejections": record.rejections,
                },
            )
            return session

        proposal = record.proposals[-1] if record.proposals else None
        if clean.action == "accept":
            if proposal is None:
                raise self._blocked(audit, stage, "nothing to accept")
            final = proposal.text
        elif clean.text:
            final = clean.text
        elif clean.append and proposal is not None:
            final = proposal.text + "\n\n" + clean.append
        else:
            raise self._blocked(audit, stage, "modify needs replacement text or text to append")

        missing = missing_sections(stage, final)
        if missing:
            raise self._blocked(
                audit,
                stage,
                "missing required sections: "
                + ", ".join(missing)
                + " (gate G2); modify the text to add them",
            )
        questions = parse_questions(final)
        unanswered = [q for q in questions if not clean.answers.get(q, "").strip()]
        if unanswered:
            raise self._blocked(
                audit,
                stage,
                "unanswered open questions: "
                + ", ".join(unanswered)
                + " (gate G3); answer each, or write ASSUME to accept the proposed assumption",
            )
        if questions:
            final += "\n\n## Engineer answers\n" + "\n".join(
                f"- {q}: {clean.answers[q].strip()}" for q in questions
            )

        clean.decided_at = self.clock()
        record.decision, record.final_text = clean, final
        spec = STAGE_BY_NUMBER[stage]
        self.store.write_artifact(
            session.id, f"{stage:02d}_{spec.key}.md", f"# Stage {stage}: {spec.title}\n\n{final}\n"
        )
        if stage == LAST_STAGE:
            session.status = "complete"
        else:
            session.current_stage, session.status = stage + 1, "awaiting_proposal"
        self.store.save(session)
        audit.append(
            "decision",
            {
                "stage": stage,
                "action": clean.action,
                "mode": clean.mode,
                "final_sha256": _sha(final),
                "answers": sorted(clean.answers),
                "assumed": sorted(k for k, v in clean.answers.items() if v.strip().upper() == "ASSUME"),
            },
        )
        if session.status == "complete":
            self.store.write_artifact(session.id, "SUMMARY.md", self._summary(session, audit.last_hash))
            audit.append("session_completed", {"stages": LAST_STAGE})
        return session

    def abort(self, session: Session, reason: str) -> None:
        session.status = "aborted"
        self.store.save(session)
        self._audit(session.id).append(
            "session_aborted", {"stage": session.current_stage, "reason_sha256": _sha(reason)}
        )

    # ----- helpers --------------------------------------------------------------------------------------------

    def _blocked(self, audit: AuditLog, stage: int, reason: str) -> GateError:
        audit.append("gate_blocked", {"stage": stage, "reason": reason})
        return GateError(reason)

    def _clean_decision(self, session: Session, d: Decision) -> Decision:
        def clean(text: str | None) -> str | None:
            return None if text is None else self._clean(session, text)[0]

        return Decision(
            stage=d.stage,
            action=d.action,
            notes=clean(d.notes) or "",
            answers={k: clean(v) or "" for k, v in d.answers.items()},
            text=clean(d.text),
            append=clean(d.append),
            mode=self.mode,
            decided_at=d.decided_at,
        )

    def _summary(self, session: Session, audit_hash: str) -> str:
        rows = []
        for spec in STAGES:
            record = session.records.get(spec.number)
            decision = record.decision if record else None
            rows.append(
                f"| {spec.number} | {spec.title} | {decision.action if decision else '-'} | {len(record.proposals) if record else 0} | "
                f"{record.rejections if record else 0} | {decision.mode if decision else '-'} |"
            )
        return "\n".join(
            [
                f"# Session {session.id}",
                "",
                f"- Provider: `{session.provider}` ({'external' if session.external_provider else 'local'})",
                f"- Redactions before sending/storing (counts only): {json.dumps(session.redaction_summary, sort_keys=True)}",
                f"- Injection-like phrases flagged in the brief: {', '.join(session.injection_flags) or 'none'}",
                f"- Audit chain head (`audit.jsonl`, verify with `python -m copilot verify`): `{audit_hash}`",
                "",
                "| Stage | Title | Engineer decision | Proposals | Rejections | Decision mode |",
                "|---|---|---|---|---|---|",
                *rows,
                "",
                "Decision mode `interactive` means a person answered at the gate; `scripted` means decisions were read from a file.",
                "",
            ]
        )
