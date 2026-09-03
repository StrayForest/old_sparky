"""Versioned, non-authoritative account details for ``GET /users/me``."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from time import perf_counter
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.services.media import compatibility_media_url, load_media_descriptors
from apps.platform_api.app.services.tournament_allowances import (
    PRIVATE_TOURNAMENT_MONTHLY_LIMIT,
    private_tournament_monthly_remaining,
)
from python_packages.platform_infra.models import (
    ExternalIdentity,
    MediaAsset,
    PasswordCredential,
    PlayerProfile,
    User,
)
from python_packages.platform_infra.redis import redis_client

logger = logging.getLogger(__name__)

USER_ACCOUNT_READ_MODEL_KEY_PREFIX = "platform:user-account:read-model:v1"
USER_ACCOUNT_READ_MODEL_SAFETY_TTL_SECONDS = 60
_REDIS_UNAVAILABLE = (RedisError, OSError, asyncio.TimeoutError)

_SET_IF_NEWER_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then
    redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
    return 1
end
local separator = string.find(current, '\\n', 1, true)
if not separator then
    redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
    return 1
end
local current_revision = tonumber(string.sub(current, 1, separator - 1))
local next_revision = tonumber(ARGV[1])
if not current_revision or current_revision <= next_revision then
    redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
    return 1
end
return 0
"""


def user_account_read_model_key(user_id: str) -> str:
    return f"{USER_ACCOUNT_READ_MODEL_KEY_PREFIX}:{user_id}"


def _revision_timestamp(value: datetime | None) -> int:
    if value is None:
        return 0
    return int(value.timestamp() * 1_000_000)


def _encode(*, revision: int, payload: dict[str, Any]) -> bytes:
    return f"{int(revision)}\n".encode("ascii") + json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")


def _decode(raw: bytes | str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    _revision, separator, payload = raw_bytes.partition(b"\n")
    if not separator:
        return None
    try:
        decoded = json.loads(payload)
    except (TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


async def build_user_account_read_model(
    db_session: AsyncSession,
    *,
    user: User,
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    """Build supplemental account fields; authentication stays DB-authoritative."""

    AvatarAsset = MediaAsset
    row = (
        await db_session.execute(
            select(PlayerProfile, ExternalIdentity, PasswordCredential, AvatarAsset)
            .select_from(User)
            .outerjoin(PlayerProfile, PlayerProfile.user_id == User.id)
            .outerjoin(
                ExternalIdentity,
                and_(
                    ExternalIdentity.user_id == User.id,
                    ExternalIdentity.provider == "steam",
                ),
            )
            .outerjoin(PasswordCredential, PasswordCredential.user_id == User.id)
            .outerjoin(AvatarAsset, AvatarAsset.id == PlayerProfile.avatar_asset_id)
            .where(User.id == user.id)
        )
    ).first()
    profile = row[0] if row is not None else None
    steam_identity = row[1] if row is not None else None
    password_credential = row[2] if row is not None else None
    avatar_asset = row[3] if row is not None else None
    avatar_media = None
    if profile is not None and profile.avatar_asset_id:
        descriptors = await load_media_descriptors(
            db_session,
            (profile.avatar_asset_id,),
        )
        avatar_media = descriptors.get(profile.avatar_asset_id)

    monthly_remaining = await private_tournament_monthly_remaining(
        db_session,
        organizer_user_id=user.id,
        now=now,
    )
    payload = {
        "avatar_url": compatibility_media_url(
            avatar_media,
            preferred_variant="avatar-256",
        ),
        "avatar_media": (
            avatar_media.model_dump(mode="json") if avatar_media is not None else None
        ),
        "steam_id": steam_identity.subject if steam_identity is not None else None,
        "steam_linked": steam_identity is not None,
        "has_password": password_credential is not None,
        "can_unlink_steam": bool(
            user.email
            and user.email_verified_at is not None
            and password_credential is not None
        ),
        "private_tournament_monthly_remaining": monthly_remaining,
        "private_tournament_monthly_limit": PRIVATE_TOURNAMENT_MONTHLY_LIMIT,
    }
    revision = max(
        _revision_timestamp(getattr(user, "updated_at", None)),
        _revision_timestamp(getattr(profile, "updated_at", None)),
        _revision_timestamp(getattr(steam_identity, "linked_at", None)),
        _revision_timestamp(getattr(password_credential, "updated_at", None)),
        _revision_timestamp(getattr(avatar_asset, "updated_at", None)),
    )
    return revision, payload


async def _write(user_id: str, *, revision: int, payload: dict[str, Any]) -> None:
    client = redis_client(decode_responses=False)
    try:
        await client.eval(
            _SET_IF_NEWER_SCRIPT,
            1,
            user_account_read_model_key(user_id),
            str(int(revision)),
            _encode(revision=revision, payload=payload),
            str(USER_ACCOUNT_READ_MODEL_SAFETY_TTL_SECONDS),
        )
    except _REDIS_UNAVAILABLE as exc:
        logger.warning(
            "Redis user account read-model write failed user_id=%s error=%s",
            user_id,
            type(exc).__name__,
        )
    finally:
        await client.aclose()


async def get_or_build_user_account_read_model(
    db_session: AsyncSession,
    *,
    user: User,
    now: datetime,
) -> dict[str, Any]:
    """Read cached supplemental fields or build them from the current session."""

    client = redis_client(decode_responses=False)
    started_at = perf_counter()
    try:
        cached = _decode(await client.get(user_account_read_model_key(user.id)))
        if cached is not None:
            # The allowance is derived from tournament rows, not the user
            # record. Keep that one small authoritative count fresh even when
            # the profile/security joins are served from Redis. Normal
            # tournament writes also invalidate the model, but this protects
            # maintenance/backfill writes that do not pass through the API.
            cached[
                "private_tournament_monthly_remaining"
            ] = await private_tournament_monthly_remaining(
                db_session,
                organizer_user_id=user.id,
                now=now,
            )
            cached["private_tournament_monthly_limit"] = PRIVATE_TOURNAMENT_MONTHLY_LIMIT
            return cached
    except _REDIS_UNAVAILABLE as exc:
        logger.warning(
            "Redis user account read-model read failed user_id=%s error=%s",
            user.id,
            type(exc).__name__,
        )
    finally:
        await client.aclose()

    revision, payload = await build_user_account_read_model(
        db_session,
        user=user,
        now=now,
    )
    await _write(user.id, revision=revision, payload=payload)
    logger.debug(
        "user_account_read_model_built user_id=%s build_ms=%.2f",
        user.id,
        (perf_counter() - started_at) * 1000,
    )
    return payload


async def delete_user_account_read_model(user_id: str) -> None:
    client = redis_client(decode_responses=False)
    try:
        await client.delete(user_account_read_model_key(user_id))
    except _REDIS_UNAVAILABLE as exc:
        logger.warning(
            "Redis user account read-model delete failed user_id=%s error=%s",
            user_id,
            type(exc).__name__,
        )
    finally:
        await client.aclose()
