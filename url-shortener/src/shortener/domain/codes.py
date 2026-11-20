"""Alias rules and the short-code generator."""

import re
import secrets
import string
from typing import Protocol

from shortener.domain.errors import ValidationFailed

CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase

# Paths served by the application itself, plus names that invite phishing or confusion.
# Compared case-insensitively so `API` and `Docs` are also refused.
RESERVED_ALIASES = frozenset(
    {
        "api",
        "health",
        "docs",
        "redoc",
        "openapi",
        "metrics",
        "admin",
        "static",
        "assets",
        "login",
        "logout",
        "signin",
        "signup",
        "status",
        "robots",
        "favicon",
        "sitemap",
        "www",
    }
)


class CodeGenerator(Protocol):
    def generate(self) -> str: ...


def is_valid_code_format(code: str) -> bool:
    # fullmatch, not match: `$` also matches before a trailing newline, so "admin\n" would slip through.
    return CODE_PATTERN.fullmatch(code) is not None


def is_reserved(code: str) -> bool:
    return code.lower() in RESERVED_ALIASES


def validate_alias(alias: str) -> str:
    if not is_valid_code_format(alias):
        raise ValidationFailed.single(
            "alias", "alias-invalid", "Alias must be 3-32 characters from A-Z, a-z, 0-9, '_' and '-'."
        )
    if is_reserved(alias):
        raise ValidationFailed.single("alias", "alias-reserved", "This alias is reserved.")
    return alias


class RandomBase62Generator:
    """Uniform Base62 codes from the OS CSPRNG.

    `secrets.choice` draws an index with rejection sampling, so there is no modulo bias, and the
    output is unpredictable. Uniqueness is *not* decided here: the database unique constraint is.
    """

    def __init__(self, length: int = 7) -> None:
        if length < 6:
            raise ValueError("code length must be at least 6")
        self._length = length

    def generate(self) -> str:
        while True:
            code = "".join(secrets.choice(BASE62) for _ in range(self._length))
            if not is_reserved(code):
                return code
