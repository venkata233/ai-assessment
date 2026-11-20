"""Safe loading of brownfield context (existing code or notes) for analysis.

The loader is deliberately conservative: it only reads regular text files under the given root, never follows symlinks out
of it, skips files that usually hold secrets, and enforces hard limits so a large repository cannot flood a prompt.
"""

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

ALLOWED_SUFFIXES = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".java",
        ".go",
        ".rb",
        ".php",
        ".cs",
        ".sql",
        ".md",
        ".txt",
        ".yaml",
        ".yml",
        ".json",
        ".toml",
        ".cfg",
        ".ini",
        ".sh",
    }
)
SKIP_DIRS = frozenset(
    {".git", "node_modules", "venv", ".venv", "__pycache__", ".agent", "dist", "build", ".idea", ".vscode"}
)
SECRET_FILE_PATTERNS = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    ".env*",
    ".netrc",
    ".pypirc",
    ".npmrc",
    "*secret*",
    "*credential*",
)


@dataclass(frozen=True)
class ContextFile:
    path: str  # relative to the context root, with forward slashes
    text: str
    truncated: bool


@dataclass
class ContextBundle:
    files: list[ContextFile] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)


def _is_secret_file(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in SECRET_FILE_PATTERNS)


def load_context(
    path: Path, *, max_files: int = 25, max_file_bytes: int = 20_000, max_total_bytes: int = 200_000
) -> ContextBundle:
    """Read candidate files in a stable order. Raises FileNotFoundError when the path does not exist."""
    target = path.resolve(strict=True)
    root = target.parent if target.is_file() else target
    candidates: list[Path] = []
    if target.is_file():
        candidates.append(target)
    else:
        for current, dirs, names in os.walk(target, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            candidates.extend(Path(current) / name for name in sorted(names))

    bundle = ContextBundle()
    total = 0
    for candidate in candidates:
        relative = (
            candidate.relative_to(root).as_posix() if candidate.is_relative_to(root) else candidate.name
        )
        if candidate.is_symlink():
            bundle.skipped.append((relative, "symlink"))
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            bundle.skipped.append((relative, "outside root"))
            continue
        if _is_secret_file(candidate.name):
            bundle.skipped.append((relative, "secret-like file name"))
            continue
        if candidate.suffix.lower() not in ALLOWED_SUFFIXES:
            bundle.skipped.append((relative, "unsupported type"))
            continue
        if len(bundle.files) >= max_files or total >= max_total_bytes:
            bundle.skipped.append((relative, "limit reached"))
            continue
        data = resolved.read_bytes()[: max_file_bytes + 1]
        if b"\x00" in data:
            bundle.skipped.append((relative, "binary"))
            continue
        truncated = len(data) > max_file_bytes
        text = data[:max_file_bytes].decode("utf-8", errors="replace")
        total += len(text)
        bundle.files.append(ContextFile(relative, text, truncated))
    return bundle
