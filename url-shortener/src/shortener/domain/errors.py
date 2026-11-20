"""Domain errors. The API layer maps each of these to an RFC 7807 problem."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldIssue:
    field: str
    code: str
    message: str


class DomainError(Exception):
    """Base class for all expected, mappable failures."""


class ValidationFailed(DomainError):
    def __init__(self, issues: list[FieldIssue]) -> None:
        super().__init__("; ".join(f"{i.field}: {i.code}" for i in issues))
        self.issues = issues

    @classmethod
    def single(cls, field: str, code: str, message: str) -> "ValidationFailed":
        return cls([FieldIssue(field, code, message)])


class AliasConflict(DomainError):
    """Alias exists or was previously deleted (permanently reserved)."""


class CodeTaken(DomainError):
    """Repository signal: the unique constraint on `code` fired."""


class LinkNotFound(DomainError):
    """Unknown, deleted, or owned by another key (deliberately indistinguishable)."""


class LinkExpired(DomainError):
    pass


class Unauthorized(DomainError):
    pass


class RateLimited(DomainError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.retry_after = max(1, retry_after)


class StorageUnavailable(DomainError):
    """PostgreSQL is unreachable or timed out."""


class CodeGenerationExhausted(DomainError):
    """All bounded retries for a generated code collided."""
