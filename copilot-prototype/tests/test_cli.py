import json
from pathlib import Path

import pytest

from conftest import FAKE_AWS
from copilot.audit import verify
from copilot.cli import (
    AutoPrompter,
    ConsolePrompter,
    PrompterError,
    ScriptedPrompter,
    drive,
    main,
)
from copilot.engine import Engine
from copilot.models import Decision
from copilot.providers import MockProvider
from copilot.store import SessionStore

BRIEF = "Build a scalable URL shortener with an API and a database. It must be fast."


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def accept_all(count: int = 7, **answers: str) -> list[dict[str, object]]:
    return [{"stage": s, "action": "accept", "answers": answers or {}} for s in range(1, count + 1)]


def test_run_with_scripted_decisions_completes_and_records_them_as_scripted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    brief = write(tmp_path / "brief.txt", BRIEF)
    # Stage 1 has open questions: every one must be answered, so the script answers them all with ASSUME.
    answers = {f"Q{i}": "ASSUME" for i in range(1, 12)}
    decisions = write(tmp_path / "d.json", json.dumps(accept_all(7, **answers)))
    code = main(
        [
            "run",
            "--brief",
            str(brief),
            "--out",
            str(tmp_path / "out"),
            "--decisions",
            str(decisions),
            "--session-id",
            "s",
            "--deterministic",
        ]
    )
    assert code == 0 and "complete" in capsys.readouterr().out
    summary = (tmp_path / "out" / "s" / "SUMMARY.md").read_text(encoding="utf-8")
    assert "scripted" in summary and "interactive" not in summary.split("| Stage")[1].replace(
        "Decision mode `interactive`", ""
    )
    assert main(["verify", "s", "--out", str(tmp_path / "out")]) == 0


def test_a_blocked_scripted_decision_stops_with_a_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    brief = write(tmp_path / "brief.txt", BRIEF)
    decisions = write(
        tmp_path / "d.json", json.dumps(accept_all(7))
    )  # no answers for the open questions of stage 1
    code = main(
        [
            "run",
            "--brief",
            str(brief),
            "--out",
            str(tmp_path / "out"),
            "--decisions",
            str(decisions),
            "--session-id",
            "blocked",
        ]
    )
    assert code == 2 and "unanswered open questions" in capsys.readouterr().err


def test_scripted_decisions_can_replace_text_and_must_match_the_stage(tmp_path: Path) -> None:
    engine = Engine(MockProvider(), SessionStore(tmp_path), clock=lambda: "t", mode="scripted")
    session = engine.start(BRIEF, session_id="x")
    prompter = ScriptedPrompter([{"stage": 2, "action": "accept"}])
    with pytest.raises(PrompterError, match="current stage is 1"):
        drive(engine, session, prompter)
    prompter = ScriptedPrompter(
        [{"stage": 1, "action": "modify", "replace": [{"old": "does not exist", "new": "x"}]}]
    )
    with pytest.raises(PrompterError, match="not found in the proposal"):
        drive(engine, session, prompter)
    with pytest.raises(PrompterError, match="no entry"):
        drive(engine, session, ScriptedPrompter([]))


def test_auto_accept_smoke_run(tmp_path: Path) -> None:
    brief = write(tmp_path / "brief.txt", BRIEF)
    assert (
        main(
            [
                "run",
                "--brief",
                str(brief),
                "--out",
                str(tmp_path / "out"),
                "--auto-accept",
                "--session-id",
                "auto",
            ]
        )
        == 0
    )
    assert "auto" in (tmp_path / "out" / "auto" / "SUMMARY.md").read_text(encoding="utf-8")
    assert isinstance(AutoPrompter().mode, str)


def test_external_provider_cannot_be_driven_non_interactively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COPILOT_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("COPILOT_MODEL", "m")
    brief = write(tmp_path / "brief.txt", BRIEF)
    assert (
        main(
            [
                "run",
                "--brief",
                str(brief),
                "--out",
                str(tmp_path / "o"),
                "--provider",
                "openai",
                "--auto-accept",
            ]
        )
        == 2
    )
    assert "external provider" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()  # nothing was created, let alone sent


def inputs(*lines: str):  # type: ignore[no-untyped-def]
    iterator = iter(lines)

    def read(prompt: str = "") -> str:
        try:
            return next(iterator)
        except StopIteration:
            raise AssertionError("the console asked for more input than the test provided") from None

    return read


def test_console_flow_reject_then_answer_questions_then_accept(tmp_path: Path) -> None:
    engine = Engine(MockProvider(), SessionStore(tmp_path), clock=lambda: "t")
    session = engine.start("Build a scalable service. It must be fast.", session_id="c")
    questions = ["ans"] * 12
    console = ConsolePrompter(
        inp=inputs("r", "make it more specific", "a", *questions),
        out=open(tmp_path / "out.txt", "w", encoding="utf-8"),
    )
    for _ in range(2):  # reject, then accept with answers
        proposal = engine.propose(session)
        engine.decide(session, console.decide(session, proposal))
    assert session.records[1].rejections == 1 and session.current_stage == 2
    assert session.records[1].feedback == ["make it more specific"]
    assert session.records[1].decision is not None and session.records[1].decision.mode == "interactive"


def test_console_quit_saves_and_resume_continues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    brief = write(tmp_path / "brief.txt", BRIEF)
    out = str(tmp_path / "out")
    monkeypatch.setattr("sys.stdin", io.StringIO("q\n"))  # the real input() reads this
    assert main(["run", "--brief", str(brief), "--out", out, "--session-id", "q1"]) == 0
    assert "Resume with" in capsys.readouterr().out
    store = SessionStore(Path(out))
    assert store.load("q1").status == "awaiting_decision"
    monkeypatch.setattr("sys.stdin", io.StringIO("q\n"))
    assert (
        main(["resume", "q1", "--out", out]) == 0
    )  # picks up the same pending proposal, does not ask the provider again
    assert store.load("q1").current_stage == 1 and len(store.load("q1").records[1].proposals) == 1


def test_console_declining_an_outbound_send_aborts_without_sending(tmp_path: Path) -> None:
    from conftest import StubProvider

    provider = StubProvider(external=True)
    engine = Engine(provider, SessionStore(tmp_path), clock=lambda: "t")
    session = engine.start(f"{BRIEF} key {FAKE_AWS}", session_id="ext")
    console = ConsolePrompter(inp=inputs("n"), out=open(tmp_path / "o.txt", "w", encoding="utf-8"))
    drive(engine, session, console)
    assert session.status == "aborted" and provider.requests == []
    assert "AKIA" not in (tmp_path / "o.txt").read_text(encoding="utf-8").replace(
        "[REDACTED:AWS_ACCESS_KEY:1]", ""
    )


def test_redact_command_never_prints_the_secret(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sample = write(tmp_path / "s.txt", f"aws {FAKE_AWS} and mail a@example.com")
    assert main(["redact", str(sample)]) == 0
    captured = capsys.readouterr()
    assert FAKE_AWS not in captured.out and "[REDACTED:AWS_ACCESS_KEY:1]" in captured.out
    assert "EMAIL" in captured.err and "a@example.com" not in captured.err


def test_verify_detects_tampering(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    brief = write(tmp_path / "brief.txt", BRIEF)
    main(["run", "--brief", str(brief), "--out", str(tmp_path / "o"), "--auto-accept", "--session-id", "t"])
    audit = tmp_path / "o" / "t" / "audit.jsonl"
    assert verify(audit)[0]
    audit.write_text(audit.read_text(encoding="utf-8").replace('"accept"', '"reject"', 1), encoding="utf-8")
    capsys.readouterr()
    assert main(["verify", "t", "--out", str(tmp_path / "o")]) == 1
    assert "TAMPERED" in capsys.readouterr().out
    assert main(["verify", "missing", "--out", str(tmp_path / "o")]) == 2


def test_usage_errors_exit_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--brief", str(tmp_path / "nope.txt"), "--out", str(tmp_path / "o")]) == 2
    empty = write(tmp_path / "e.txt", "   ")
    assert main(["run", "--brief", str(empty), "--out", str(tmp_path / "o"), "--auto-accept"]) == 2
    assert "empty" in capsys.readouterr().err
    assert Decision(1, "accept").mode == "interactive"
