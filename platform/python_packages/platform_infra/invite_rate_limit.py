from __future__ import annotations

import hmac
from hashlib import sha256
import time
from typing import Literal

from fastapi import HTTPException, Request, status
from redis.exceptions import RedisError

from python_packages.platform_infra.config import PlatformSettings, get_settings
from python_packages.platform_infra.redis import redis_client


INVITE_INCREMENT_SCRIPT = """
local user_count = redis.call('INCR', KEYS[1])
local ip_count = redis.call('INCR', KEYS[2])
for index = 1, 2 do
  if redis.call('TTL', KEYS[index]) < 0 then
    redis.call('EXPIREAT', KEYS[index], ARGV[1])
  end
end
return {user_count, ip_count}
"""

InviteOperation = Literal["lookup", "claim", "manage"]


def invite_rate_limits_enabled(settings: PlatformSettings) -> bool:
    if settings.platform_environment.strip().lower() == "production":
        return True
    return bool(settings.platform_invite_rate_limit_enabled)


def _fingerprint(settings: PlatformSettings, value: str) -> str:
    return hmac.new(
        settings.platform_secret_key.encode("utf-8"),
        value.encode("utf-8"),
        sha256,
    ).hexdigest()[:32]


def _limits(settings: PlatformSettings, operation: InviteOperation) -> tuple[int, int]:
    if operation == "lookup":
        return (
            settings.platform_invite_lookup_user_limit,
            settings.platform_invite_lookup_ip_limit,
        )
    if operation == "claim":
        return (
            settings.platform_invite_claim_user_limit,
            settings.platform_invite_claim_ip_limit,
        )
    return (
        settings.platform_invite_manage_user_limit,
        settings.platform_invite_manage_ip_limit,
    )


async def check_invite_rate_limit(
    request: Request,
    *,
    user_id: str,
    operation: InviteOperation,
    settings: PlatformSettings | None = None,
    now_epoch: int | None = None,
) -> None:
    resolved = settings or get_settings()
    if not invite_rate_limits_enabled(resolved):
        return
    now = int(time.time()) if now_epoch is None else now_epoch
    window = resolved.platform_invite_rate_window_seconds
    bucket = now // window
    expires_at = (bucket + 1) * window + 1
    retry_after = max(1, expires_at - now)
    address = request.client.host if request.client and request.client.host else "unknown"
    user_key = _fingerprint(resolved, f"{operation}:user:{user_id}")
    ip_key = _fingerprint(resolved, f"{operation}:ip:{address}")
    prefix = f"platform:invite-rate:v1:{bucket}:{operation}"
    client = redis_client()
    try:
        values = await client.eval(
            INVITE_INCREMENT_SCRIPT,
            2,
            f"{prefix}:user:{user_key}",
            f"{prefix}:ip:{ip_key}",
            expires_at,
        )
        user_count, ip_count = (int(value) for value in values)
    except RedisError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "invite_protection_unavailable",
                "message": "Invite protection is temporarily unavailable.",
            },
        ) from exc
    finally:
        await client.aclose()

    user_limit, ip_limit = _limits(resolved, operation)
    if user_count > user_limit or ip_count > ip_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "invite_rate_limited",
                "message": "Too many invite attempts. Try again later.",
            },
            headers={"Retry-After": str(retry_after)},
        )
