"""RFC 7807 / RFC 9457 problem responses and the exception handlers that produce them."""

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from shortener.domain.errors import (
    AliasConflict,
    CodeGenerationExhausted,
    DomainError,
    LinkExpired,
    LinkNotFound,
    RateLimited,
    StorageUnavailable,
    Unauthorized,
    ValidationFailed,
)

PROBLEM_BASE = "https://shortener.example/problems/"
_log = structlog.get_logger()


def request_id_of(request: Request) -> str:
    return str(request.scope.get("state", {}).get("request_id", ""))


def problem_response(
    request: Request,
    *,
    status: int,
    slug: str,
    title: str,
    detail: str | None = None,
    errors: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"type": f"{PROBLEM_BASE}{slug}", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    body["instance"] = request.url.path
    request_id = request_id_of(request)
    if request_id:
        body["request_id"] = request_id
    if errors:
        body["errors"] = errors
    response_headers = dict(headers or {})
    if request_id:
        response_headers["X-Request-ID"] = request_id
    return JSONResponse(
        body, status_code=status, media_type="application/problem+json", headers=response_headers
    )


async def _domain_error(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, ValidationFailed):
        return problem_response(
            request,
            status=422,
            slug="validation-error",
            title="Validation failed",
            detail="One or more fields are invalid.",
            errors=[{"field": i.field, "message": i.message, "code": i.code} for i in exc.issues],
        )
    if isinstance(exc, AliasConflict):
        return problem_response(
            request,
            status=409,
            slug="alias-conflict",
            title="Alias unavailable",
            detail="The alias is already in use.",
        )
    if isinstance(exc, LinkNotFound):
        return problem_response(request, status=404, slug="link-not-found", title="Link not found")
    if isinstance(exc, LinkExpired):
        return problem_response(
            request,
            status=410,
            slug="link-expired",
            title="Link expired",
            detail="This short link has expired.",
        )
    if isinstance(exc, Unauthorized):
        return problem_response(
            request,
            status=401,
            slug="unauthorized",
            title="Unauthorized",
            detail="A valid X-API-Key header is required.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if isinstance(exc, RateLimited):
        return problem_response(
            request,
            status=429,
            slug="rate-limited",
            title="Too many requests",
            detail="Rate limit exceeded. Retry later.",
            headers={"Retry-After": str(exc.retry_after)},
        )
    if isinstance(exc, StorageUnavailable | CodeGenerationExhausted):
        return problem_response(
            request,
            status=503,
            slug="service-unavailable",
            title="Service unavailable",
            headers={"Retry-After": "1"},
        )
    return await _unhandled(request, exc)


async def _request_validation_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        return await _unhandled(request, exc)
    errors: list[dict[str, str]] = []
    for item in exc.errors():
        loc = [str(part) for part in item["loc"] if not isinstance(part, int)]
        if loc and loc[0] in {"body", "query", "path", "header"}:
            loc = loc[1:]
        # Pydantic messages never echo the submitted value; `input` is deliberately not forwarded.
        errors.append(
            {"field": ".".join(loc) or "body", "message": str(item["msg"]), "code": str(item["type"])}
        )
    return problem_response(
        request,
        status=422,
        slug="validation-error",
        title="Validation failed",
        detail="One or more fields are invalid.",
        errors=errors,
    )


async def _http_exception(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        return await _unhandled(request, exc)
    titles = {404: "Not found", 405: "Method not allowed"}
    slug = {404: "not-found", 405: "method-not-allowed"}.get(exc.status_code, "http-error")
    return problem_response(
        request,
        status=exc.status_code,
        slug=slug,
        title=titles.get(exc.status_code, "Request failed"),
        headers=dict(exc.headers) if exc.headers else None,
    )


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    _log.error("unhandled_exception", error=type(exc).__name__, exc_info=exc)
    return problem_response(
        request,
        status=500,
        slug="internal-error",
        title="Internal server error",
        detail="An unexpected error occurred.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error)
    app.add_exception_handler(RequestValidationError, _request_validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(Exception, _unhandled)
