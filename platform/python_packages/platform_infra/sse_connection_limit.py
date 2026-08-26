from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Callable

from fastapi import Depends
from redis.asyncio import BlockingConnectionPool, Redis
from redis.exceptions import RedisError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from python_packages.platform_infra.config import (
    PlatformSettings,
    get_settings,
    is_load_test_source,
)
from python_packages.platform_infra.security import (
    get_optional_authenticated_session_for_stream,
)

logger = logging.getLogger(__name__)

SSE_PATH_RE = re.compile(r"^/api/v1/tournaments/[^/]+/bracket/events$")
# Cloudflare's public edge starts returning Error 1200 while the origin is
# still below its own CPU/DB ceilings. Keep a deliberate local headroom so
# excess viewers receive an immediate, controlled 429 and can fall back to
# revision polling instead of waiting in the edge queue.
SSE_GLOBAL_LIMIT = 3_000
SSE_SOURCE_LIMIT = 32
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
SSE_LOAD_TEST_BYPASS_HEADER = "x-platform-qa-sse-bypass"
SSE_LOAD_TEST_BYPASS_CONTEXT = b"platform-sse-load-test-v1"
SSE_LIMITER_REDIS_POOL_MAX_CONNECTIONS = 64
SSE_LIMITER_REDIS_POOL_TIMEOUT_SECONDS = 0.5
SSE_CONNECTION_LEASE_SCOPE = "platform.sse_connection_lease"

_limiter_redis_client: Redis | None = None

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


def sse_load_test_bypass_token(settings: PlatformSettings) -> str:
    return hmac.new(
        settings.platform_secret_key.encode("utf-8"),
        SSE_LOAD_TEST_BYPASS_CONTEXT,
        sha256,
    ).hexdigest()


def _has_sse_load_test_bypass(scope: Scope, settings: PlatformSettings) -> bool:
    request = Request(scope)
    supplied = request.headers.get(SSE_LOAD_TEST_BYPASS_HEADER, "")
    if not supplied:
        return False
    return hmac.compare_digest(supplied, sse_load_test_bypass_token(settings))


def _global_key() -> str:
    return f"{SSE_KEY_PREFIX}:global"


def _source_key(settings: PlatformSettings, source_address: str) -> str:
    return f"{SSE_KEY_PREFIX}:source:{_fingerprint(settings, f'source:{source_address}')}"


def _user_key(settings: PlatformSettings, user_id: str) -> str:
    return f"{SSE_KEY_PREFIX}:user:{_fingerprint(settings, f'user:{user_id}')}"


def _limiter_client() -> Redis:
    global _limiter_redis_client
    if _limiter_redis_client is None:
        settings = get_settings()
        pool = BlockingConnectionPool.from_url(
            settings.platform_redis_url,
            decode_responses=True,
            max_connections=SSE_LIMITER_REDIS_POOL_MAX_CONNECTIONS,
            timeout=SSE_LIMITER_REDIS_POOL_TIMEOUT_SECONDS,
        )
        _limiter_redis_client = Redis(connection_pool=pool)
    return _limiter_redis_client


async def dispose_sse_connection_limiter() -> None:
    global _limiter_redis_client
    client = _limiter_redis_client
    _limiter_redis_client = None
    if client is not None:
        await client.aclose()


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
    cache = _limiter_client()
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
    if rejected_index:
        raise SseConnectionLimitExceeded(scopes[rejected_index - 1])


async def _release_keys(keys: list[str], *, member: str) -> None:
    if not keys:
        return
    cache = _limiter_client()
    await cache.eval(_RELEASE_SCRIPT, len(keys), *keys, member)


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
    bypass_source_limit: bool = False,
    lease_seconds: int = SSE_LEASE_SECONDS,
    now_epoch: int | None = None,
) -> SseConnectionLease:
    resolved_settings = settings or get_settings()
    now = int(time.time()) if now_epoch is None else now_epoch
    member = secrets.token_urlsafe(18)
    keys = [_global_key()]
    limits = [global_limit]
    scopes = ["global"]
    if not bypass_source_limit and not is_load_test_source(
        resolved_settings, source_address
    ):
        keys.append(_source_key(resolved_settings, source_address))
        limits.append(source_limit)
        scopes.append("source")
    await _reserve_keys(
        keys,
        limits,
        scopes,
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
        bypass_source_limit = _has_sse_load_test_bypass(scope, settings)
        lease: SseConnectionLease | None = None
        try:
            lease = await reserve_sse_connection(
                source_address,
                settings=settings,
                bypass_source_limit=bypass_source_limit,
            )
        except SseConnectionLimitExceeded as exc:
            if lease is not None:
                await lease.release()
            await self._send_limit_response(scope, receive, send, exc, source_fingerprint)
            return
        except SseConnectionLimiterUnavailable:
            if lease is not None:
                await lease.release()
            await self._send_unavailable_response(scope, receive, send, source_fingerprint)
            return

        scope[SSE_CONNECTION_LEASE_SCOPE] = lease
        try:
            await self.app(scope, receive, send)
        except SseConnectionLimitExceeded as exc:
            await self._send_limit_response(scope, receive, send, exc, source_fingerprint)
        except SseConnectionLimiterUnavailable:
            await self._send_unavailable_response(scope, receive, send, source_fingerprint)
        finally:
            if lease is not None:
                await lease.release()
            scope.pop(SSE_CONNECTION_LEASE_SCOPE, None)

    @staticmethod
    async def _send_limit_response(
        scope: Scope,
        receive: Receive,
        send: Send,
        exc: SseConnectionLimitExceeded,
        source_fingerprint: str,
    ) -> None:
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

    @staticmethod
    async def _send_unavailable_response(
        scope: Scope,
        receive: Receive,
        send: Send,
        source_fingerprint: str,
    ) -> None:
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


async def admit_sse_authenticated_user(
    request: Request,
    auth_session=Depends(get_optional_authenticated_session_for_stream),
) -> None:
    """Attach the authenticated-user lease after the route auth check."""

    if auth_session is None:
        return
    lease = request.scope.get(SSE_CONNECTION_LEASE_SCOPE)
    if not isinstance(lease, SseConnectionLease):
        raise RuntimeError("SSE connection lease is missing from the request scope.")
    await add_sse_authenticated_user_scope(request, str(auth_session.user.id))


async def add_sse_authenticated_user_scope(request: Request, user_id: str) -> None:
    """Add the authenticated-user lease after stream authorization."""

    lease = request.scope.get(SSE_CONNECTION_LEASE_SCOPE)
    if not isinstance(lease, SseConnectionLease):
        raise RuntimeError("SSE connection lease is missing from the request scope.")
    await lease.add_user_scope(user_id)
