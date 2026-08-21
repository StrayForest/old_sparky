from __future__ import annotations

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from apps.platform_api.app.api.router import api_router
from apps.platform_api.app.lifespan import platform_lifespan
from python_packages.platform_infra.config import get_settings, validate_platform_settings
from python_packages.platform_infra.csrf import CsrfProtectionMiddleware
from python_packages.platform_infra.performance import RequestPerformanceMiddleware
from python_packages.platform_infra.object_storage import get_object_storage, object_key_from_upload_url
from python_packages.platform_infra.security import validate_auth_security_settings
from python_packages.platform_infra.sse_connection_limit import SseConnectionLimitMiddleware
from starlette.concurrency import run_in_threadpool


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
    app.add_middleware(RequestPerformanceMiddleware)
    app.add_middleware(CsrfProtectionMiddleware, settings_factory=get_settings)
    app.add_middleware(SseConnectionLimitMiddleware, settings_factory=get_settings)
    if not is_production:
        # Keep CORS outermost so browser clients can inspect CSRF rejections as
        # well as successful token-issuing responses.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.platform_web_origin],
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-CSRF-Token", "X-Platform-QA-Phase"],
            expose_headers=[
                "X-CSRF-Token",
                "X-Total-Count",
                "X-Limit",
                "X-Offset",
                "X-Has-More",
                "Retry-After",
            ],
        )
    app.include_router(api_router, prefix="/api/v1")
    if settings.platform_object_storage_backend.strip().lower() == "r2":
        @app.get("/api/v1/uploads/{key:path}", include_in_schema=False)
        async def get_uploaded_object(key: str) -> Response:
            normalized_key = object_key_from_upload_url(f"/api/v1/uploads/{key}")
            if normalized_key is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            stored = await run_in_threadpool(get_object_storage().get, normalized_key)
            if stored is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            return Response(
                content=stored.content,
                media_type=stored.content_type,
                headers={"Cache-Control": "public, max-age=31536000, immutable"},
            )
    else:
        settings.platform_upload_dir.mkdir(parents=True, exist_ok=True)
        app.mount(
            "/api/v1/uploads",
            StaticFiles(directory=settings.platform_upload_dir),
            name="platform_uploads",
        )

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, object]:
        return {
            "service": "deadlock-platform-api",
            "status": "ok",
            "api_prefix": "/api/v1",
        }

    return app


app = create_app()
