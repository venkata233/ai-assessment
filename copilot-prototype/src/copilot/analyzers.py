"""Rule-based analysis used by the offline mock provider.

Everything here is a heuristic: it finds *candidates* for the engineer to examine. It does not prove anything and it is not a
language model. Findings and flags are always labelled accordingly in the generated output.
"""

import re
from dataclasses import dataclass

from copilot.context import ContextFile
from copilot.redaction import Redactor

# ----- brief analysis ---------------------------------------------------------------------------------------------

VAGUE_TERMS: dict[str, str] = {
    "fast": "What latency target (for example p95 in ms) and under what load?",
    "quick": "What response time is acceptable, and for which operations?",
    "scalable": "Scalable to what: users, requests per second, data volume, over what period?",
    "scale": "Scale to what numbers, and how soon?",
    "secure": "Which threats and which standards (authentication, authorisation, data protection) are in scope?",
    "simple": "Simple for whom, and what must be absent to count as simple?",
    "easy": "Easy for which user, measured how?",
    "intuitive": "Which users and which tasks should be self-explanatory?",
    "user-friendly": "Which users and what usability criteria?",
    "robust": "Which failures must it survive, and with what recovery behaviour?",
    "reliable": "What availability target and what is the cost of an outage?",
    "flexible": "Flexible in which dimensions (configuration, extension points, formats)?",
    "modern": "Which concrete technologies or practices are required or forbidden?",
    "real-time": "What maximum delay counts as real time?",
    "high availability": "What uptime target, and what is the recovery objective?",
    "large": "How large (rows, bytes, users)?",
    "many": "How many, as a number?",
    "etc": "The list is open-ended: what is the complete list?",
    "and so on": "The list is open-ended: what is the complete list?",
    "as needed": "Who decides what is needed, and by what rule?",
    "optimal": "Optimal by which measure?",
    "best": "Best by which measure?",
}

NFR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "scale / volume": (
        "per second",
        "per day",
        "users",
        "requests",
        "million",
        "thousand",
        "rps",
        "qps",
        "throughput",
    ),
    "latency": ("latency", "ms", "milliseconds", "response time", "p95", "p99"),
    "availability": ("uptime", "availability", "99.", "sla", "downtime"),
    "security": ("auth", "encrypt", "tls", "owasp", "permission", "token", "password"),
    "privacy / retention": ("privacy", "gdpr", "pii", "retention", "personal data", "consent"),
    "observability": ("log", "metric", "monitor", "alert", "trace"),
}

COMPONENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "HTTP API": ("api", "endpoint", "rest", "http", "service"),
    "Persistent storage": ("database", "persist", "store", "storage", "sql", "record"),
    "Cache": ("cache", "fast", "latency", "low-latency"),
    "Background processing": ("queue", "async", "batch", "background", "worker", "event"),
    "Analytics / reporting": ("analytics", "report", "dashboard", "metrics", "statistics", "count"),
    "Authentication and authorisation": ("login", "auth", "user", "account", "permission", "role", "api key"),
    "User interface": ("ui", "web page", "frontend", "dashboard", "screen", "form"),
}

ACTOR_WORDS = (
    "user",
    "admin",
    "administrator",
    "customer",
    "visitor",
    "operator",
    "developer",
    "client",
    "manager",
    "analyst",
    "engineer",
)
REQUIREMENT_VERBS = re.compile(
    r"\b(must|shall|should|can|could|needs? to|will|want|wants|allow|allows|support|supports|provide|provides|record|records|expose|exposes"
    r"|build|create|implement|develop|design|make|enable|enables|track|tracks)\b",
    re.I,
)
INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all previous",
    "disregard the above",
    "you are now",
    "system prompt",
    "reveal your instructions",
    "act as",
)


@dataclass(frozen=True)
class VagueHit:
    term: str
    sentence: str
    question: str


@dataclass(frozen=True)
class BriefAnalysis:
    sentences: tuple[str, ...]
    requirements: tuple[str, ...]
    vague: tuple[VagueHit, ...]
    missing_nfr: tuple[str, ...]
    actors: tuple[str, ...]
    components: tuple[str, ...]
    injection: tuple[str, ...]


def split_sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text.strip())
    parts = re.split(r"(?<=[.!?])\s+|\s*\n\s*|\s*[;]\s+", cleaned)
    return [p.strip(" -*\u2022") for p in parts if len(p.strip(" -*\u2022")) > 2]


def _contains_word(haystack: str, term: str, *, prefix: bool = False) -> bool:
    """Whole-word match; with `prefix=True` the term may be the start of a longer word ("persist" matches "persistence")."""
    tail = "" if prefix else r"(?![\w-])"
    return re.search(rf"(?<![\w-]){re.escape(term)}{tail}", haystack, re.I) is not None


def analyse_brief(brief: str) -> BriefAnalysis:
    sentences = split_sentences(brief)
    requirements = [s for s in sentences if REQUIREMENT_VERBS.search(s)] or sentences[:5]
    vague: list[VagueHit] = []
    seen: set[str] = set()
    for sentence in sentences:
        for term, question in VAGUE_TERMS.items():
            if term not in seen and _contains_word(sentence, term):
                seen.add(term)
                vague.append(VagueHit(term, sentence, question))
    lowered = brief.lower()
    missing = [name for name, words in NFR_KEYWORDS.items() if not any(w in lowered for w in words)]
    has_number = re.search(r"\d", brief) is not None
    if not has_number and "scale / volume" not in missing:
        missing.append("scale / volume")
    actors = [a for a in ACTOR_WORDS if _contains_word(brief, a, prefix=len(a) > 3)]
    components = [
        name
        for name, words in COMPONENT_KEYWORDS.items()
        if any(_contains_word(brief, w, prefix=len(w) > 3) for w in words)
    ]
    injection = [m for m in INJECTION_MARKERS if m in lowered]
    return BriefAnalysis(
        tuple(sentences),
        tuple(requirements),
        tuple(vague),
        tuple(missing),
        tuple(actors),
        tuple(components),
        tuple(injection),
    )


# ----- brownfield code scanning -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    rule: str
    severity: str  # high | medium | low (heuristic severity, not a CVSS score)
    message: str
    snippet: str  # already redacted


_READ_HINT = re.compile(
    r"(select\b.*\b(from|where)\b|\.exists\(|\.first\(|\.get\(|\.filter|\bcount\(|is_taken|already_exists|\bexists\b)",
    re.I,
)
_KEY_HINT = re.compile(r"\b(alias|slug|code|username|email|name|handle)\b", re.I)
_WRITE_HINT = re.compile(r"(insert\s+into|\.insert\(|\.add\(|\.create\(|\.save\(|\.put\()", re.I)
_GUARD_HINT = re.compile(
    r"(on\s+conflict|unique|IntegrityError|UniqueViolation|for\s+update|\block\b|\.acquire\(|serializable)",
    re.I,
)

_LINE_RULES: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    (
        "INSECURE_RANDOM",
        "medium",
        re.compile(r"\brandom\.(choice|choices|randint|randrange|random|sample)\b"),
        "Non-cryptographic random: predictable for tokens, codes, or identifiers. Consider `secrets`.",
    ),
    (
        "SQL_STRING_BUILDING",
        "high",
        re.compile(
            r"execute\(\s*f[\"']|execute\([^)]*\.format\(|execute\([^)]*[\"']\s*%\s*[\(\w]|(select|insert|update|delete)\b[^\n]*[\"']\s*\+\s*\w",
            re.I,
        ),
        "SQL built from strings: possible injection. Use bound parameters.",
    ),
    (
        "WEAK_HASH",
        "medium",
        re.compile(r"hashlib\.(md5|sha1)\("),
        "MD5/SHA-1 are not suitable for security purposes.",
    ),
    ("TLS_VERIFY_OFF", "high", re.compile(r"verify\s*=\s*False"), "Certificate verification disabled."),
    (
        "DANGEROUS_CALL",
        "high",
        re.compile(
            r"\beval\(|\bexec\(|pickle\.loads?\(|subprocess\.[a-z_]+\([^)]*shell\s*=\s*True|\bos\.system\("
        ),
        "Dangerous call: code execution or unsafe deserialisation of possibly untrusted input.",
    ),
)
_SWALLOW = re.compile(r"except\s*(?:Exception)?\s*:\s*\n\s*pass\b")


def _snippet(line: str, redactor: Redactor) -> str:
    return redactor.redact(line.strip())[0][:120]


def scan_code(files: list[ContextFile]) -> list[Finding]:
    findings: list[Finding] = []
    redactor = Redactor()
    for file in files:
        lines = file.text.splitlines()
        for number, line in enumerate(lines, start=1):
            for rule, severity, pattern, message in _LINE_RULES:
                if pattern.search(line):
                    findings.append(
                        Finding(file.path, number, rule, severity, message, _snippet(line, redactor))
                    )
            if (
                "[REDACTED:" in line or redactor.redact(line)[1].total
            ):  # context is redacted before it is scanned
                findings.append(
                    Finding(
                        file.path,
                        number,
                        "HARDCODED_SECRET",
                        "high",
                        "A value that looks like a credential or personal data is present in the source.",
                        _snippet(line, redactor),
                    )
                )
        findings.extend(_check_then_insert(file, lines, redactor))
        for match in _SWALLOW.finditer(file.text):
            line_number = file.text.count("\n", 0, match.start()) + 1
            findings.append(
                Finding(
                    file.path,
                    line_number,
                    "SWALLOWED_EXCEPTION",
                    "low",
                    "Exception swallowed silently: failures become invisible.",
                    _snippet(lines[line_number - 1], redactor),
                )
            )
    return sorted(findings, key=lambda f: (f.file, f.line, f.rule))


def _check_then_insert(file: ContextFile, lines: list[str], redactor: Redactor) -> list[Finding]:
    """Read-then-write on a key in one function without a uniqueness guard: the classic race behind duplicate-alias bugs."""
    results: list[Finding] = []
    starts = [i for i, line in enumerate(lines) if re.match(r"\s*(async\s+)?def\s+\w+", line)]
    bounds = [*starts[1:], len(lines)]
    for start, end in zip(starts, bounds, strict=False):
        body = lines[start:end]
        text = "\n".join(body)
        if _GUARD_HINT.search(text) or not _WRITE_HINT.search(text):
            continue
        for offset, line in enumerate(body):
            if _READ_HINT.search(line) and _KEY_HINT.search(line):
                results.append(
                    Finding(
                        file.path,
                        start + offset + 1,
                        "CHECK_THEN_INSERT",
                        "high",
                        "Reads whether a key exists, then writes it, with no uniqueness constraint, lock, or conflict handling "
                        "in the same function: two concurrent requests can both pass the check (duplicate-key race).",
                        _snippet(line, redactor),
                    )
                )
                break
    return results
