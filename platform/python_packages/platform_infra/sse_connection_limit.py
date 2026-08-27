from __future__ import annotations

import asyncio
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Callable, Literal

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

SSE_PATH_RE = re.compile(
    r"^(?:/api/v1/tournaments/[^/]+/bracket/events|/api/v1/ready-check/events)$"
)
# Cloudflare's public edge starts returning Error 1200 while the origin is
# still below its own CPU/DB ceilings. Keep a deliberate local headroom so
# excess viewers receive an immediate, controlled 429 and can fall back to
# revision polling instead of waiting in the edge queue.
SSE_GLOBAL_LIMIT = 3_000
READY_CHECK_SSE_GLOBAL_LIMIT = 3_000
READY_CHECK_SSE_HARD_TARGET = 10_000
SSE_QA_GLOBAL_LIMIT_MAX = 30_000
SSE_SOURCE_LIMIT = 32
SSE_USER_LIMIT = 4
READY_CHECK_SSE_USER_LIMIT = 1
SSE_KEEPALIVE_SECONDS = 15
SSE_RECONNECT_MIN_MS = 5_000
SSE_RECONNECT_JITTER_MS = 7_000
SSE_RETRY_AFTER_SECONDS = 15
SSE_LEASE_SECONDS = 120
SSE_LEASE_RENEW_INTERVAL_SECONDS = 30
SSE_KEY_EXPIRY_GRACE_SECONDS = 60
SSE_KEY_PREFIX = "platform:sse-limit:v1"
READY_CHECK_SSE_KEY_PREFIX = "platform:ready-check-sse-limit:v1"
SseUserScope = Literal["legacy", "ready_check"]
SSE_LOAD_TEST_BYPASS_HEADER = "x-platform-qa-sse-bypass"
SSE_LOAD_TEST_BYPASS_CONTEXT = b"platform-sse-load-test-v1"
SSE_LOAD_TEST_CAPACITY_HEADER = "x-platform-qa-sse-capacity"
SSE_LOAD_TEST_CAPACITY_CONTEXT = b"platform-sse-capacity-v1"
# A paced 30,000-stream diagnostic at 25 opens/sec takes about 20 minutes.
# Keep the proof short-lived while leaving enough time for the deliberately
# bounded QA window to finish opening; it is never used by browser clients.
SSE_LOAD_TEST_CAPACITY_TTL_SECONDS = 1_800
# A burst of signed-ticket opens still performs one atomic global lease in
# Redis. Keep this pool large enough to absorb the bounded 3,000-connection
# application ceiling without turning pool checkout into a false 503, while
# retaining a finite wait so Redis failure remains fail-closed.
SSE_LIMITER_REDIS_POOL_MAX_CONNECTIONS = 512
SSE_LIMITER_REDIS_POOL_TIMEOUT_SECONDS = 2.0
# Stream teardown can happen in a single cancellation wave. Bound only the
# best-effort release commands so that teardown cannot exhaust the Redis pool
# needed by admission and renewal. A missed release remains safe: the lease
# expires and is pruned by the next reservation.
SSE_RELEASE_CONCURRENCY = 128
SSE_CONNECTION_LEASE_SCOPE = "platform.sse_connection_lease"

_limiter_redis_client: Redis | None = None
_sse_release_semaphore: asyncio.Semaphore | None = None

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

_RENEW_SCRIPT = """
local lease_expires_at = tonumber(ARGV[2])
local key_expires_at = tonumber(ARGV[3])
local member = ARGV[1]
local renewed = 0

for i = 1, #KEYS do
  if redis.call('ZSCORE', KEYS[i], member) then
    redis.call('ZADD', KEYS[i], lease_expires_at, member)
    redis.call('EXPIREAT', KEYS[i], key_expires_at)
    renewed = renewed + 1
  end
end

return renewed
"""


class SseConnectionLimitError(RuntimeError):
    pass


class SseConnectionLimitExceeded(SseConnectionLimitError):
    def __init__(self, scope: str) -> None:
        super().__init__(f"SSE connection limit exceeded for {scope}.")
        self.scope = scope


class SseConnectionLimiterUnavailable(SseConnectionLimitError):
    pass


class SseConnectionLeaseRenewalFailed(SseConnectionLimitError):
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


def _capacity_signature(settings: PlatformSettings, payload: str) -> str:
    return hmac.new(
        settings.platform_secret_key.encode("utf-8"),
        b".".join((SSE_LOAD_TEST_CAPACITY_CONTEXT, payload.encode("ascii"))),
        sha256,
    ).hexdigest()


def sse_load_test_capacity_token(
    settings: PlatformSettings,
    global_limit: int,
    *,
    now_epoch: int | None = None,
) -> str:
    """Issue a short-lived signed global-cap proof for an approved QA run."""

    if not 1 <= global_limit <= SSE_QA_GLOBAL_LIMIT_MAX:
        raise ValueError(
            f"QA SSE global limit must be between 1 and {SSE_QA_GLOBAL_LIMIT_MAX}."
        )
    now = int(time.time()) if now_epoch is None else now_epoch
    payload = f"{global_limit}:{now + SSE_LOAD_TEST_CAPACITY_TTL_SECONDS}"
    return f"{payload}:{_capacity_signature(settings, payload)}"


def _has_sse_load_test_bypass(scope: Scope, settings: PlatformSettings) -> bool:
    request = Request(scope)
    supplied = request.headers.get(SSE_LOAD_TEST_BYPASS_HEADER, "")
    if not supplied:
        return False
    return hmac.compare_digest(supplied, sse_load_test_bypass_token(settings))


def _qa_global_limit(scope: Scope, settings: PlatformSettings) -> int | None:
    # Capacity proofs are deliberately scoped to the Ready Check contour. A
    # valid proof must not be reusable to raise the compatibility bracket
    # contour, even if a caller supplies the header outside the QA wrapper.
    if str(scope.get("path", "")) not in {
        "/api/v1/ready-check/agenda",
        "/api/v1/ready-check/events",
    }:
        return None
    request = Request(scope)
    parts = request.headers.get(SSE_LOAD_TEST_CAPACITY_HEADER, "").split(":")
    if len(parts) != 3:
        return None
    raw_limit, raw_expires_at, supplied_signature = parts
    if not raw_limit.isdecimal() or not raw_expires_at.isdecimal():
        return None
    global_limit = int(raw_limit)
    expires_at = int(raw_expires_at)
    if not 1 <= global_limit <= SSE_QA_GLOBAL_LIMIT_MAX:
        return None
    now = int(time.time())
    if expires_at <= now or expires_at > now + SSE_LOAD_TEST_CAPACITY_TTL_SECONDS:
        return None
    payload = f"{global_limit}:{expires_at}"
    if not hmac.compare_digest(supplied_signature, _capacity_signature(settings, payload)):
        return None
    return global_limit


def qa_sse_capacity_limit(request: Request) -> int | None:
    """Return an operator-signed QA capacity override for this request."""

    return _qa_global_limit(request.scope, get_settings())


def _global_key() -> str:
    return f"{SSE_KEY_PREFIX}:global"


def _ready_check_global_key() -> str:
    return f"{READY_CHECK_SSE_KEY_PREFIX}:global"


def _source_key(settings: PlatformSettings, source_address: str) -> str:
    return f"{SSE_KEY_PREFIX}:source:{_fingerprint(settings, f'source:{source_address}')}"


def _user_key(settings: PlatformSettings, user_id: str) -> str:
    return f"{SSE_KEY_PREFIX}:user:{_fingerprint(settings, f'user:{user_id}')}"


def _ready_check_user_key(settings: PlatformSettings, user_id: str) -> str:
    return f"{READY_CHECK_SSE_KEY_PREFIX}:user:{_fingerprint(settings, f'user:{user_id}')}"


def _user_key_for_scope(
    settings: PlatformSettings,
    user_id: str,
    user_scope: SseUserScope,
) -> str:
    if user_scope == "legacy":
        return _user_key(settings, user_id)
    if user_scope == "ready_check":
        return _ready_check_user_key(settings, user_id)
    raise ValueError(f"Unsupported SSE user scope: {user_scope}")


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
    global _limiter_redis_client, _sse_release_semaphore
    client = _limiter_redis_client
    _limiter_redis_client = None
    _sse_release_semaphore = None
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
    global _sse_release_semaphore
    if _sse_release_semaphore is None:
        _sse_release_semaphore = asyncio.Semaphore(SSE_RELEASE_CONCURRENCY)
    async with _sse_release_semaphore:
        cache = _limiter_client()
        await cache.eval(_RELEASE_SCRIPT, len(keys), *keys, member)


async def _renew_keys(
    keys: list[str],
    *,
    member: str,
    lease_seconds: int,
    now_epoch: int,
) -> None:
    if not keys:
        return
    lease_expires_at = now_epoch + lease_seconds
    key_expires_at = lease_expires_at + SSE_KEY_EXPIRY_GRACE_SECONDS
    cache = _limiter_client()
    try:
        renewed = int(
            await cache.eval(
                _RENEW_SCRIPT,
                len(keys),
                *keys,
                member,
                lease_expires_at,
                key_expires_at,
            )
        )
    except RedisError as exc:
        raise SseConnectionLeaseRenewalFailed(
            "SSE connection protection renewal is temporarily unavailable."
        ) from exc
    if renewed != len(keys):
        raise SseConnectionLeaseRenewalFailed(
            "SSE connection protection lease is no longer active."
        )


@dataclass(slots=True)
class SseConnectionLease:
    member: str
    settings: PlatformSettings
    keys: list[str] = field(default_factory=list)
    lease_seconds: int = SSE_LEASE_SECONDS
    last_renewed_epoch: int = 0
    released: bool = False

    async def add_user_scope(
        self,
        user_id: str,
        *,
        user_limit: int = SSE_USER_LIMIT,
        user_scope: SseUserScope = "legacy",
        lease_seconds: int | None = None,
        now_epoch: int | None = None,
    ) -> None:
        if self.released:
            raise RuntimeError("Cannot extend a released SSE connection lease.")
        key = _user_key_for_scope(self.settings, user_id, user_scope)
        if key in self.keys:
            return
        now = int(time.time()) if now_epoch is None else now_epoch
        await _reserve_keys(
            [key],
            [user_limit],
            ["user"],
            member=self.member,
            lease_seconds=self.lease_seconds if lease_seconds is None else lease_seconds,
            now_epoch=now,
        )
        self.keys.append(key)

    async def renew(self, *, now_epoch: int | None = None) -> None:
        if self.released or not self.keys:
            return
        now = int(time.time()) if now_epoch is None else now_epoch
        if now - self.last_renewed_epoch < SSE_LEASE_RENEW_INTERVAL_SECONDS:
            return
        await _renew_keys(
            self.keys,
            member=self.member,
            lease_seconds=self.lease_seconds,
            now_epoch=now,
        )
        self.last_renewed_epoch = now

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
    global_key: str | None = None,
) -> SseConnectionLease:
    resolved_settings = settings or get_settings()
    now = int(time.time()) if now_epoch is None else now_epoch
    member = secrets.token_urlsafe(18)
    keys = [global_key or _global_key()]
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
        lease_seconds=lease_seconds,
        last_renewed_epoch=now,
    )


async def current_ready_check_sse_connection_count(
    *,
    now_epoch: int | None = None,
) -> int:
    """Return the bounded Ready Check pool count for admission planning."""

    now = int(time.time()) if now_epoch is None else now_epoch
    cache = _limiter_client()
    try:
        await cache.zremrangebyscore(_ready_check_global_key(), "-inf", now)
        return int(await cache.zcard(_ready_check_global_key()))
    except RedisError:
        # Planning is advisory. The atomic lease remains fail-closed when the
        # actual stream attempts to open, so a stale/unknown count cannot grant
        # capacity.
        logger.warning("Failed to read Ready Check SSE occupancy for planning.", exc_info=True)
        return 0


def _source_address(scope: Scope) -> str:
    client = scope.get("client")
    if client is None or not client[0]:
        return "unknown"
    return str(client[0])


def _global_limit_for_path(scope: Scope, settings: PlatformSettings) -> int:
    path = str(scope.get("path", ""))
    if path == "/api/v1/ready-check/events":
        return READY_CHECK_SSE_GLOBAL_LIMIT
    return SSE_GLOBAL_LIMIT


def _global_key_for_path(scope: Scope) -> str:
    if str(scope.get("path", "")) == "/api/v1/ready-check/events":
        return _ready_check_global_key()
    return _global_key()


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
        global_limit = _qa_global_limit(scope, settings) or _global_limit_for_path(scope, settings)
        lease: SseConnectionLease | None = None
        try:
            reservation_kwargs = {
                "settings": settings,
                "global_limit": global_limit,
                "bypass_source_limit": bypass_source_limit,
            }
            if str(scope.get("path", "")) == "/api/v1/ready-check/events":
                reservation_kwargs["global_key"] = _global_key_for_path(scope)
            lease = await reserve_sse_connection(source_address, **reservation_kwargs)
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


async def add_sse_authenticated_user_scope(
    request: Request,
    user_id: str,
    *,
    user_limit: int = SSE_USER_LIMIT,
    user_scope: SseUserScope = "legacy",
) -> None:
    """Add the authenticated-user lease after stream authorization."""

    lease = request.scope.get(SSE_CONNECTION_LEASE_SCOPE)
    if not isinstance(lease, SseConnectionLease):
        raise RuntimeError("SSE connection lease is missing from the request scope.")
    if user_scope == "legacy":
        await lease.add_user_scope(user_id, user_limit=user_limit)
        return
    await lease.add_user_scope(
        user_id,
        user_limit=user_limit,
        user_scope=user_scope,
    )
