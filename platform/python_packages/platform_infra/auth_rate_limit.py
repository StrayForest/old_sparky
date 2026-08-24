from __future__ import annotations

import asyncio
import hmac
import time
from dataclasses import dataclass
from hashlib import sha256

from fastapi import HTTPException, Request, status
from redis.exceptions import RedisError

from python_packages.platform_infra.config import (
    PlatformSettings,
    get_settings,
    is_load_test_source,
)
from python_packages.platform_infra.redis import redis_client


FIXED_WINDOW_INCREMENT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
local expires_at = tonumber(ARGV[1])
if count == 1 or redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIREAT', KEYS[1], expires_at)
end
return count
"""

DELIVERY_COOLDOWN_SCRIPT = """
local created = redis.call('SET', KEYS[1], '1', 'EX', tonumber(ARGV[1]), 'NX')
if created then
  return 0
end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
  return tonumber(ARGV[1])
end
return ttl
"""


@dataclass(frozen=True, slots=True)
class AuthRateLimitState:
    adaptive_turnstile_required: bool = False


def auth_rate_limits_enabled(settings: PlatformSettings) -> bool:
    if settings.platform_environment.strip().lower() == "production":
        return True
    return bool(settings.platform_auth_rate_limit_enabled)


def _remote_address(request: Request) -> str:
    if request.client is None or not request.client.host:
        return "unknown"
    return request.client.host


def _fingerprint(settings: PlatformSettings, value: str) -> str:
    return hmac.new(
        settings.platform_secret_key.encode("utf-8"),
        value.strip().lower().encode("utf-8"),
        sha256,
    ).hexdigest()[:32]


def _window_key(scope: str, fingerprint: str, window_seconds: int, now_epoch: int) -> tuple[str, int]:
    bucket = now_epoch // window_seconds
    expires_at = (bucket + 1) * window_seconds + 1
    return f"platform:auth-rate:v1:{scope}:{fingerprint}:{bucket}", expires_at


async def _increment_fixed_window(
    cache,
    *,
    scope: str,
    fingerprint: str,
    window_seconds: int,
    now_epoch: int,
) -> tuple[int, int]:
    key, expires_at = _window_key(scope, fingerprint, window_seconds, now_epoch)
    count = int(await cache.eval(FIXED_WINDOW_INCREMENT_SCRIPT, 1, key, expires_at))
    retry_after = max(1, expires_at - now_epoch)
    return count, retry_after


async def _current_fixed_window_count(
    cache,
    *,
    scope: str,
    fingerprint: str,
    window_seconds: int,
    now_epoch: int,
) -> int:
    key, _ = _window_key(scope, fingerprint, window_seconds, now_epoch)
    value = await cache.get(key)
    return int(value or 0)


def _rate_limit_error(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many authentication attempts. Try again later.",
        headers={"Retry-After": str(max(1, retry_after))},
    )


def _backend_unavailable_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication protection is temporarily unavailable.",
    )


def progressive_delay_seconds(failed_attempts: int, settings: PlatformSettings) -> float:
    if failed_attempts <= 0 or settings.platform_auth_progressive_delay_base_seconds <= 0:
        return 0.0
    exponent = min(failed_attempts - 1, 8)
    return min(
        settings.platform_auth_progressive_delay_max_seconds,
        settings.platform_auth_progressive_delay_base_seconds * (2**exponent),
    )


def _login_fingerprints(
    request: Request,
    email: str,
    settings: PlatformSettings,
) -> tuple[str, str]:
    address = _remote_address(request)
    ip_fingerprint = _fingerprint(settings, f"ip:{address}")
    account_fingerprint = _fingerprint(settings, f"login-account:{email}")
    return ip_fingerprint, account_fingerprint


def _login_cooldown_key(account_fingerprint: str) -> str:
    return f"platform:auth-rate:v2:login-cooldown:{account_fingerprint}"


async def check_login_rate_limit(
    request: Request,
    email: str,
    *,
    settings: PlatformSettings | None = None,
    now_epoch: int | None = None,
) -> AuthRateLimitState:
    resolved_settings = settings or get_settings()
    if not auth_rate_limits_enabled(resolved_settings):
        return AuthRateLimitState()
    now = int(time.time()) if now_epoch is None else now_epoch
    source_address = _remote_address(request)
    source_exempt = is_load_test_source(resolved_settings, source_address)
    ip_fingerprint, account_fingerprint = _login_fingerprints(
        request,
        email,
        resolved_settings,
    )
    cache = redis_client()
    try:
        if source_exempt:
            ip_count = 0
            retry_after = resolved_settings.platform_auth_login_window_seconds
        else:
            ip_count, retry_after = await _increment_fixed_window(
                cache,
                scope="login-ip",
                fingerprint=ip_fingerprint,
                window_seconds=resolved_settings.platform_auth_login_window_seconds,
                now_epoch=now,
            )
        account_failures = await _current_fixed_window_count(
            cache,
            scope="login-failure",
            fingerprint=account_fingerprint,
            window_seconds=resolved_settings.platform_auth_login_window_seconds,
            now_epoch=now,
        )
        cooldown_active = bool(await cache.get(_login_cooldown_key(account_fingerprint)))
    except RedisError as exc:
        raise _backend_unavailable_error() from exc
    finally:
        await cache.aclose()
    if ip_count > resolved_settings.platform_auth_login_ip_limit:
        raise _rate_limit_error(retry_after)
    if cooldown_active:
        # Return the configured bound rather than the precise remaining TTL so
        # the response does not disclose a high-resolution retry schedule.
        raise _rate_limit_error(
            resolved_settings.platform_auth_login_account_cooldown_seconds
        )
    return AuthRateLimitState(
        adaptive_turnstile_required=(
            account_failures >= resolved_settings.platform_auth_adaptive_turnstile_threshold
            or ip_count >= resolved_settings.platform_auth_login_ip_limit // 2
        )
    )


async def record_login_failure(
    request: Request,
    email: str,
    *,
    settings: PlatformSettings | None = None,
    now_epoch: int | None = None,
) -> None:
    resolved_settings = settings or get_settings()
    if not auth_rate_limits_enabled(resolved_settings):
        return
    now = int(time.time()) if now_epoch is None else now_epoch
    _, account_fingerprint = _login_fingerprints(request, email, resolved_settings)
    cache = redis_client()
    cooldown_retry_after = 0
    try:
        failures, _ = await _increment_fixed_window(
            cache,
            scope="login-failure",
            fingerprint=account_fingerprint,
            window_seconds=resolved_settings.platform_auth_login_window_seconds,
            now_epoch=now,
        )
        if failures > resolved_settings.platform_auth_login_account_limit:
            await cache.eval(
                DELIVERY_COOLDOWN_SCRIPT,
                1,
                _login_cooldown_key(account_fingerprint),
                resolved_settings.platform_auth_login_account_cooldown_seconds,
            )
            cooldown_retry_after = (
                resolved_settings.platform_auth_login_account_cooldown_seconds
            )
            # Reset the failure window after a cooldown starts. Once the bounded
            # cooldown expires, another source must build a fresh account-wide
            # failure budget instead of re-locking the account with one request.
            failure_key, _ = _window_key(
                "login-failure",
                account_fingerprint,
                resolved_settings.platform_auth_login_window_seconds,
                now,
            )
            await cache.delete(failure_key)
    except RedisError as exc:
        raise _backend_unavailable_error() from exc
    finally:
        await cache.aclose()
    delay = progressive_delay_seconds(failures, resolved_settings)
    if delay > 0:
        await asyncio.sleep(delay)
    if failures > resolved_settings.platform_auth_login_account_limit:
        raise _rate_limit_error(cooldown_retry_after)


async def clear_login_failures(
    request: Request,
    email: str,
    *,
    settings: PlatformSettings | None = None,
    now_epoch: int | None = None,
) -> None:
    resolved_settings = settings or get_settings()
    if not auth_rate_limits_enabled(resolved_settings):
        return
    now = int(time.time()) if now_epoch is None else now_epoch
    _, account_fingerprint = _login_fingerprints(request, email, resolved_settings)
    key, _ = _window_key(
        "login-failure",
        account_fingerprint,
        resolved_settings.platform_auth_login_window_seconds,
        now,
    )
    cache = redis_client()
    try:
        await cache.delete(key)
        await cache.delete(_login_cooldown_key(account_fingerprint))
    except RedisError as exc:
        raise _backend_unavailable_error() from exc
    finally:
        await cache.aclose()


async def check_registration_rate_limit(
    request: Request,
    *,
    settings: PlatformSettings | None = None,
    now_epoch: int | None = None,
) -> AuthRateLimitState:
    resolved_settings = settings or get_settings()
    if not auth_rate_limits_enabled(resolved_settings):
        return AuthRateLimitState()
    if is_load_test_source(resolved_settings, _remote_address(request)):
        return AuthRateLimitState()
    now = int(time.time()) if now_epoch is None else now_epoch
    fingerprint = _fingerprint(resolved_settings, f"ip:{_remote_address(request)}")
    cache = redis_client()
    try:
        attempts, retry_after = await _increment_fixed_window(
            cache,
            scope="register-ip",
            fingerprint=fingerprint,
            window_seconds=resolved_settings.platform_auth_register_window_seconds,
            now_epoch=now,
        )
    except RedisError as exc:
        raise _backend_unavailable_error() from exc
    finally:
        await cache.aclose()
    if attempts > resolved_settings.platform_auth_register_ip_limit:
        raise _rate_limit_error(retry_after)
    return AuthRateLimitState(
        adaptive_turnstile_required=(
            attempts >= resolved_settings.platform_auth_adaptive_turnstile_threshold
        )
    )


async def check_password_reset_rate_limit(
    request: Request,
    account_key: str,
    *,
    operation: str,
    settings: PlatformSettings | None = None,
    now_epoch: int | None = None,
) -> AuthRateLimitState:
    if operation not in {"request", "verify", "confirm"}:
        raise ValueError(
            "Password-reset rate limit operation must be request, verify or confirm."
        )
    return await _check_token_action_rate_limit(
        request,
        account_key,
        scope=f"reset-{operation}",
        settings=settings,
        now_epoch=now_epoch,
    )


async def check_email_verification_resend_rate_limit(
    request: Request,
    account_key: str,
    *,
    settings: PlatformSettings | None = None,
    now_epoch: int | None = None,
) -> AuthRateLimitState:
    return await _check_token_action_rate_limit(
        request,
        account_key,
        scope="verification-resend",
        settings=settings,
        now_epoch=now_epoch,
    )


async def check_email_verification_confirm_rate_limit(
    request: Request,
    account_key: str,
    *,
    settings: PlatformSettings | None = None,
    now_epoch: int | None = None,
) -> AuthRateLimitState:
    return await _check_token_action_rate_limit(
        request,
        account_key,
        scope="verification-confirm",
        settings=settings,
        now_epoch=now_epoch,
    )


async def check_steam_auth_rate_limit(
    request: Request,
    account_key: str,
    *,
    operation: str,
    settings: PlatformSettings | None = None,
    now_epoch: int | None = None,
) -> AuthRateLimitState:
    if operation not in {"callback", "link", "login"}:
        raise ValueError("Steam authentication operation is invalid.")
    return await _check_token_action_rate_limit(
        request,
        account_key,
        scope=f"steam-{operation}",
        settings=settings,
        now_epoch=now_epoch,
    )


async def check_email_link_rate_limit(
    request: Request,
    account_key: str,
    *,
    operation: str,
    settings: PlatformSettings | None = None,
    now_epoch: int | None = None,
) -> AuthRateLimitState:
    if operation not in {"request", "resend", "confirm"}:
        raise ValueError("Email-link rate limit operation is invalid.")
    return await _check_token_action_rate_limit(
        request,
        account_key,
        scope=f"email-link-{operation}",
        settings=settings,
        now_epoch=now_epoch,
    )


async def reserve_auth_delivery_cooldown(
    account_key: str,
    *,
    scope: str,
    settings: PlatformSettings | None = None,
) -> int:
    """Atomically reserve a per-account delivery slot or raise with Retry-After.

    The key is an HMAC fingerprint, so Redis never receives an email address or
    other account identifier. Production fails closed if Redis is unavailable.
    """

    resolved_settings = settings or get_settings()
    cooldown_seconds = resolved_settings.platform_auth_delivery_cooldown_seconds
    if not auth_rate_limits_enabled(resolved_settings):
        return cooldown_seconds
    if not scope or len(scope) > 64:
        raise ValueError("Authentication delivery cooldown scope is invalid.")
    fingerprint = _fingerprint(
        resolved_settings,
        f"delivery:{scope}:{account_key}",
    )
    key = f"platform:auth-delivery:v1:{scope}:{fingerprint}"
    cache = redis_client()
    try:
        retry_after = int(
            await cache.eval(
                DELIVERY_COOLDOWN_SCRIPT,
                1,
                key,
                cooldown_seconds,
            )
        )
    except RedisError as exc:
        raise _backend_unavailable_error() from exc
    finally:
        await cache.aclose()
    if retry_after > 0:
        raise _rate_limit_error(retry_after)
    return cooldown_seconds


async def _check_token_action_rate_limit(
    request: Request,
    account_key: str,
    *,
    scope: str,
    settings: PlatformSettings | None,
    now_epoch: int | None,
) -> AuthRateLimitState:
    resolved_settings = settings or get_settings()
    if not auth_rate_limits_enabled(resolved_settings):
        return AuthRateLimitState()
    now = int(time.time()) if now_epoch is None else now_epoch
    address = _remote_address(request)
    source_exempt = is_load_test_source(resolved_settings, address)
    ip_fingerprint = _fingerprint(resolved_settings, f"{scope}-ip:{address}")
    account_fingerprint = _fingerprint(
        resolved_settings,
        f"{scope}-account:{account_key}",
    )
    cache = redis_client()
    try:
        if source_exempt:
            ip_count = 0
            retry_after = resolved_settings.platform_auth_reset_window_seconds
        else:
            ip_count, retry_after = await _increment_fixed_window(
                cache,
                scope=f"{scope}-ip",
                fingerprint=ip_fingerprint,
                window_seconds=resolved_settings.platform_auth_reset_window_seconds,
                now_epoch=now,
            )
        account_count, account_retry_after = await _increment_fixed_window(
            cache,
            scope=f"{scope}-account",
            fingerprint=account_fingerprint,
            window_seconds=resolved_settings.platform_auth_reset_window_seconds,
            now_epoch=now,
        )
    except RedisError as exc:
        raise _backend_unavailable_error() from exc
    finally:
        await cache.aclose()
    if ip_count > resolved_settings.platform_auth_reset_ip_limit:
        raise _rate_limit_error(retry_after)
    if account_count > resolved_settings.platform_auth_reset_account_limit:
        raise _rate_limit_error(account_retry_after)
    return AuthRateLimitState(
        adaptive_turnstile_required=(
            max(ip_count, account_count)
            >= resolved_settings.platform_auth_adaptive_turnstile_threshold
        )
    )
