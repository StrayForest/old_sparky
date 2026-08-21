from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, status

from python_packages.platform_infra.config import PlatformSettings, get_settings


TURNSTILE_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_MODES = frozenset({"off", "always", "adaptive"})


def normalized_turnstile_mode(settings: PlatformSettings) -> str:
    mode = settings.platform_turnstile_mode.strip().lower()
    if mode not in TURNSTILE_MODES:
        raise RuntimeError("PLATFORM_TURNSTILE_MODE must be off, always, or adaptive.")
    return mode


def expected_turnstile_hostname(settings: PlatformSettings) -> str:
    configured = (settings.platform_turnstile_expected_hostname or "").strip().lower().rstrip(".")
    if configured:
        return configured
    parsed = urlsplit(settings.platform_web_origin)
    if not parsed.hostname:
        raise RuntimeError("PLATFORM_WEB_ORIGIN must contain a hostname for Turnstile validation.")
    return parsed.hostname.lower().rstrip(".")


def turnstile_is_required(settings: PlatformSettings, *, adaptive_required: bool) -> bool:
    mode = normalized_turnstile_mode(settings)
    return mode == "always" or (mode == "adaptive" and adaptive_required)


def validate_turnstile_settings(settings: PlatformSettings) -> None:
    mode = normalized_turnstile_mode(settings)
    if mode == "off":
        return
    if not (settings.platform_turnstile_site_key or "").strip():
        raise RuntimeError("PLATFORM_TURNSTILE_SITE_KEY is required when Turnstile is enabled.")
    if not (settings.platform_turnstile_secret_key or "").strip():
        raise RuntimeError("PLATFORM_TURNSTILE_SECRET_KEY is required when Turnstile is enabled.")
    expected_turnstile_hostname(settings)


async def verify_turnstile_token(
    token: str | None,
    *,
    expected_action: str,
    remote_ip: str | None,
    adaptive_required: bool = False,
    settings: PlatformSettings | None = None,
    client: httpx.AsyncClient | None = None,
) -> None:
    resolved_settings = settings or get_settings()
    mode = normalized_turnstile_mode(resolved_settings)
    if mode == "off":
        return
    required = turnstile_is_required(
        resolved_settings,
        adaptive_required=adaptive_required,
    )
    submitted_token = (token or "").strip()
    if not required and not submitted_token:
        return
    if not submitted_token or len(submitted_token) > 2048:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Human verification is required.",
        )

    secret = (resolved_settings.platform_turnstile_secret_key or "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Human verification is temporarily unavailable.",
        )
    form_data = {"secret": secret, "response": submitted_token}
    if remote_ip:
        form_data["remoteip"] = remote_ip

    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(resolved_settings.platform_turnstile_timeout_seconds)
    )
    try:
        response = await http_client.post(TURNSTILE_SITEVERIFY_URL, data=form_data)
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Human verification is temporarily unavailable.",
        ) from exc
    finally:
        if owns_client:
            await http_client.aclose()

    if not isinstance(result, dict):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Human verification is temporarily unavailable.",
        )
    response_hostname = str(result.get("hostname") or "").strip().lower().rstrip(".")
    response_action = str(result.get("action") or "").strip()
    if (
        result.get("success") is not True
        or response_hostname != expected_turnstile_hostname(resolved_settings)
        or response_action != expected_action
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Human verification failed.",
        )
