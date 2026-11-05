"""Versioned, non-authoritative account details for ``GET /users/me``."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import secrets
from time import perf_counter
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.services.media import (
    compatibility_media_url,
    load_media_descriptor_for_preloaded_asset,
    load_media_descriptors,
)
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
from python_packages.platform_infra.performance import record_redis_read_model_event
from python_packages.platform_infra.redis import redis_client

logger = logging.getLogger(__name__)

USER_ACCOUNT_READ_MODEL_KEY_PREFIX = "platform:user-account:read-model:v1"
USER_ACCOUNT_READ_MODEL_SAFETY_TTL_SECONDS = 60
_REDIS_UNAVAILABLE = (RedisError, OSError, asyncio.TimeoutError)

_RESERVE_GENERATION_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current then
    local separator = string.find(current, '\\n', 1, true)
    if separator then
        local revision = tonumber(string.sub(current, 1, separator - 1))
        local body = string.sub(current, separator + 1)
        local ok, envelope = pcall(cjson.decode, body)
        if ok and revision and type(envelope) == 'table' then
            if envelope[1] == 'ready'
                and type(envelope[2]) == 'string'
                and string.len(envelope[2]) > 0
                and type(envelope[3]) == 'table' then
                return current
            end
            if revision == 0 and envelope[1] == 'pending'
                and type(envelope[2]) == 'string'
                and string.len(envelope[2]) > 0 then
                return current
            end
        end
    end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return ARGV[1]
"""

_SET_IF_CURRENT_GENERATION_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then
    return 0
end
local separator = string.find(current, '\\n', 1, true)
if not separator then
    return 0
end
local current_revision = tonumber(string.sub(current, 1, separator - 1))
local body = string.sub(current, separator + 1)
local ok, envelope = pcall(cjson.decode, body)
if not ok or type(envelope) ~= 'table' or not current_revision then
    return 0
end
local current_generation = nil
if envelope[1] == 'pending' and current_revision == 0
    and type(envelope[2]) == 'string' and string.len(envelope[2]) > 0 then
    current_generation = envelope[2]
elseif envelope[1] == 'ready' and type(envelope[2]) == 'string'
    and string.len(envelope[2]) > 0 and type(envelope[3]) == 'table' then
    current_generation = envelope[2]
else
    return 0
end
if type(current_generation) ~= 'string' or current_generation ~= ARGV[1] then
    return 0
end
local next_revision = tonumber(ARGV[2])
if not next_revision or current_revision > next_revision then
    return 0
end
redis.call('SET', KEYS[1], ARGV[3], 'EX', ARGV[4])
return 1
"""


def user_account_read_model_key(user_id: str) -> str:
    return f"{USER_ACCOUNT_READ_MODEL_KEY_PREFIX}:{user_id}"


def _revision_timestamp(value: datetime | None) -> int:
    if value is None:
        return 0
    return int(value.timestamp() * 1_000_000)


def _encode_pending(*, generation: str) -> bytes:
    return b"0\n" + json.dumps(
        ["pending", generation],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _encode(*, revision: int, generation: str, payload: dict[str, Any]) -> bytes:
    return f"{int(revision)}\n".encode("ascii") + json.dumps(
        ["ready", generation, payload],
        separators=(",", ":"),
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")


@dataclass(slots=True)
class UserAccountReadModelLookup:
    account: dict[str, Any]
    get_ms: float
    build_ms: float
    outcome: str
    pending_generation: str | None = None
    pending_revision: int | None = None
    pending_payload: dict[str, Any] | None = None
    finalized: bool = False

    def finish_without_write(self) -> None:
        if self.finalized:
            return
        self._record(set_ms=0.0)

    async def write_after_db_release(self, *, user_id: str) -> None:
        if self.finalized:
            return
        set_ms = 0.0
        if self.pending_generation is not None:
            started = perf_counter()
            try:
                write_ok = await _write(
                    user_id,
                    generation=self.pending_generation,
                    revision=self.pending_revision or 0,
                    payload=self.pending_payload or {},
                )
            except BaseException:
                self.outcome = "write_error"
                raise
            finally:
                # This is wall time across Redis I/O, not CPU time.
                set_ms = (perf_counter() - started) * 1000
            if write_ok is None:
                self.outcome = "write_error"
        self._record(set_ms=set_ms)

    def _record(self, *, set_ms: float) -> None:
        if self.finalized:
            return
        self.finalized = True
        record_redis_read_model_event(
            model="user_account",
            outcome=self.outcome,
            get_ms=self.get_ms,
            build_ms=self.build_ms,
            set_ms=set_ms,
            include_payload_metadata=False,
        )


def _decode(
    raw: bytes | str | None,
) -> tuple[str, str, int, dict[str, Any]] | tuple[str, str] | None:
    if raw is None:
        return None
    raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    raw_revision, separator, payload = raw_bytes.partition(b"\n")
    if not separator:
        return None
    try:
        revision = int(raw_revision)
        decoded = json.loads(payload)
    except (TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, list) or not decoded:
        # v1 object payloads and malformed values are never served by the v2 reader.
        return None
    if (
        decoded[0] == "pending"
        and len(decoded) == 2
        and isinstance(decoded[1], str)
        and bool(decoded[1])
        and revision == 0
    ):
        return "pending", decoded[1]
    if (
        decoded[0] == "ready"
        and len(decoded) == 3
        and isinstance(decoded[1], str)
        and bool(decoded[1])
        and isinstance(decoded[2], dict)
    ):
        return "ready", decoded[1], revision, decoded[2]
    return None


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
            select(
                PlayerProfile.user_id,
                PlayerProfile.avatar_asset_id,
                PlayerProfile.updated_at,
                ExternalIdentity.id,
                ExternalIdentity.subject,
                ExternalIdentity.linked_at,
                PasswordCredential.user_id,
                PasswordCredential.updated_at,
                AvatarAsset,
            )
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
    profile_id = row[0] if row is not None else None
    avatar_asset_id = row[1] if row is not None else None
    profile_updated_at = row[2] if row is not None else None
    steam_identity_id = row[3] if row is not None else None
    steam_subject = row[4] if row is not None else None
    steam_linked_at = row[5] if row is not None else None
    password_credential_id = row[6] if row is not None else None
    password_credential_updated_at = row[7] if row is not None else None
    avatar_asset = row[8] if row is not None else None
    avatar_media = None
    if profile_id is not None and avatar_asset_id:
        if avatar_asset is not None and avatar_asset.id == avatar_asset_id:
            avatar_media = await load_media_descriptor_for_preloaded_asset(
                db_session,
                avatar_asset,
            )
        else:
            # Preserve the established fallback for a missing/inconsistent
            # joined asset row instead of changing its absence semantics.
            descriptors = await load_media_descriptors(db_session, (avatar_asset_id,))
            avatar_media = descriptors.get(avatar_asset_id)

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
        "steam_id": steam_subject if steam_identity_id is not None else None,
        "steam_linked": steam_identity_id is not None,
        "has_password": password_credential_id is not None,
        "can_unlink_steam": bool(
            user.email
            and user.email_verified_at is not None
            and password_credential_id is not None
        ),
        "private_tournament_monthly_remaining": monthly_remaining,
        "private_tournament_monthly_limit": PRIVATE_TOURNAMENT_MONTHLY_LIMIT,
    }
    revision = max(
        _revision_timestamp(getattr(user, "updated_at", None)),
        _revision_timestamp(profile_updated_at if profile_id is not None else None),
        _revision_timestamp(steam_linked_at if steam_identity_id is not None else None),
        _revision_timestamp(
            password_credential_updated_at
            if password_credential_id is not None
            else None
        ),
        _revision_timestamp(getattr(avatar_asset, "updated_at", None)),
    )
    return revision, payload


async def _write(
    user_id: str,
    *,
    generation: str,
    revision: int,
    payload: dict[str, Any],
) -> bool | None:
    client = redis_client(decode_responses=False)
    try:
        result = await client.eval(
            _SET_IF_CURRENT_GENERATION_SCRIPT,
            1,
            user_account_read_model_key(user_id),
            generation,
            str(int(revision)),
            _encode(revision=revision, generation=generation, payload=payload),
            str(USER_ACCOUNT_READ_MODEL_SAFETY_TTL_SECONDS),
        )
        return int(result) == 1
    except _REDIS_UNAVAILABLE as exc:
        logger.warning(
            "Redis user account read-model write failed user_id=%s error=%s",
            user_id,
            type(exc).__name__,
        )
        return None
    finally:
        await client.aclose()


async def get_or_build_user_account_read_model(
    db_session: AsyncSession,
    *,
    user: User,
    now: datetime,
) -> UserAccountReadModelLookup:
    """Read cached supplemental fields or build them from the current session."""

    client = redis_client(decode_responses=False)
    get_ms = 0.0
    build_ms = 0.0
    outcome = "other"
    generation: str | None = None
    lookup_returned = False
    try:
        get_started = perf_counter()
        try:
            generated_token = secrets.token_urlsafe(18)
            reservation = await client.eval(
                _RESERVE_GENERATION_SCRIPT,
                1,
                user_account_read_model_key(user.id),
                _encode_pending(generation=generated_token),
                str(USER_ACCOUNT_READ_MODEL_SAFETY_TTL_SECONDS),
            )
        except _REDIS_UNAVAILABLE as exc:
            logger.warning(
                "Redis user account read-model read failed user_id=%s error=%s",
                user.id,
                type(exc).__name__,
            )
            reservation = None
        finally:
            get_ms = (perf_counter() - get_started) * 1000
            await client.aclose()
        decoded = _decode(reservation)
        if decoded is not None and decoded[0] == "ready":
            _, generation, _revision, cached = decoded
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
            outcome = "hit"
            lookup = UserAccountReadModelLookup(
                account=cached,
                get_ms=get_ms,
                build_ms=0.0,
                outcome=outcome,
            )
            lookup_returned = True
            return lookup
        if decoded is not None and decoded[0] == "pending":
            _, generation = decoded

        build_started = perf_counter()
        try:
            revision, payload = await build_user_account_read_model(
                db_session,
                user=user,
                now=now,
            )
        except BaseException:
            outcome = "build_error"
            raise
        finally:
            # This is wall time across the async builder, not CPU time.
            build_ms = (perf_counter() - build_started) * 1000

        if generation is None:
            outcome = "read_error"
        else:
            outcome = "miss"
        logger.debug(
            "user_account_read_model_built user_id=%s build_ms=%.2f",
            user.id,
            build_ms,
        )
        lookup = UserAccountReadModelLookup(
            account=payload,
            get_ms=get_ms,
            build_ms=build_ms,
            outcome=outcome,
            pending_generation=generation,
            pending_revision=revision if generation is not None else None,
            pending_payload=payload if generation is not None else None,
        )
        lookup_returned = True
        return lookup
    finally:
        if not lookup_returned:
            record_redis_read_model_event(
                model="user_account",
                outcome=outcome,
                get_ms=get_ms,
                build_ms=build_ms,
                set_ms=0.0,
                include_payload_metadata=False,
            )


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
