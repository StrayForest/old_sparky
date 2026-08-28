from __future__ import annotations

import secrets
from collections.abc import Callable
import hmac
from hashlib import sha256
from hmac import compare_digest
from urllib.parse import urlsplit

from fastapi import Request, Response
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from python_packages.platform_infra.config import PlatformSettings, get_settings


UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_EXEMPT_PATHS = frozenset({"/api/v1/security/csp-report"})
PUBLIC_AUTH_PATHS = frozenset(
    {
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/password-reset/request",
        "/api/v1/auth/password-reset/verify-code",
        "/api/v1/auth/password-reset/confirm",
        "/api/v1/auth/email-verification/confirm",
        "/api/v1/auth/email-verification/resend",
        "/api/v1/auth/steam/login/start",
    }
)


class CsrfValidationError(ValueError):
    pass


def csrf_protection_enabled(settings: PlatformSettings) -> bool:
    if settings.platform_environment.strip().lower() == "production":
        return True
    return bool(settings.platform_csrf_enabled)


def csrf_cookie_name(settings: PlatformSettings) -> str:
    return f"{settings.platform_session_cookie_name}_csrf"


def _csrf_signature(settings: PlatformSettings, session_token: str, nonce: str) -> str:
    session_digest = sha256(session_token.encode("utf-8")).hexdigest()
    message = f"{session_digest}:{nonce}".encode("utf-8")
    return hmac.new(
        settings.platform_secret_key.encode("utf-8"),
        message,
        sha256,
    ).hexdigest()


def _normalized_origin(value: str) -> str:
    candidate = value.strip()
    if not candidate or candidate == "null" or "," in candidate:
        raise CsrfValidationError("Request origin is not allowed.")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CsrfValidationError("Request origin is not allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise CsrfValidationError("Request origin is not allowed.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CsrfValidationError("Request origin is not allowed.") from exc
    default_port = 443 if parsed.scheme == "https" else 80
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme}://{parsed.hostname.lower()}{port_suffix}"


def validate_request_origin(request: Request, settings: PlatformSettings) -> None:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        normalized_fetch_site = fetch_site.strip().lower()
        if normalized_fetch_site not in {"same-origin", "same-site", "none"}:
            raise CsrfValidationError("Cross-site request rejected.")

    expected_origin = _normalized_origin(settings.platform_web_origin)
    supplied_origin = request.headers.get("origin")
    if supplied_origin:
        request_origin = _normalized_origin(supplied_origin)
    else:
        supplied_referer = request.headers.get("referer")
        if not supplied_referer:
            raise CsrfValidationError("Request origin is required.")
        request_origin = _normalized_origin(supplied_referer)
    if request_origin != expected_origin:
        raise CsrfValidationError("Cross-site request rejected.")


def validate_csrf_request(request: Request, settings: PlatformSettings) -> None:
    validate_request_origin(request, settings)
    session_token = request.cookies.get(settings.platform_session_cookie_name, "")
    cookie_token = request.cookies.get(csrf_cookie_name(settings), "")
    header_token = request.headers.get(CSRF_HEADER_NAME, "")
    if len(cookie_token) < 32 or len(header_token) < 32:
        raise CsrfValidationError("CSRF token is missing or invalid.")
    if not compare_digest(cookie_token, header_token):
        raise CsrfValidationError("CSRF token is missing or invalid.")
    try:
        nonce, supplied_signature = cookie_token.split(".", 1)
    except ValueError as exc:
        raise CsrfValidationError("CSRF token is missing or invalid.") from exc
    if len(nonce) < 32 or len(supplied_signature) != 64 or not session_token:
        raise CsrfValidationError("CSRF token is missing or invalid.")
    expected_signature = _csrf_signature(settings, session_token, nonce)
    if not compare_digest(supplied_signature, expected_signature):
        raise CsrfValidationError("CSRF token is missing or invalid.")


def issue_csrf_token(
    response: Response,
    session_token: str,
    settings: PlatformSettings | None = None,
) -> str:
    resolved_settings = settings or get_settings()
    token = generate_csrf_token(session_token, resolved_settings)
    response.set_cookie(
        key=csrf_cookie_name(resolved_settings),
        value=token,
        httponly=False,
        secure=resolved_settings.platform_cookie_secure,
        samesite="lax",
        path="/",
        max_age=resolved_settings.platform_session_ttl_days * 24 * 60 * 60,
    )
    response.headers[CSRF_HEADER_NAME] = token
    response.headers["Cache-Control"] = "no-store"
    return token


def generate_csrf_token(
    session_token: str,
    settings: PlatformSettings | None = None,
) -> str:
    """Create a session-bound token without requiring an HTTP response."""

    resolved_settings = settings or get_settings()
    nonce = secrets.token_urlsafe(32)
    return f"{nonce}.{_csrf_signature(resolved_settings, session_token, nonce)}"


def clear_csrf_cookie(response: Response, settings: PlatformSettings | None = None) -> None:
    resolved_settings = settings or get_settings()
    response.delete_cookie(
        key=csrf_cookie_name(resolved_settings),
        path="/",
        secure=resolved_settings.platform_cookie_secure,
        httponly=False,
        samesite="lax",
    )


class CsrfProtectionMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        settings_factory: Callable[[], PlatformSettings] = get_settings,
    ) -> None:
        self.app = app
        self.settings_factory = settings_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method", "GET").upper() not in UNSAFE_METHODS
            or scope.get("path") in CSRF_EXEMPT_PATHS
        ):
            await self.app(scope, receive, send)
            return

        settings = self.settings_factory()
        if not csrf_protection_enabled(settings):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        is_public_auth_path = str(scope.get("path") or "") in PUBLIC_AUTH_PATHS
        has_session_cookie = bool(
            request.cookies.get(settings.platform_session_cookie_name)
        )
        if not is_public_auth_path and not has_session_cookie:
            await self.app(scope, receive, send)
            return

        try:
            if is_public_auth_path:
                validate_request_origin(request, settings)
            else:
                validate_csrf_request(request, settings)
        except CsrfValidationError as exc:
            response = JSONResponse(
                status_code=403,
                content={"detail": str(exc)},
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
