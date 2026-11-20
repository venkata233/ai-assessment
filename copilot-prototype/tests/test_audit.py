import json
from pathlib import Path

import pytest

from conftest import FAKE_AWS, FIXED
from copilot.audit import GENESIS, AuditError, AuditLog, verify


def make(tmp_path: Path) -> tuple[AuditLog, Path]:
    path = tmp_path / "audit.jsonl"
    return AuditLog(path, lambda: FIXED), path


def test_chain_verifies_and_links_entries(tmp_path: Path) -> None:
    log, path = make(tmp_path)
    first = log.append("a", {"n": 1})
    second = log.append("b", {"n": 2})
    assert first["prev"] == GENESIS and second["prev"] == first["hash"] and second["seq"] == 2
    assert verify(path) == (True, None, 2)


def test_editing_an_entry_is_detected(tmp_path: Path) -> None:
    log, path = make(tmp_path)
    for i in range(3):
        log.append("e", {"i": i})
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["data"]["i"] = 99
    lines[1] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify(path) == (False, 2, 1)


def test_deleting_a_middle_entry_is_detected(tmp_path: Path) -> None:
    log, path = make(tmp_path)
    for i in range(4):
        log.append("e", {"i": i})
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2], lines[3]]) + "\n", encoding="utf-8")
    ok, bad, _ = verify(path)
    assert not ok and bad == 2


def test_truncating_the_tail_is_not_detected_by_the_chain_alone(tmp_path: Path) -> None:
    """Documented limit: keep the head hash elsewhere (the session summary records it) if tail truncation matters."""
    log, path = make(tmp_path)
    for i in range(3):
        log.append("e", {"i": i})
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    assert verify(path)[0] is True


def test_garbage_lines_fail_verification(tmp_path: Path) -> None:
    _, path = make(tmp_path)
    path.write_text("not json\n", encoding="utf-8")
    assert verify(path) == (False, 1, 0)


def test_reopening_continues_the_chain_and_refuses_a_corrupt_file(tmp_path: Path) -> None:
    log, path = make(tmp_path)
    log.append("a", {})
    reopened = AuditLog(path, lambda: FIXED)
    assert reopened.append("b", {})["seq"] == 2 and verify(path)[0]
    path.write_text(path.read_text(encoding="utf-8").replace('"a"', '"z"'), encoding="utf-8")
    with pytest.raises(AuditError):
        AuditLog(path, lambda: FIXED)


def test_entries_containing_secrets_are_refused(tmp_path: Path) -> None:
    log, path = make(tmp_path)
    with pytest.raises(AuditError):
        log.append("oops", {"note": f"key {FAKE_AWS}"})
    assert not path.exists() or path.read_text(encoding="utf-8") == ""
