import http.client
import re
import threading
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import FAKE_AWS, StubProvider
from copilot.providers import MockProvider
from copilot.web import CopilotServer, make_server

CSRF_RE = re.compile(r'name=csrf value="([0-9a-f]+)"')
ANSWER_RE = re.compile(r'name="(answer_Q\d+)"')


@pytest.fixture
def server(tmp_path: Path) -> Iterator[CopilotServer]:
    srv = make_server(tmp_path / "sessions", MockProvider(), 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(
    srv: CopilotServer,
    method: str,
    path: str,
    form: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> tuple[int, dict[str, str], str]:
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    all_headers = dict(headers or {})
    body = raw
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        all_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    conn.request(method, path, body=body, headers=all_headers)
    response = conn.getresponse()
    text = response.read().decode("utf-8", errors="replace")
    result = (response.status, {k.lower(): v for k, v in response.getheaders()}, text)
    conn.close()
    return result


def new_session(
    srv: CopilotServer, brief: str = "Build a scalable service with an API. It must be fast."
) -> str:
    token = CSRF_RE.search(call(srv, "GET", "/")[2])
    assert token is not None
    status, headers, _ = call(srv, "POST", "/new", {"brief": brief, "csrf": token.group(1)})
    assert status == 303
    return headers["location"].split("/")[-1]


def token_for(srv: CopilotServer, session_id: str) -> str:
    match = CSRF_RE.search(call(srv, "GET", f"/s/{session_id}")[2])
    assert match is not None
    return match.group(1)


def test_binds_to_loopback_and_sends_hardening_headers(server: CopilotServer) -> None:
    assert server.server_address[0] == "127.0.0.1"
    status, headers, body = call(server, "GET", "/")
    assert status == 200
    assert (
        "default-src 'none'" in headers["content-security-policy"]
        and "frame-ancestors 'none'" in headers["content-security-policy"]
    )
    assert (
        headers["x-content-type-options"] == "nosniff"
        and headers["cache-control"] == "no-store"
        and headers["referrer-policy"] == "no-referrer"
    )
    assert "x-powered-by" not in headers
    assert "<script" not in body  # no scripts at all


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "localhost:1", "127.0.0.1.evil.example:80"])
def test_foreign_host_headers_are_refused_to_stop_dns_rebinding(server: CopilotServer, host: str) -> None:
    for method in ("GET", "POST"):
        assert (
            call(server, method, "/", headers={"Host": host}, form={} if method == "POST" else None)[0] == 400
        )


def test_posts_need_a_valid_csrf_token_and_same_origin(server: CopilotServer) -> None:
    brief = {"brief": "Build X."}
    assert call(server, "POST", "/new", brief)[0] == 403  # no token
    assert call(server, "POST", "/new", {**brief, "csrf": "0" * 64})[0] == 403
    token = CSRF_RE.search(call(server, "GET", "/")[2]).group(1)  # type: ignore[union-attr]
    assert (
        call(server, "POST", "/new", {**brief, "csrf": token}, headers={"Origin": "http://evil.example"})[0]
        == 403
    )
    assert (
        call(
            server,
            "POST",
            "/new",
            {**brief, "csrf": token},
            headers={"Origin": f"http://127.0.0.1:{server.server_address[1]}"},
        )[0]
        == 303
    )


def test_a_token_for_one_session_does_not_work_for_another(server: CopilotServer) -> None:
    first, second = new_session(server), new_session(server)
    stolen = token_for(server, first)
    assert call(server, "POST", f"/s/{second}/propose", {"csrf": stolen})[0] == 403
    assert call(server, "POST", f"/s/{second}/propose", {"csrf": token_for(server, second)})[0] == 303


def test_request_size_and_type_limits(server: CopilotServer) -> None:
    token = CSRF_RE.search(call(server, "GET", "/")[2]).group(1)  # type: ignore[union-attr]
    # The server refuses an oversized body from its Content-Length without reading it. Depending on timing the client
    # sees the 413 or (notably on Windows) a connection reset while it is still sending; both mean "refused".
    try:
        assert call(server, "POST", "/new", {"brief": "x" * 70_000, "csrf": token})[0] == 413
    except (ConnectionError, OSError):
        pass
    assert (
        call(server, "POST", "/new", raw=b'{"brief":"x"}', headers={"Content-Type": "application/json"})[0]
        == 415
    )
    assert (
        call(
            server,
            "POST",
            "/new",
            raw=b"brief=x",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )[0]
        == 403  # well-formed but no CSRF token
    )
    assert (
        call(server, "POST", "/new", {"brief": "x" * 20_001, "csrf": token})[0] == 400
    )  # longer than the brief limit


def test_user_text_is_escaped_everywhere(server: CopilotServer) -> None:
    payload = "<script>alert(1)</script> Build a service. <img src=x onerror=alert(1)> It must be fast."
    session_id = new_session(server, payload)
    call(server, "POST", f"/s/{session_id}/propose", {"csrf": token_for(server, session_id)})
    _, _, page = call(server, "GET", f"/s/{session_id}")
    assert "<script>alert(1)</script>" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page
    # the brief artifact is served as plain text with nosniff, so it is not interpreted as HTML
    _, headers, brief = call(server, "GET", f"/s/{session_id}/a/00_brief.md")
    assert headers["content-type"].startswith("text/plain") and headers["x-content-type-options"] == "nosniff"


def test_artifacts_are_allow_listed_and_paths_cannot_escape(server: CopilotServer) -> None:
    session_id = new_session(server)
    assert call(server, "GET", f"/s/{session_id}/a/audit.jsonl")[0] == 200
    for name in (
        "session.json",
        "context.json",
        "..%2f..%2fsession.json",
        "%2e%2e%2fsession.json",
        "prompts",
    ):
        assert call(server, "GET", f"/s/{session_id}/a/{name}")[0] == 404, name
    for path in ("/s/..%2f..%2fx", "/s/nope", "/s/%00", "/etc/passwd", "/s/" + "a" * 100):
        assert call(server, "GET", path)[0] == 404, path


def test_the_gates_apply_through_the_web_ui_too(server: CopilotServer) -> None:
    session_id = new_session(server)
    token = token_for(server, session_id)
    status, _, page = call(server, "POST", f"/s/{session_id}/decide", {"csrf": token, "action": "accept"})
    assert status == 200 and "Blocked" in page and "no proposal is waiting" in page
    call(server, "POST", f"/s/{session_id}/propose", {"csrf": token})
    status, _, page = call(server, "POST", f"/s/{session_id}/decide", {"csrf": token, "action": "accept"})
    assert status == 200 and "unanswered open questions" in page  # gate G3, from the engine
    assert call(server, "POST", f"/s/{session_id}/decide", {"csrf": token, "action": "delete"})[0] == 400


def test_full_seven_stage_flow_over_http(server: CopilotServer) -> None:
    session_id = new_session(server)
    for _ in range(7):
        token = token_for(server, session_id)
        assert call(server, "POST", f"/s/{session_id}/propose", {"csrf": token})[0] == 303
        _, _, page = call(server, "GET", f"/s/{session_id}")
        form = {"csrf": token, "action": "accept", **dict.fromkeys(ANSWER_RE.findall(page), "ASSUME")}
        assert call(server, "POST", f"/s/{session_id}/decide", form)[0] == 303
    _, _, page = call(server, "GET", f"/s/{session_id}")
    assert "All seven stages are complete" in page
    assert "Seven stages were completed" in call(server, "GET", f"/s/{session_id}/a/07_summary.md")[2]
    assert "Engineer decision" in call(server, "GET", f"/s/{session_id}/a/SUMMARY.md")[2]


def test_reject_and_modify_through_the_ui(server: CopilotServer) -> None:
    session_id = new_session(server)
    token = token_for(server, session_id)
    call(server, "POST", f"/s/{session_id}/propose", {"csrf": token})
    assert (
        call(server, "POST", f"/s/{session_id}/decide", {"csrf": token, "action": "reject", "text": ""})[
            2
        ].count("needs a reason")
        == 1
    )
    assert (
        call(
            server,
            "POST",
            f"/s/{session_id}/decide",
            {"csrf": token, "action": "reject", "text": "be specific"},
        )[0]
        == 303
    )
    call(server, "POST", f"/s/{session_id}/propose", {"csrf": token})
    _, _, page = call(server, "GET", f"/s/{session_id}")
    answers = dict.fromkeys(ANSWER_RE.findall(page), "ASSUME")
    assert (
        call(
            server,
            "POST",
            f"/s/{session_id}/decide",
            {"csrf": token, "action": "modify", "text": "My correction.", **answers},
        )[0]
        == 303
    )
    assert "My correction." in call(server, "GET", f"/s/{session_id}/a/01_requirements.md")[2]


def test_external_provider_requires_the_preview_step_in_the_ui(tmp_path: Path) -> None:
    provider = StubProvider(external=True)
    srv = make_server(tmp_path / "s", provider, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        session_id = new_session(srv, f"Build a service. key {FAKE_AWS}")
        token = token_for(srv, session_id)
        status, _, page = call(srv, "POST", f"/s/{session_id}/propose", {"csrf": token})
        assert status == 200 and "Send exactly this" in page and "Outbound preview" in page
        assert FAKE_AWS not in page and "[REDACTED:AWS_ACCESS_KEY:1]" in page
        assert provider.requests == []  # nothing sent yet
        assert call(srv, "POST", f"/s/{session_id}/propose", {"csrf": token, "confirm": "yes"})[0] == 303
        assert len(provider.requests) == 1 and FAKE_AWS not in provider.requests[0].user
        assert "External provider" in call(srv, "GET", "/")[2]
    finally:
        srv.shutdown()
        srv.server_close()


def test_provider_failure_is_shown_without_internals(tmp_path: Path) -> None:
    provider = StubProvider()
    provider.fail_next = True
    srv = make_server(tmp_path / "s", provider, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        session_id = new_session(srv)
        _, _, page = call(srv, "POST", f"/s/{session_id}/propose", {"csrf": token_for(srv, session_id)})
        assert "Provider failed: simulated provider outage" in page and "Traceback" not in page
    finally:
        srv.shutdown()
        srv.server_close()


def test_unknown_routes_and_methods(server: CopilotServer) -> None:
    assert call(server, "GET", "/new")[0] == 404
    assert call(server, "POST", "/", form={})[0] == 404
    assert call(server, "DELETE", "/")[0] in {404, 501}
