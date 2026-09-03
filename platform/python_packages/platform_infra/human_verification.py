from __future__ import annotations

import secrets
from hashlib import sha256

from fastapi import Request, Response

from python_packages.platform_infra.config import PlatformSettings
from python_packages.platform_infra.redis import redis_client


HUMAN_VERIFICATION_COOKIE_SUFFIX = "_human_verification"
HUMAN_VERIFICATION_KEY_PREFIX = "platform:auth-human-verification:v1:"
HUMAN_VERIFICATION_TOKEN_MIN_LENGTH = 32


def human_verification_cookie_name(settings: PlatformSettings) -> str:
    return f"{settings.platform_session_cookie_name}{HUMAN_VERIFICATION_COOKIE_SUFFIX}"


def _redis_key(token: str) -> str:
    return f"{HUMAN_VERIFICATION_KEY_PREFIX}{sha256(token.encode('utf-8')).hexdigest()}"


async def has_human_verification_trust(
    request: Request,
    *,
    settings: PlatformSettings,
) -> bool:
    token = request.cookies.get(human_verification_cookie_name(settings), "").strip()
    if len(token) < HUMAN_VERIFICATION_TOKEN_MIN_LENGTH:
        return False

    cache = redis_client(shared=True)
    # A present trust cookie must fail closed if its server-side record cannot
    # be checked. Requests without the cookie do not touch Redis.
    return bool(await cache.get(_redis_key(token)))


async def issue_human_verification_trust(
    response: Response,
    *,
    settings: PlatformSettings,
) -> None:
    token = secrets.token_urlsafe(32)
    cache = redis_client(shared=True)
    await cache.set(
        _redis_key(token),
        "1",
        ex=settings.platform_auth_human_verification_ttl_seconds,
    )
    response.set_cookie(
        key=human_verification_cookie_name(settings),
        value=token,
        httponly=True,
        secure=settings.platform_cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings.platform_auth_human_verification_ttl_seconds,
    )
    response.headers["Cache-Control"] = "no-store"
