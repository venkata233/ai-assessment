"""HTTP routes. Thin: authenticate, rate limit, call a service, shape the response."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import APIKeyHeader

from shortener.api.container import Container
from shortener.api.schemas import AnalyticsResponse, CreateUrlRequest, Readiness, UrlResource
from shortener.domain.clicks import (
    country_from_header,
    referrer_origin,
    truncate_user_agent,
    ua_family,
    visitor_hash,
)
from shortener.domain.codes import CODE_PATTERN, is_reserved, is_valid_code_format
from shortener.domain.errors import LinkNotFound, Unauthorized
from shortener.domain.models import ClickRecord
from shortener.services.client_ip import client_ip

router = APIRouter()
metrics_router = APIRouter()
redirect_router = APIRouter()
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

CodePath = Annotated[str, Path(min_length=3, max_length=32, pattern=CODE_PATTERN.pattern)]


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def _client_ip(request: Request, container: Container) -> str | None:
    peer = request.client.host if request.client else None
    return client_ip(request.headers.get("x-forwarded-for"), peer, container.settings.trusted_proxy_hops)


async def _authenticate(
    request: Request, container: Container, api_key: str | None, limit: int, scope: str
) -> str:
    try:
        owner = container.authenticator.authenticate(api_key)
    except Unauthorized:
        # Throttle failed attempts per client address so keys cannot be guessed at wire speed.
        await container.rate_limiter.check("authfail", _client_ip(request, container) or "unknown", limit)
        raise
    request.scope.setdefault("state", {})["owner"] = owner
    await container.rate_limiter.check(scope, owner, limit)
    return owner


async def owner_for_create(
    request: Request, container: ContainerDep, api_key: Annotated[str | None, Depends(_api_key_header)]
) -> str:
    return await _authenticate(
        request, container, api_key, container.settings.rate_limit_create_per_min, "create"
    )


async def owner_for_api(
    request: Request, container: ContainerDep, api_key: Annotated[str | None, Depends(_api_key_header)]
) -> str:
    return await _authenticate(request, container, api_key, container.settings.rate_limit_api_per_min, "api")


CreateOwner = Annotated[str, Depends(owner_for_create)]
ApiOwner = Annotated[str, Depends(owner_for_api)]


# ----- management ---------------------------------------------------------------------------------


@router.post(
    "/api/v1/urls", status_code=201, response_model=UrlResource, tags=["Links"], operation_id="createUrl"
)
async def create_url(
    body: CreateUrlRequest, response: Response, owner: CreateOwner, container: ContainerDep
) -> UrlResource:
    link = await container.links.create(
        owner=owner, url=body.url, alias=body.alias, expires_at=body.parsed_expires_at()
    )
    response.headers["Location"] = f"/api/v1/urls/{link.code}"
    return UrlResource.from_link(link, base_url=container.settings.base_url, now=container.clock.now())


@router.get("/api/v1/urls/{code}", response_model=UrlResource, tags=["Links"], operation_id="getUrl")
async def get_url(code: CodePath, owner: ApiOwner, container: ContainerDep) -> UrlResource:
    link = await container.links.get(owner=owner, code=code)
    return UrlResource.from_link(link, base_url=container.settings.base_url, now=container.clock.now())


@router.delete("/api/v1/urls/{code}", status_code=204, tags=["Links"], operation_id="deleteUrl")
async def delete_url(code: CodePath, owner: ApiOwner, container: ContainerDep) -> Response:
    await container.links.delete(owner=owner, code=code)
    return Response(status_code=204)


@router.get(
    "/api/v1/urls/{code}/analytics",
    response_model=AnalyticsResponse,
    response_model_by_alias=True,
    tags=["Analytics"],
    operation_id="getUrlAnalytics",
)
async def get_analytics(
    code: CodePath,
    owner: ApiOwner,
    container: ContainerDep,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    granularity: Literal["hour", "day"] = "day",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> AnalyticsResponse:
    report = await container.analytics.report(
        owner=owner, code=code, start=start, end=end, granularity=granularity, limit=limit, cursor=cursor
    )
    return AnalyticsResponse.from_report(report)


# ----- operations ---------------------------------------------------------------------------------


@router.get("/health", tags=["Operations"], operation_id="liveness")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", response_model=Readiness, tags=["Operations"], operation_id="readiness")
async def readiness(container: ContainerDep) -> JSONResponse:
    database_up, redis_up = await container.readiness()
    if not database_up:
        status: Literal["ok", "degraded", "unavailable"] = "unavailable"
    elif not redis_up:
        status = "degraded"
    else:
        status = "ok"
    body = Readiness(
        status=status,
        checks={"database": "ok" if database_up else "down", "redis": "ok" if redis_up else "down"},
    )
    return JSONResponse(body.model_dump(), status_code=200 if database_up else 503)


@metrics_router.get("/metrics", include_in_schema=False)
async def metrics(container: ContainerDep) -> PlainTextResponse:
    gauges = await container.gauges() if container.gauges else {}
    return PlainTextResponse(container.metrics.render(gauges), media_type="text/plain; version=0.0.4")


# ----- redirect (included last in app.py: it matches any single path segment) ---------------------


@redirect_router.head("/{code}", include_in_schema=False, response_class=Response)
@redirect_router.get("/{code}", tags=["Redirect"], response_class=Response, operation_id="redirect")
async def redirect(
    code: str, request: Request, background: BackgroundTasks, container: ContainerDep
) -> Response:
    # Cheap rejections first: malformed or reserved names can never exist, so skip Redis and PostgreSQL.
    if not is_valid_code_format(code) or is_reserved(code):
        raise LinkNotFound
    ip = _client_ip(request, container)
    await container.rate_limiter.check(
        "redirect", ip or "unknown", container.settings.rate_limit_redirect_per_min
    )
    resolved = await container.links.resolve(code)

    response = Response(
        status_code=302,
        headers={
            "Location": resolved.target,
            "Cache-Control": "private, max-age=0",
            "Referrer-Policy": "no-referrer",
        },
    )
    if request.method == "GET":  # HEAD never counts as a click (ADR-001)
        settings = container.settings
        now = container.clock.now()
        user_agent = truncate_user_agent(request.headers.get("user-agent"))
        record = ClickRecord(
            url_id=resolved.url_id,
            ts_ms=int(now.timestamp() * 1000),
            referrer=referrer_origin(request.headers.get("referer")),
            user_agent=user_agent,
            ua_family=ua_family(user_agent),
            country=country_from_header(request.headers.get(settings.geo_country_header)),
            visitor_hash=visitor_hash(
                settings.analytics_secret.get_secret_value(), ip, user_agent, now.date()
            ),
        )
        # Runs after the response is sent; publish() never raises and is bounded by a short timeout.
        background.add_task(container.publisher.publish, record)
    return response
