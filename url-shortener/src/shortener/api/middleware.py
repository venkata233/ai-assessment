"""Pure ASGI middleware: request id, security headers, structured access log."""

import re
import time
import uuid
from typing import Any

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_log = structlog.get_logger("access")
_DOCS_PATHS = ("/docs", "/openapi.json")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = Headers(scope=scope).get("x-request-id")
        request_id = supplied if supplied and _REQUEST_ID_RE.fullmatch(supplied) else uuid.uuid4().hex
        state: dict[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        is_docs = scope["path"].startswith(_DOCS_PATHS)
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Cache-Control", "no-store")
                if not is_docs:
                    headers.setdefault(
                        "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
                    )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            _log.info(
                "request",
                method=scope["method"],
                route=getattr(route, "path", "unmatched"),
                status=status_code,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                owner=state.get("owner"),
            )
