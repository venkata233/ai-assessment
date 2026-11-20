"""FastAPI application factory."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from shortener.api.container import Container
from shortener.api.middleware import RequestContextMiddleware
from shortener.api.problems import register_exception_handlers
from shortener.api.routes import metrics_router, redirect_router, router

Cleanup = Callable[[], Awaitable[None]]


def create_app(container: Container, cleanup: Cleanup | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if cleanup is not None:
                await cleanup()

    lifespan_ctx: Callable[[FastAPI], AbstractAsyncContextManager[None]] = lifespan
    settings = container.settings
    app = FastAPI(
        title="URL Shortener API",
        version="1.0.0",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan_ctx,
    )
    app.state.container = container
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(router)
    if settings.metrics_enabled:
        app.include_router(metrics_router)
    app.include_router(redirect_router)  # last: `/{code}` matches any single path segment
    return app
