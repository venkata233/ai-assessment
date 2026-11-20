"""Redaction: remove secrets and personal data before text leaves the engine, and before anything is stored.

Design rules (engineer-owned, see docs/COPILOT_PROTOTYPE.md):
- Redact **before** sending to any provider and **before** writing to disk; raw secrets are never persisted.
- Placeholders are stable within a session (`[REDACTED:EMAIL:1]`) so the model can still reason about "the same value".
- The mapping from placeholder to original value lives only in memory and is never logged or written.
- The report records counts per category, never values.
- Redaction is pattern based: it is a safety net, not a guarantee (high-entropy strings without context, names, and free-text
  PII are NOT detected). That is why external providers also require the outbound confirmation gate.
"""

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Detector:
    kind: str
    pattern: re.Pattern[str]
    group: int = 0  # which capture group holds the sensitive part
    accept: Callable[[str], bool] | None = None  # extra check on the candidate (for example Luhn)


def _luhn_ok(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _public_ipv4(candidate: str) -> bool:
    try:
        address = ipaddress.IPv4Address(candidate)
    except ValueError:
        return False
    return not (address.is_loopback or address.is_unspecified)


_NOT_ALREADY = r"(?!\[REDACTED)"

DETECTORS: tuple[Detector, ...] = (
    Detector(
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----(?:[\s\S]{0,8000}?-----END [A-Z ]{0,30}PRIVATE KEY-----|[\s\S]{0,4000})"
        ),
    ),
    Detector("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Detector(
        "GITHUB_TOKEN", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{40,255})\b")
    ),
    Detector("API_KEY", re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{20,200}\b")),
    Detector("SLACK_TOKEN", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,200}\b")),
    Detector("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    Detector(
        "BEARER_TOKEN", re.compile(rf"(?i)\bbearer\s+{_NOT_ALREADY}([A-Za-z0-9._~+/=-]{{16,}})"), group=1
    ),
    Detector(
        "URL_CREDENTIALS",
        re.compile(rf"(?i)\b[a-z][a-z0-9+.-]{{0,31}}://{_NOT_ALREADY}([^\s/@:]+:[^\s/@]+)@"),
        group=1,
    ),
    Detector(
        "SECRET_VALUE",
        re.compile(
            # The variable name may carry the keyword anywhere (ADMIN_PASSWORD, AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN). The
            # lookbehind only lets a match start at the beginning of a name, which keeps the scan linear on long inputs.
            r"(?i)(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]{0,64}?"
            r"(?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret)[A-Za-z0-9_.-]{0,64}"
            rf"[\"']?\s*[:=]\s*[\"']?{_NOT_ALREADY}([^\s\"',;)]{{4,}})"
        ),
        group=1,
    ),
    Detector("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+\b")),
    Detector(
        "IPV4",
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
        accept=_public_ipv4,
    ),
    Detector("PHONE", re.compile(r"(?<![\w.])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}(?!\w)")),
    Detector("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), accept=_luhn_ok),
)


@dataclass
class RedactionReport:
    """How many values were replaced per category. Contains no values."""

    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def merge(self, other: "RedactionReport") -> None:
        for kind, count in other.counts.items():
            self.counts[kind] = self.counts.get(kind, 0) + count

    def add(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1


class Redactor:
    """Stateful per session: the same original value always maps to the same placeholder."""

    def __init__(self) -> None:
        self._numbers: dict[tuple[str, str], int] = {}
        self._next: dict[str, int] = {}

    def _placeholder(self, kind: str, value: str) -> str:
        key = (kind, value)
        if key not in self._numbers:
            self._next[kind] = self._next.get(kind, 0) + 1
            self._numbers[key] = self._next[kind]
        return f"[REDACTED:{kind}:{self._numbers[key]}]"

    def redact(self, text: str) -> tuple[str, RedactionReport]:
        report = RedactionReport()
        for detector in DETECTORS:

            def replace(match: re.Match[str], detector: Detector = detector) -> str:
                secret = match.group(detector.group)
                if detector.accept is not None and not detector.accept(secret):
                    return match.group(0)
                report.add(detector.kind)
                placeholder = self._placeholder(detector.kind, secret)
                if detector.group == 0:
                    return placeholder
                start, end = match.span(detector.group)
                offset = match.start()
                return match.group(0)[: start - offset] + placeholder + match.group(0)[end - offset :]

            text = detector.pattern.sub(replace, text)
        return text, report


def scan(text: str) -> RedactionReport:
    """Report what *would* be redacted without changing anything (used on provider output and for previews)."""
    return Redactor().redact(text)[1]
