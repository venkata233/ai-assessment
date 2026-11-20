"""Provider port: the only thing the engine knows about a model."""

from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(Exception):
    """Any failure talking to a provider. Messages never contain credentials or response bodies."""


@dataclass(frozen=True)
class CompletionRequest:
    stage: int
    system: str
    user: str
    inputs: dict[str, Any]  # structured form of the same data, for local providers that do not parse prompts


@dataclass(frozen=True)
class CompletionResponse:
    text: str
    model: str


class Provider(Protocol):
    name: str

    @property
    def is_external(self) -> bool:
        """True when the prompt leaves this machine. External providers require the outbound confirmation gate."""
        ...

    def complete(self, request: CompletionRequest) -> CompletionResponse: ...
