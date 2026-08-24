from __future__ import annotations

import hmac
from hashlib import sha256
import time

from fastapi import HTTPException, Request, status
from redis.exceptions import RedisError

from python_packages.platform_infra.config import (
    PlatformSettings,
    get_settings,
    is_load_test_source,
)
from python_packages.platform_infra.redis import redis_client


MEDIA_UPLOAD_INCREMENT_SCRIPT = """
local user_count = redis.call('INCR', KEYS[1])
local ip_count = redis.call('INCR', KEYS[2])
local user_bytes = redis.call('INCRBY', KEYS[3], ARGV[2])
for index = 1, 3 do
  if redis.call('TTL', KEYS[index]) < 0 then
    redis.call('EXPIREAT', KEYS[index], ARGV[1])
  end
end
return {user_count, ip_count, user_bytes}
"""

MEDIA_UPLOAD_USER_INCREMENT_SCRIPT = """
local user_count = redis.call('INCR', KEYS[1])
local user_bytes = redis.call('INCRBY', KEYS[2], ARGV[2])
for index = 1, 2 do
  if redis.call('TTL', KEYS[index]) < 0 then
    redis.call('EXPIREAT', KEYS[index], ARGV[1])
  end
end
return {user_count, user_bytes}
"""


def media_rate_limits_enabled(settings: PlatformSettings) -> bool:
    if settings.platform_environment.strip().lower() == "production":
        return True
    return bool(settings.platform_media_rate_limit_enabled)


def _fingerprint(settings: PlatformSettings, value: str) -> str:
    return hmac.new(
        settings.platform_secret_key.encode("utf-8"),
        value.encode("utf-8"),
        sha256,
    ).hexdigest()[:32]


def _rate_limit_error(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"code": "media_rate_limited", "message": "Media upload limit reached."},
        headers={"Retry-After": str(max(1, retry_after))},
    )


async def check_media_upload_rate_limit(
    request: Request,
    *,
    user_id: str,
    upload_bytes: int,
    settings: PlatformSettings | None = None,
    now_epoch: int | None = None,
) -> None:
    resolved = settings or get_settings()
    if not media_rate_limits_enabled(resolved):
        return
    now = int(time.time()) if now_epoch is None else now_epoch
    window = resolved.platform_media_upload_window_seconds
    bucket = now // window
    expires_at = (bucket + 1) * window + 1
    retry_after = max(1, expires_at - now)
    address = request.client.host if request.client and request.client.host else "unknown"
    user_key = _fingerprint(resolved, f"user:{user_id}")
    ip_key = _fingerprint(resolved, f"ip:{address}")
    prefix = f"platform:media-rate:v1:{bucket}"
    keys = (
        f"{prefix}:user-count:{user_key}",
        f"{prefix}:ip-count:{ip_key}",
        f"{prefix}:user-bytes:{user_key}",
    )
    client = redis_client()
    try:
        if is_load_test_source(resolved, address):
            values = await client.eval(
                MEDIA_UPLOAD_USER_INCREMENT_SCRIPT,
                2,
                keys[0],
                keys[2],
                expires_at,
                max(0, upload_bytes),
            )
            user_count, user_bytes = (int(value) for value in values)
            ip_count = 0
        else:
            values = await client.eval(
                MEDIA_UPLOAD_INCREMENT_SCRIPT,
                3,
                *keys,
                expires_at,
                max(0, upload_bytes),
            )
            user_count, ip_count, user_bytes = (int(value) for value in values)
    except RedisError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "media_protection_unavailable",
                "message": "Media upload protection is temporarily unavailable.",
            },
        ) from exc
    finally:
        await client.aclose()

    if (
        user_count > resolved.platform_media_upload_user_limit
        or ip_count > resolved.platform_media_upload_ip_limit
        or user_bytes > resolved.platform_media_upload_user_byte_limit
    ):
        raise _rate_limit_error(retry_after)
