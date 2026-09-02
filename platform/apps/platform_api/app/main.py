from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from apps.platform_api.app.api.router import api_router
from apps.platform_api.app.lifespan import platform_lifespan
from python_packages.platform_infra.config import get_settings, validate_platform_settings
from python_packages.platform_infra.csrf import CsrfProtectionMiddleware
from python_packages.platform_infra.authenticated_read_admission import (
    AuthenticatedReadAdmissionMiddleware,
)
from python_packages.platform_infra.performance import RequestPerformanceMiddleware
from python_packages.platform_infra.security import validate_auth_security_settings


logger = logging.getLogger(__name__)


async def handle_database_pool_timeout(
    request: Request,
    _exc: SQLAlchemyTimeoutError,
) -> JSONResponse:
    """Return a bounded transient response when the DB pool is exhausted."""

    logger.warning(
        "database_pool_timeout method=%s path=%s retry_after_seconds=1",
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "1"},
        content={
            "detail": "The platform is temporarily busy. Retry the request shortly."
        },
    )


def create_app() -> FastAPI:
    settings = get_settings()
    validate_platform_settings(settings)
    validate_auth_security_settings(settings)
    is_production = settings.platform_environment.strip().lower() == "production"
    app = FastAPI(
        title="Old Sparky Arena API",
        version="0.1.0",
        lifespan=platform_lifespan,
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )
    app.add_exception_handler(SQLAlchemyTimeoutError, handle_database_pool_timeout)
    app.add_middleware(CsrfProtectionMiddleware, settings_factory=get_settings)
    app.add_middleware(
        AuthenticatedReadAdmissionMiddleware,
        settings_factory=get_settings,
    )
    # Keep request performance outermost so admission-shed responses are
    # observable in the same request_perf stream as admitted requests.
    app.add_middleware(RequestPerformanceMiddleware)
    if not is_production:
        # Keep CORS outermost so browser clients can inspect CSRF rejections as
        # well as successful token-issuing responses.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.platform_web_origin],
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "Idempotency-Key",
                "X-CSRF-Token",
                "X-Platform-QA-Phase",
            ],
            expose_headers=[
                "X-CSRF-Token",
                "X-Total-Count",
                "X-Limit",
                "X-Offset",
                "X-Has-More",
                "X-Next-Cursor",
                "Retry-After",
            ],
        )
    app.include_router(api_router, prefix="/api/v1")

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, object]:
        return {
            "service": "deadlock-platform-api",
            "status": "ok",
            "api_prefix": "/api/v1",
        }

    return app


app = create_app()
