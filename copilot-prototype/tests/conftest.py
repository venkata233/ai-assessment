"""Shared test helpers: a scriptable stub provider, a tiny engine factory, and fake-secret builders."""

from collections.abc import Callable
from pathlib import Path

import pytest

from copilot.engine import Engine
from copilot.providers import CompletionRequest, CompletionResponse, MockProvider, ProviderError
from copilot.stages import STAGE_BY_NUMBER
from copilot.store import SessionStore

FIXED = "2026-11-20T00:00:00Z"

# Obviously fake credentials shaped like real ones, assembled at run time so no scanner flags this file.
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
FAKE_SK = "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
FAKE_PASSWORD = "hunter2-correct-horse"


def full_text(stage: int, extra: str = "", questions: str = "") -> str:
    """A valid proposal for a stage: every required heading present; `questions` fills the 'Open questions' section."""
    spec = STAGE_BY_NUMBER[stage]
    body = "\n\n".join(
        f"## {name}\n{questions if (name == 'Open questions' and questions) else 'content'}"
        for name in spec.required_sections
    )
    return body + extra


class StubProvider:
    """Records what it is asked and answers from a callable; can pretend to be external."""

    name = "stub"

    def __init__(
        self, answer: Callable[[CompletionRequest], str] | None = None, external: bool = False
    ) -> None:
        self._answer = answer or (lambda request: full_text(request.stage))
        self._external = external
        self.requests: list[CompletionRequest] = []
        self.fail_next = False

    @property
    def is_external(self) -> bool:
        return self._external

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self.fail_next:
            self.fail_next = False
            raise ProviderError("simulated provider outage")
        self.requests.append(request)
        return CompletionResponse(self._answer(request), "stub-model")


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


@pytest.fixture
def make_engine(store: SessionStore) -> Callable[..., Engine]:
    def factory(provider: object | None = None, **kwargs: object) -> Engine:
        return Engine(provider or MockProvider(), store, clock=lambda: FIXED, **kwargs)  # type: ignore[arg-type]

    return factory
