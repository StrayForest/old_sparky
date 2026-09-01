from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
import json
import logging
from time import perf_counter
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import JSON, cast, func, literal, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from apps.platform_api.app.api.schemas import (
    DeadlockProfileResponse,
    TournamentProfileResponse,
    TournamentScopedProfileResponse,
)
from apps.platform_api.app.services.media import (
    compatibility_media_url,
    media_descriptor_response,
)
from python_packages.platform_infra.db import session_factory
from python_packages.platform_infra.media.repository import AssetDescriptor, VariantRecord
from python_packages.platform_infra.models import (
    DeadlockProfile,
    MediaAsset,
    MediaVariant,
    PlayerProfile,
)
from python_packages.platform_infra.performance import record_profile_read_model_event
from python_packages.platform_infra.redis import redis_client

logger = logging.getLogger(__name__)

PROFILE_READ_MODEL_KEY_PREFIX = "platform:profile:read-model:v1"
PROFILE_READ_MODEL_SAFETY_TTL_SECONDS = 7 * 24 * 60 * 60
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


@dataclass(frozen=True, slots=True)
class ProfileReadModel:
    revision: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class ProfileReadModelEnvelope:
    revision: int
    payload: bytes


def profile_read_model_key(user_id: str) -> str:
    return f"{PROFILE_READ_MODEL_KEY_PREFIX}:{user_id}"


def _encode_envelope(*, revision: int, payload: bytes) -> bytes:
    return f"{int(revision)}\n".encode("ascii") + payload


def _decode_envelope(raw: bytes | str | None) -> ProfileReadModelEnvelope | None:
    if raw is None:
        return None
    raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    revision_bytes, separator, payload = raw_bytes.partition(b"\n")
    if not separator:
        return None
    try:
        return ProfileReadModelEnvelope(revision=int(revision_bytes), payload=payload)
    except (TypeError, ValueError):
        return None


def profile_read_model_payload(raw: bytes | str | None) -> bytes | None:
    envelope = _decode_envelope(raw)
    return envelope.payload if envelope is not None else None


def profile_read_model_cached_revision(raw: bytes | str | None) -> int | None:
    envelope = _decode_envelope(raw)
    return envelope.revision if envelope is not None else None


def _variant_json_aggregate(asset_id_column):
    variant_object = func.json_build_object(
        "variant_name",
        MediaVariant.variant_name,
        "object_key",
        MediaVariant.object_key,
        "mime_type",
        MediaVariant.mime_type,
        "width",
        MediaVariant.width,
        "height",
        MediaVariant.height,
        "byte_size",
        MediaVariant.byte_size,
        "sha256",
        MediaVariant.sha256,
    )
    return (
        select(
            func.coalesce(
                func.json_agg(
                    aggregate_order_by(
                        variant_object,
                        MediaVariant.width,
                        MediaVariant.variant_name,
                    )
                ),
                cast(literal("[]"), JSON),
            )
        )
        .where(MediaVariant.asset_id == asset_id_column)
        .correlate(PlayerProfile)
        .scalar_subquery()
    )


def _json_variants(value: Any) -> Iterable[dict[str, Any]]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
    if not isinstance(value, list):
        return ()
    return (item for item in value if isinstance(item, dict))


def _media_descriptor(
    asset: MediaAsset | None,
    raw_variants: Any,
):
    if asset is None:
        return None
    descriptor = AssetDescriptor(
        asset_id=str(asset.id),
        purpose=str(asset.purpose),
        status=str(asset.status),
        error_code=asset.error_code,
        variants=tuple(
            VariantRecord(
                variant_name=str(item["variant_name"]),
                object_key=str(item["object_key"]),
                mime_type=str(item["mime_type"]),
                width=int(item["width"]),
                height=int(item["height"]),
                byte_size=int(item["byte_size"]),
                sha256=str(item["sha256"]),
            )
            for item in _json_variants(raw_variants)
            if all(
                key in item
                for key in (
                    "variant_name",
                    "object_key",
                    "mime_type",
                    "width",
                    "height",
                    "byte_size",
                    "sha256",
                )
            )
        ),
    )
    return media_descriptor_response(descriptor)


def _revision_timestamp(value: datetime | None) -> int:
    if value is None:
        return 0
    return int(value.timestamp() * 1_000_000)


def profile_read_model_revision(
    profile: PlayerProfile,
    deadlock_profile: DeadlockProfile | None,
    avatar_asset: MediaAsset | None,
    banner_asset: MediaAsset | None,
) -> int:
    return max(
        _revision_timestamp(getattr(profile, "updated_at", None)),
        _revision_timestamp(getattr(deadlock_profile, "updated_at", None)),
        _revision_timestamp(getattr(avatar_asset, "updated_at", None)),
        _revision_timestamp(getattr(banner_asset, "updated_at", None)),
    )


async def _build_profile_with_session(
    db_session: AsyncSession,
    user_id: str,
) -> ProfileReadModel | None:
    AvatarAsset = aliased(MediaAsset, name="avatar_asset")
    BannerAsset = aliased(MediaAsset, name="banner_asset")
    row = (
        await db_session.execute(
            select(
                PlayerProfile,
                DeadlockProfile,
                AvatarAsset,
                BannerAsset,
                _variant_json_aggregate(PlayerProfile.avatar_asset_id).label(
                    "avatar_variants"
                ),
                _variant_json_aggregate(PlayerProfile.banner_asset_id).label(
                    "banner_variants"
                ),
            )
            .outerjoin(
                DeadlockProfile,
                DeadlockProfile.user_id == PlayerProfile.user_id,
            )
            .outerjoin(AvatarAsset, AvatarAsset.id == PlayerProfile.avatar_asset_id)
            .outerjoin(BannerAsset, BannerAsset.id == PlayerProfile.banner_asset_id)
            .where(PlayerProfile.user_id == user_id)
        )
    ).one_or_none()
    if row is None:
        return None

    profile, deadlock_profile, avatar_asset, banner_asset = row[:4]
    avatar_media = _media_descriptor(avatar_asset, row.avatar_variants)
    banner_media = _media_descriptor(banner_asset, row.banner_variants)
    profile_response = TournamentProfileResponse.model_validate(profile).model_copy(
        update={
            "avatar_url": compatibility_media_url(
                avatar_media,
                preferred_variant="avatar-256",
            ),
            "banner_url": compatibility_media_url(
                banner_media,
                preferred_variant="banner-1920",
            ),
            "avatar_media": avatar_media,
            "banner_media": banner_media,
        }
    )
    response = TournamentScopedProfileResponse(
        profile=profile_response,
        deadlock_profile=(
            DeadlockProfileResponse.model_validate(deadlock_profile)
            if deadlock_profile is not None
            else None
        ),
    )
    payload = response.model_dump_json().encode("utf-8")
    revision = profile_read_model_revision(
        profile,
        deadlock_profile,
        avatar_asset,
        banner_asset,
    )
    return ProfileReadModel(revision=revision, payload=payload)


async def build_profile_read_model(
    user_id: str,
    db_session: AsyncSession | None = None,
) -> ProfileReadModel | None:
    """Build one tournament-profile JSON payload with one DB statement."""

    if db_session is not None:
        return await _build_profile_with_session(db_session, user_id)
    async with session_factory()() as owned_session:
        return await _build_profile_with_session(owned_session, user_id)


async def write_profile_read_model(
    user_id: str,
    *,
    revision: int,
    payload: bytes,
) -> bool:
    client = redis_client(decode_responses=False)
    try:
        stored = bool(
            await client.eval(
                _SET_IF_NEWER_SCRIPT,
                1,
                profile_read_model_key(user_id),
                str(int(revision)),
                _encode_envelope(revision=revision, payload=payload),
                str(PROFILE_READ_MODEL_SAFETY_TTL_SECONDS),
            )
        )
        record_profile_read_model_event(
            "profile_read_model_write" if stored else "profile_read_model_stale_write_skipped",
            payload_bytes=len(payload),
            revision=revision,
        )
        return stored
    except _REDIS_UNAVAILABLE as exc:
        record_profile_read_model_event(
            "profile_read_model_redis_error",
            payload_bytes=len(payload),
            revision=revision,
        )
        logger.warning(
            "Redis profile read-model write failed user_id=%s error=%s",
            user_id,
            type(exc).__name__,
        )
        return False
    finally:
        await client.aclose()


async def delete_profile_read_model(user_id: str) -> None:
    client = redis_client(decode_responses=False)
    try:
        await client.delete(profile_read_model_key(user_id))
    except _REDIS_UNAVAILABLE as exc:
        logger.warning(
            "Redis profile read-model delete failed user_id=%s error=%s",
            user_id,
            type(exc).__name__,
        )
    finally:
        await client.aclose()


async def refresh_profile_read_model(user_id: str) -> ProfileReadModel | None:
    started_at = perf_counter()
    try:
        model = await build_profile_read_model(user_id)
        record_profile_read_model_event(
            "profile_read_model_build",
            build_ms=(perf_counter() - started_at) * 1000,
            payload_bytes=len(model.payload) if model is not None else 0,
            revision=model.revision if model is not None else None,
        )
        if model is None:
            await delete_profile_read_model(user_id)
            return None
        await write_profile_read_model(
            user_id,
            revision=model.revision,
            payload=model.payload,
        )
        return model
    except Exception:
        record_profile_read_model_event(
            "profile_read_model_db_fallback",
            build_ms=(perf_counter() - started_at) * 1000,
        )
        logger.exception("Profile read-model refresh failed user_id=%s", user_id)
        return None
