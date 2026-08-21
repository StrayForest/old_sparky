from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Callable

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from python_packages.platform_infra.auth_lifecycle import email_verification_required
from python_packages.platform_infra.config import PlatformSettings, get_settings
from python_packages.platform_infra.db import session_factory
from python_packages.platform_infra.models import User, UserSession
from python_packages.platform_infra.redis import redis_client
from python_packages.platform_infra.security import session_token_digest

logger = logging.getLogger(__name__)

SSE_PATH_RE = re.compile(r"^/api/v1/tournaments/[^/]+/bracket/events$")
SSE_GLOBAL_LIMIT = 128
SSE_SOURCE_LIMIT = 6
SSE_USER_LIMIT = 4
SSE_STREAM_MAX_LIFETIME_SECONDS = 600
SSE_KEEPALIVE_SECONDS = 15
SSE_RECONNECT_MIN_MS = 5_000
SSE_RECONNECT_JITTER_MS = 7_000
SSE_RETRY_AFTER_SECONDS = 15
SSE_LEASE_GRACE_SECONDS = 60
SSE_KEY_EXPIRY_GRACE_SECONDS = 60
SSE_LEASE_SECONDS = SSE_STREAM_MAX_LIFETIME_SECONDS + SSE_LEASE_GRACE_SECONDS
SSE_KEY_PREFIX = "platform:sse-limit:v1"

_ACQUIRE_SCRIPT = """
local now = tonumber(ARGV[1])
local lease_expires_at = tonumber(ARGV[2])
local key_expires_at = tonumber(ARGV[3])
local member = ARGV[4]

for i = 1, #KEYS do
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now)
  local count = redis.call('ZCARD', KEYS[i])
  local limit = tonumber(ARGV[4 + i])
  if count >= limit then
    return i
  end
end

for i = 1, #KEYS do
  redis.call('ZADD', KEYS[i], lease_expires_at, member)
  redis.call('EXPIREAT', KEYS[i], key_expires_at)
end

return 0
"""

_RELEASE_SCRIPT = """
for i = 1, #KEYS do
  redis.call('ZREM', KEYS[i], ARGV[1])
  if redis.call('ZCARD', KEYS[i]) == 0 then
    redis.call('DEL', KEYS[i])
  end
end
return 0
"""


class SseConnectionLimitError(RuntimeError):
    pass


class SseConnectionLimitExceeded(SseConnectionLimitError):
    def __init__(self, scope: str) -> None:
        super().__init__(f"SSE connection limit exceeded for {scope}.")
        self.scope = scope


class SseConnectionLimiterUnavailable(SseConnectionLimitError):
    pass


def _fingerprint(settings: PlatformSettings, value: str) -> str:
    return hmac.new(
        settings.platform_secret_key.encode("utf-8"),
        value.strip().lower().encode("utf-8"),
        sha256,
    ).hexdigest()[:32]


def _global_key() -> str:
    return f"{SSE_KEY_PREFIX}:global"


def _source_key(settings: PlatformSettings, source_address: str) -> str:
    return f"{SSE_KEY_PREFIX}:source:{_fingerprint(settings, f'source:{source_address}')}"


def _user_key(settings: PlatformSettings, user_id: str) -> str:
    return f"{SSE_KEY_PREFIX}:user:{_fingerprint(settings, f'user:{user_id}')}"


async def _reserve_keys(
    keys: list[str],
    limits: list[int],
    scopes: list[str],
    *,
    member: str,
    lease_seconds: int,
    now_epoch: int,
) -> None:
    if not keys or len(keys) != len(limits) or len(keys) != len(scopes):
        raise ValueError("SSE reservation keys, limits and scopes must align.")
    if lease_seconds < 1 or any(limit < 1 for limit in limits):
        raise ValueError("SSE reservation limits and lease duration must be positive.")

    lease_expires_at = now_epoch + lease_seconds
    key_expires_at = lease_expires_at + SSE_KEY_EXPIRY_GRACE_SECONDS
    cache = redis_client()
    try:
        rejected_index = int(
            await cache.eval(
                _ACQUIRE_SCRIPT,
                len(keys),
                *keys,
                now_epoch,
                lease_expires_at,
                key_expires_at,
                member,
                *limits,
            )
        )
    except RedisError as exc:
        raise SseConnectionLimiterUnavailable(
            "SSE connection protection is temporarily unavailable."
        ) from exc
    finally:
        await cache.aclose()

    if rejected_index:
        raise SseConnectionLimitExceeded(scopes[rejected_index - 1])


async def _release_keys(keys: list[str], *, member: str) -> None:
    if not keys:
        return
    cache = redis_client()
    try:
        await cache.eval(_RELEASE_SCRIPT, len(keys), *keys, member)
    finally:
        await cache.aclose()


@dataclass(slots=True)
class SseConnectionLease:
    member: str
    settings: PlatformSettings
    keys: list[str] = field(default_factory=list)
    released: bool = False

    async def add_user_scope(
        self,
        user_id: str,
        *,
        user_limit: int = SSE_USER_LIMIT,
        lease_seconds: int = SSE_LEASE_SECONDS,
        now_epoch: int | None = None,
    ) -> None:
        if self.released:
            raise RuntimeError("Cannot extend a released SSE connection lease.")
        key = _user_key(self.settings, user_id)
        if key in self.keys:
            return
        now = int(time.time()) if now_epoch is None else now_epoch
        await _reserve_keys(
            [key],
            [user_limit],
            ["user"],
            member=self.member,
            lease_seconds=lease_seconds,
            now_epoch=now,
        )
        self.keys.append(key)

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            await _release_keys(self.keys, member=self.member)
        except RedisError:
            logger.warning(
                "Failed to release SSE connection lease; expiry will reclaim it.",
                exc_info=True,
            )


async def reserve_sse_connection(
    source_address: str,
    *,
    settings: PlatformSettings | None = None,
    global_limit: int = SSE_GLOBAL_LIMIT,
    source_limit: int = SSE_SOURCE_LIMIT,
    lease_seconds: int = SSE_LEASE_SECONDS,
    now_epoch: int | None = None,
) -> SseConnectionLease:
    resolved_settings = settings or get_settings()
    now = int(time.time()) if now_epoch is None else now_epoch
    member = secrets.token_urlsafe(18)
    keys = [
        _global_key(),
        _source_key(resolved_settings, source_address),
    ]
    await _reserve_keys(
        keys,
        [global_limit, source_limit],
        ["global", "source"],
        member=member,
        lease_seconds=lease_seconds,
        now_epoch=now,
    )
    return SseConnectionLease(
        member=member,
        settings=resolved_settings,
        keys=keys,
    )


def _source_address(scope: Scope) -> str:
    client = scope.get("client")
    if client is None or not client[0]:
        return "unknown"
    return str(client[0])


async def _authenticated_user_id(
    scope: Scope,
    settings: PlatformSettings,
) -> str | None:
    request = Request(scope)
    token = request.cookies.get(settings.platform_session_cookie_name)
    if not token:
        return None

    now = datetime.now(UTC)
    predicates = [User.status == "active"]
    if email_verification_required(settings):
        predicates.append(
            (User.email.is_(None)) | (User.email_verified_at.is_not(None))
        )

    try:
        async with session_factory()() as db_session:
            user_id = await db_session.scalar(
                select(UserSession.user_id)
                .join(User, User.id == UserSession.user_id)
                .where(
                    UserSession.token_digest == session_token_digest(token),
                    UserSession.invalidated_at.is_(None),
                    UserSession.expires_at > now,
                    *predicates,
                )
            )
    except SQLAlchemyError:
        logger.warning(
            "Could not resolve authenticated SSE user for connection limiting.",
            exc_info=True,
        )
        return None

    return str(user_id) if user_id is not None else None


class SseConnectionLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        settings_factory: Callable[[], PlatformSettings] = get_settings,
    ) -> None:
        self.app = app
        self.settings_factory = settings_factory

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "GET"
            or not SSE_PATH_RE.fullmatch(str(scope.get("path", "")))
        ):
            await self.app(scope, receive, send)
            return

        settings = self.settings_factory()
        source_address = _source_address(scope)
        source_fingerprint = _fingerprint(settings, f"source:{source_address}")
        lease: SseConnectionLease | None = None
        try:
            lease = await reserve_sse_connection(
                source_address,
                settings=settings,
            )
            user_id = await _authenticated_user_id(scope, settings)
            if user_id is not None:
                await lease.add_user_scope(user_id)
        except SseConnectionLimitExceeded as exc:
            if lease is not None:
                await lease.release()
            logger.warning(
                "Rejected SSE connection: scope=%s source=%s",
                exc.scope,
                source_fingerprint,
            )
            response = JSONResponse(
                {"detail": "Too many live-update connections. Try again shortly."},
                status_code=429,
                headers={
                    "Retry-After": str(SSE_RETRY_AFTER_SECONDS),
                    "Cache-Control": "no-store",
                },
            )
            await response(scope, receive, send)
            return
        except SseConnectionLimiterUnavailable:
            if lease is not None:
                await lease.release()
            logger.error(
                "Rejected SSE connection because the limiter backend is unavailable: source=%s",
                source_fingerprint,
            )
            response = JSONResponse(
                {"detail": "Live-update connection protection is temporarily unavailable."},
                status_code=503,
                headers={
                    "Retry-After": str(SSE_RETRY_AFTER_SECONDS),
                    "Cache-Control": "no-store",
                },
            )
            await response(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            if lease is not None:
                await lease.release()
