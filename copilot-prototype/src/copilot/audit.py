"""Append-only, hash-chained audit log (one JSON object per line).

Each entry commits to the previous one (`prev`), so editing or deleting an earlier line is detectable by `verify`. This is
*tamper-evident*, not tamper-proof: someone who can rewrite the whole file can recompute every hash. Keep a copy of the final
hash elsewhere (for example in the commit message) if that matters.

Entries hold metadata only (hashes, counts, decisions), never prompt text, brief text, or secrets; `append` refuses data that
looks like it contains a secret.
"""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from copilot.redaction import scan

GENESIS = "0" * 64


class AuditError(Exception):
    pass


def _digest(entry: dict[str, Any]) -> str:
    body = {k: entry[k] for k in ("seq", "ts", "event", "data", "prev")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path, clock: Callable[[], str]) -> None:
        self._path = path
        self._clock = clock
        self._seq = 0
        self._prev = GENESIS
        if path.exists():
            ok, bad, count = verify(path)
            if not ok:
                raise AuditError(f"existing audit log is corrupt at entry {bad}")
            if count:
                last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
                self._seq, self._prev = last["seq"], last["hash"]

    @property
    def last_hash(self) -> str:
        return self._prev

    def append(self, event: str, data: dict[str, Any]) -> dict[str, Any]:
        serialised = json.dumps(data, sort_keys=True)
        if scan(serialised).total:
            raise AuditError(f"refusing to log {event!r}: the data contains secret-like values")
        entry: dict[str, Any] = {
            "seq": self._seq + 1,
            "ts": self._clock(),
            "event": event,
            "data": data,
            "prev": self._prev,
        }
        entry["hash"] = _digest(entry)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
        self._seq, self._prev = entry["seq"], entry["hash"]
        return entry


def verify(path: Path) -> tuple[bool, int | None, int]:
    """Return (ok, first_bad_seq_or_None, entries_checked)."""
    prev = GENESIS
    count = 0
    for expected_seq, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            entry = json.loads(line)
            valid = entry["seq"] == expected_seq and entry["prev"] == prev and entry["hash"] == _digest(entry)
        except (ValueError, KeyError, TypeError):
            return False, expected_seq, count
        if not valid:
            return False, expected_seq, count
        prev = entry["hash"]
        count += 1
    return True, None, count
