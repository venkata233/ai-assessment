import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from copilot.providers import (
    CompletionRequest,
    MockProvider,
    OpenAICompatibleProvider,
    ProviderError,
    create_provider,
)
from copilot.providers.mock import BANNER
from copilot.stages import STAGES

REQUEST = CompletionRequest(
    1, "system text", "user text", {"brief": "b", "context_files": [], "previous": {}, "feedback": []}
)


@contextmanager
def stub_server(
    handler: Callable[[BaseHTTPRequestHandler], None],
) -> Iterator[tuple[str, list[dict[str, str]]]]:
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode()
            seen.append({"path": self.path, "auth": self.headers.get("Authorization", ""), "body": body})
            handler(self)

        def log_message(self, *args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", seen
    finally:
        server.shutdown()
        server.server_close()


def reply(payload: object, status: int = 200) -> Callable[[BaseHTTPRequestHandler], None]:
    def respond(h: BaseHTTPRequestHandler) -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        h.send_response(status)
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)

    return respond


def test_mock_provider_covers_every_stage_with_every_required_heading() -> None:
    inputs = {
        "brief": "Build a scalable API with a database. It must be fast.",
        "context_files": [],
        "previous": {},
        "feedback": [],
    }
    for spec in STAGES:
        text = MockProvider().complete(CompletionRequest(spec.number, "", "", inputs)).text
        assert text.startswith(BANNER.strip()[:40])
        for heading in spec.required_sections:
            assert f"## {heading}" in text, (spec.number, heading)


def test_mock_provider_is_local_deterministic_and_marks_itself() -> None:
    provider = MockProvider()
    assert provider.is_external is False
    inputs = {"brief": "Build X.", "context_files": [], "previous": {}, "feedback": []}
    first = provider.complete(CompletionRequest(1, "", "", inputs))
    assert first == provider.complete(CompletionRequest(1, "", "", inputs))
    assert "no language model" in first.text and first.model == "mock-rule-based-v1"


def test_mock_never_makes_decisions_for_the_engineer() -> None:
    inputs = {
        "brief": "Build an API with a database and a cache.",
        "context_files": [],
        "previous": {},
        "feedback": [],
    }
    design = MockProvider().complete(CompletionRequest(3, "", "", inputs)).text
    assert "Engineer decision: ____ (DECISION REQUIRED)" in design
    assert "we chose" not in design.lower() and "recommended" not in design.lower()


def test_openai_provider_sends_the_key_and_parses_the_answer() -> None:
    answer = {"choices": [{"message": {"content": "## Interpretation\nhi"}}]}
    with stub_server(reply(answer)) as (url, seen):
        provider = OpenAICompatibleProvider(url, "test-model", "k-secret-123456")
        response = provider.complete(REQUEST)
    assert response.text.startswith("## Interpretation") and response.model == "test-model"
    assert seen[0]["path"] == "/v1/chat/completions" and seen[0]["auth"] == "Bearer k-secret-123456"
    payload = json.loads(seen[0]["body"])
    assert payload["model"] == "test-model" and payload["temperature"] == 0
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]


def test_openai_provider_without_a_key_sends_no_authorization_header() -> None:
    with stub_server(reply({"choices": [{"message": {"content": "ok"}}]})) as (url, seen):
        OpenAICompatibleProvider(url, "m", None).complete(REQUEST)
    assert seen[0]["auth"] == ""


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_errors_become_provider_errors_without_the_key_or_body(status: int) -> None:
    with stub_server(reply({"error": "SECRET-DETAIL k-secret-123456"}, status)) as (url, _):
        with pytest.raises(ProviderError) as info:
            OpenAICompatibleProvider(url, "m", "k-secret-123456").complete(REQUEST)
    assert (
        str(status) in str(info.value)
        and "k-secret" not in str(info.value)
        and "SECRET-DETAIL" not in str(info.value)
    )


def test_redirects_are_not_followed_so_the_key_cannot_be_forwarded() -> None:
    with stub_server(reply({"choices": [{"message": {"content": "elsewhere"}}]})) as (other_url, other_seen):

        def redirect(h: BaseHTTPRequestHandler) -> None:
            h.send_response(307)
            h.send_header("Location", other_url + "/chat/completions")
            h.send_header("Content-Length", "0")
            h.end_headers()

        with stub_server(redirect) as (url, _), pytest.raises(ProviderError, match="HTTP 307"):
            OpenAICompatibleProvider(url, "m", "k-secret-123456").complete(REQUEST)
        assert other_seen == []  # the second server never saw the request or the key


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": "   "}}]},
        {"choices": [{"message": {"content": 5}}]},
    ],
)
def test_malformed_provider_answers_are_rejected(payload: object) -> None:
    with stub_server(reply(payload)) as (url, _), pytest.raises(ProviderError):
        OpenAICompatibleProvider(url, "m", None).complete(REQUEST)


def test_oversized_answers_are_rejected() -> None:
    huge = b'{"choices":[{"message":{"content":"' + b"a" * 1_100_000 + b'"}}]}'
    with stub_server(reply(huge)) as (url, _), pytest.raises(ProviderError, match="too large"):
        OpenAICompatibleProvider(url, "m", None).complete(REQUEST)


def test_unreachable_provider_is_a_clean_error() -> None:
    with pytest.raises(ProviderError, match="could not reach"):
        OpenAICompatibleProvider("http://127.0.0.1:9/v1", "m", "k", timeout_seconds=1).complete(REQUEST)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",
        "ftp://x/v1",
        "https://user:pw@api.example.com/v1",
        "api.example.com",
        "https:///v1",
    ],
)
def test_unsafe_base_urls_are_refused(url: str) -> None:
    with pytest.raises(ProviderError):
        OpenAICompatibleProvider(url, "m", "k")


def test_external_versus_local_is_decided_by_the_host() -> None:
    assert OpenAICompatibleProvider("https://api.example.com/v1", "m", "k").is_external
    for local in ("http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1"):
        assert not OpenAICompatibleProvider(local, "m", None).is_external


def test_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(create_provider("mock"), MockProvider)
    for name in ("COPILOT_BASE_URL", "COPILOT_MODEL", "COPILOT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ProviderError, match="COPILOT_BASE_URL"):
        create_provider("openai")
    monkeypatch.setenv("COPILOT_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("COPILOT_MODEL", "m")
    assert create_provider("openai").is_external
    with pytest.raises(ProviderError, match="unknown provider"):
        create_provider("other")
