from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from time import perf_counter
from typing import Any, Awaitable

from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.db import session_factory
from python_packages.platform_infra.models import (
    Tournament,
    TournamentParticipant,
    TournamentTeamMember,
)
from python_packages.platform_infra.performance import (
    record_profile_read_model_event,
    record_tournament_profile_access_event,
)
from python_packages.platform_infra.redis import redis_client

logger = logging.getLogger(__name__)

PROFILE_ACCESS_KEY_PREFIX = "platform:tournament:profile-access:v2"
PROFILE_VIEWERS_KEY_PREFIX = "platform:tournament:profile-viewers:v2"
PROFILE_ROSTER_KEY_PREFIX = "platform:tournament:profile-roster:v2"
LEGACY_PROFILE_ACCESS_KEY_PREFIX = "platform:tournament:profile-access:v1"
LEGACY_PROFILE_VIEWERS_KEY_PREFIX = "platform:tournament:profile-viewers:v1"
LEGACY_PROFILE_ROSTER_KEY_PREFIX = "platform:tournament:profile-roster:v1"
PROFILE_ACCESS_SAFETY_TTL_SECONDS = 7 * 24 * 60 * 60
PROFILE_ACCESS_PURGE_SCAN_COUNT = 256
PROFILE_ACCESS_PURGE_MAX_KEYS = 100_000
PROFILE_ACCESS_PURGE_MAX_SCAN_PAGES = 10_000
PROFILE_ACCESS_PURGE_TIMEOUT_SECONDS = 30.0
PROFILE_ACCESS_PURGE_UNLINK_BATCH_SIZE = 256
INACTIVE_PARTICIPANT_STATUSES = ("withdrawn", "disqualified")
_REDIS_UNAVAILABLE = (RedisError, OSError, asyncio.TimeoutError)
_V1_PROFILE_ACCESS_PREFIXES = (
    LEGACY_PROFILE_ACCESS_KEY_PREFIX,
    LEGACY_PROFILE_VIEWERS_KEY_PREFIX,
    LEGACY_PROFILE_ROSTER_KEY_PREFIX,
)
_V2_PROFILE_ACCESS_PREFIXES = (
    PROFILE_ACCESS_KEY_PREFIX,
    PROFILE_VIEWERS_KEY_PREFIX,
    PROFILE_ROSTER_KEY_PREFIX,
)

_SET_ACCESS_IF_NEWER_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current then
    local current_generation = string.match(current, '"profile_access_generation":(%d+)')
    if current_generation then
        local incoming_generation = ARGV[1]
        if #current_generation > #incoming_generation
            or (#current_generation == #incoming_generation and current_generation >= incoming_generation) then
            return 0
        end
    end
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
redis.call('DEL', KEYS[2], KEYS[3])
local viewer_count = tonumber(ARGV[4])
local roster_count = tonumber(ARGV[5])
local index = 6
for _ = 1, viewer_count do
    redis.call('SADD', KEYS[2], ARGV[index])
    index = index + 1
end
if viewer_count > 0 then
    redis.call('EXPIRE', KEYS[2], ARGV[3])
end
for _ = 1, roster_count do
    redis.call('SADD', KEYS[3], ARGV[index])
    index = index + 1
end
if roster_count > 0 then
    redis.call('EXPIRE', KEYS[3], ARGV[3])
end
return 1
"""

# A self-join changes exactly one field in a warmed authorization snapshot:
# the joined user becomes a viewer.  Keep this operation conditional on the
# prior DB generation and on the cached set cardinalities so a partial/evicted
# projection falls back to the authoritative one-statement builder.
_ADD_JOINED_VIEWER_IF_CURRENT_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then
    return 0
end

local current_generation = string.match(current, '"profile_access_generation":(%d+)')
if not current_generation or current_generation ~= ARGV[1] then
    return 0
end
local _, generation_fields = string.gsub(current, '"profile_access_generation":', '')
if generation_fields ~= 1 then
    return 0
end

local viewer_count_raw = string.match(current, '"viewer_count":(%d+)')
local roster_count_raw = string.match(current, '"roster_count":(%d+)')
if not viewer_count_raw or not roster_count_raw then
    return 0
end
local _, viewer_count_fields = string.gsub(current, '"viewer_count":', '')
local _, roster_count_fields = string.gsub(current, '"roster_count":', '')
if viewer_count_fields ~= 1 or roster_count_fields ~= 1 then
    return 0
end
local viewer_count = tonumber(viewer_count_raw)
local roster_count = tonumber(roster_count_raw)
if not viewer_count or not roster_count
    or viewer_count < 0 or roster_count < 0
    or math.floor(viewer_count) ~= viewer_count
    or math.floor(roster_count) ~= roster_count then
    return 0
end
local roster_ready = string.find(current, '"roster_ready":true', 1, true)
local roster_not_ready = string.find(current, '"roster_ready":false', 1, true)
if (roster_ready and roster_not_ready)
    or (not roster_ready and not roster_not_ready)
    or (roster_ready and roster_count == 0)
    or (roster_not_ready and roster_count > 0) then
    return 0
end

local function set_matches(key, expected_count)
    local key_type = redis.call('TYPE', key).ok
    if expected_count == 0 then
        return key_type == 'none' or (key_type == 'set' and redis.call('SCARD', key) == 0)
    end
    return key_type == 'set' and redis.call('SCARD', key) == expected_count
end

if not set_matches(KEYS[2], viewer_count) or not set_matches(KEYS[3], roster_count) then
    return 0
end
if redis.call('SISMEMBER', KEYS[2], ARGV[3]) ~= 0 then
    return 0
end

local updated, generation_replacements = string.gsub(
    current,
    '"profile_access_generation":%d+',
    '"profile_access_generation":' .. ARGV[2],
    1
)
if generation_replacements ~= 1 then
    return 0
end
local updated_with_count, count_replacements = string.gsub(
    updated,
    '"viewer_count":%d+',
    '"viewer_count":' .. tostring(viewer_count + 1),
    1
)
if count_replacements ~= 1 then
    return 0
end

-- SADD comes before the access document's generation advance.  If a later
-- Redis write fails, the old generation remains and the reader rejects this
-- partial projection against its fresh DB generation.
if redis.call('SADD', KEYS[2], ARGV[3]) ~= 1 then
    return 0
end
redis.call('SET', KEYS[1], updated_with_count, 'EX', ARGV[4])
redis.call('EXPIRE', KEYS[2], ARGV[4])
if roster_count > 0 then
    redis.call('EXPIRE', KEYS[3], ARGV[4])
end
return 1
"""


@dataclass(frozen=True, slots=True)
class TournamentProfileAccessState:
    tournament_id: str
    organizer_user_id: str
    roster_ready: bool
    profile_access_generation: int
    viewer_user_ids: frozenset[str]
    roster_user_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class TournamentProfilePipelineResult:
    access_raw: bytes | str | None
    requester_is_viewer: bool
    target_is_roster_member: bool
    profile_raw: bytes | str | None
    redis_available: bool
    pipeline_ms: float


def profile_access_key(slug: str) -> str:
    return f"{PROFILE_ACCESS_KEY_PREFIX}:{slug}"


def profile_viewers_key(slug: str) -> str:
    return f"{PROFILE_VIEWERS_KEY_PREFIX}:{slug}"


def profile_roster_key(slug: str) -> str:
    return f"{PROFILE_ROSTER_KEY_PREFIX}:{slug}"


def _state_payload(state: TournamentProfileAccessState) -> bytes:
    return json.dumps(
        {
            "tournament_id": state.tournament_id,
            "organizer_user_id": state.organizer_user_id,
            "roster_ready": state.roster_ready,
            "profile_access_generation": state.profile_access_generation,
            "viewer_count": len(state.viewer_user_ids),
            "roster_count": len(state.roster_user_ids),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_profile_access_state(
    raw: bytes | str | None,
) -> TournamentProfileAccessState | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return TournamentProfileAccessState(
            tournament_id=str(value["tournament_id"]),
            organizer_user_id=str(value["organizer_user_id"]),
            roster_ready=bool(value["roster_ready"]),
            profile_access_generation=_decode_profile_access_generation(
                value.get("profile_access_generation")
            ),
            viewer_user_ids=frozenset(),
            roster_user_ids=frozenset(),
        )
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _ids_from_aggregate(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    return frozenset(str(item) for item in value if item is not None)


def _decode_profile_access_generation(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("profile access generation is invalid")
    return value


async def _build_state_with_session(
    db_session: AsyncSession,
    slug: str,
) -> TournamentProfileAccessState | None:
    active_viewer_ids = (
        select(func.array_agg(TournamentParticipant.user_id))
        .where(
            TournamentParticipant.tournament_id == Tournament.id,
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
        )
        .correlate(Tournament)
        .scalar_subquery()
    )
    roster_user_ids = (
        select(func.array_agg(TournamentTeamMember.user_id))
        .where(TournamentTeamMember.tournament_id == Tournament.id)
        .correlate(Tournament)
        .scalar_subquery()
    )
    roster_ready = (
        select(TournamentTeamMember.id)
        .where(TournamentTeamMember.tournament_id == Tournament.id)
        .limit(1)
        .correlate(Tournament)
        .exists()
    )
    row = (
        await db_session.execute(
            select(
                Tournament,
                active_viewer_ids.label("active_viewer_ids"),
                roster_user_ids.label("roster_user_ids"),
                roster_ready.label("roster_ready"),
            ).where(Tournament.slug == slug)
        )
    ).one_or_none()
    if row is None:
        return None

    tournament = row[0]
    viewer_ids = _ids_from_aggregate(row.active_viewer_ids)
    roster_ids = _ids_from_aggregate(row.roster_user_ids)
    return TournamentProfileAccessState(
        tournament_id=str(tournament.id),
        organizer_user_id=str(tournament.organizer_user_id),
        roster_ready=bool(row.roster_ready),
        profile_access_generation=_decode_profile_access_generation(
            tournament.profile_access_generation
        ),
        viewer_user_ids=viewer_ids,
        roster_user_ids=roster_ids,
    )


async def build_tournament_profile_access_state(
    slug: str,
    db_session: AsyncSession | None = None,
) -> TournamentProfileAccessState | None:
    """Build tournament profile authorization state with one DB statement."""

    if db_session is not None:
        return await _build_state_with_session(db_session, slug)
    async with session_factory()() as owned_session:
        return await _build_state_with_session(owned_session, slug)


async def _write_profile_access_state(
    slug: str,
    state: TournamentProfileAccessState,
) -> None:
    client = redis_client(decode_responses=False)
    started_at = perf_counter()
    try:
        viewer_ids = tuple(sorted(state.viewer_user_ids))
        roster_ids = tuple(sorted(state.roster_user_ids))
        stored = bool(
            await client.eval(
                _SET_ACCESS_IF_NEWER_SCRIPT,
                3,
                profile_access_key(slug),
                profile_viewers_key(slug),
                profile_roster_key(slug),
                str(state.profile_access_generation),
                _state_payload(state),
                str(PROFILE_ACCESS_SAFETY_TTL_SECONDS),
                str(len(viewer_ids)),
                str(len(roster_ids)),
                *viewer_ids,
                *roster_ids,
            )
        )
        record_tournament_profile_access_event(
            "tournament_profile_access_write"
            if stored
            else "tournament_profile_access_stale_write_skipped",
            pipeline_ms=(perf_counter() - started_at) * 1000,
            payload_bytes=len(_state_payload(state)),
            revision=state.profile_access_generation,
        )
    except _REDIS_UNAVAILABLE as exc:
        record_tournament_profile_access_event(
            "tournament_profile_access_redis_error",
            pipeline_ms=(perf_counter() - started_at) * 1000,
            revision=state.profile_access_generation,
        )
        logger.warning(
            "Redis profile access refresh failed slug=%s error=%s",
            slug,
            type(exc).__name__,
        )
    finally:
        await client.aclose()


async def add_joined_tournament_profile_viewer(
    slug: str,
    user_id: str,
    *,
    expected_generation: int | None,
) -> bool:
    """Advance a warmed profile projection for one committed solo self-join.

    The route supplies the generation read after taking the Tournament row
    lock.  Any missing, stale, malformed, or incomplete cache returns False so
    the caller can use the existing authoritative full-snapshot refresh.
    """

    if (
        type(expected_generation) is not int
        or expected_generation < 0
        or expected_generation >= 9_223_372_036_854_775_807
        or not user_id
    ):
        return False

    client = redis_client(decode_responses=False)
    started_at = perf_counter()
    next_generation = expected_generation + 1
    try:
        updated = bool(
            await client.eval(
                _ADD_JOINED_VIEWER_IF_CURRENT_SCRIPT,
                3,
                profile_access_key(slug),
                profile_viewers_key(slug),
                profile_roster_key(slug),
                str(expected_generation),
                str(next_generation),
                user_id,
                str(PROFILE_ACCESS_SAFETY_TTL_SECONDS),
            )
        )
        record_tournament_profile_access_event(
            "tournament_profile_access_join_delta"
            if updated
            else "tournament_profile_access_join_delta_fallback",
            pipeline_ms=(perf_counter() - started_at) * 1000,
            revision=next_generation,
        )
        return updated
    except _REDIS_UNAVAILABLE as exc:
        record_tournament_profile_access_event(
            "tournament_profile_access_redis_error",
            pipeline_ms=(perf_counter() - started_at) * 1000,
            revision=next_generation,
        )
        logger.warning(
            "Redis profile access join delta failed slug=%s error=%s",
            slug,
            type(exc).__name__,
        )
        return False
    finally:
        await client.aclose()


async def refresh_tournament_profile_access_state(
    slug: str,
    db_session: AsyncSession | None = None,
) -> TournamentProfileAccessState | None:
    started_at = perf_counter()
    try:
        state = await build_tournament_profile_access_state(slug, db_session)
        record_tournament_profile_access_event(
            "tournament_profile_access_build",
            build_ms=(perf_counter() - started_at) * 1000,
            payload_bytes=len(_state_payload(state)) if state is not None else 0,
            revision=(
                state.profile_access_generation if state is not None else None
            ),
        )
        if state is not None:
            await _write_profile_access_state(slug, state)
        return state
    except Exception:
        logger.exception("Tournament profile access build failed slug=%s", slug)
        return None


async def delete_tournament_profile_access_state(slug: str) -> None:
    client = redis_client(decode_responses=False)
    try:
        await client.delete(
            profile_access_key(slug),
            profile_viewers_key(slug),
            profile_roster_key(slug),
        )
    except _REDIS_UNAVAILABLE as exc:
        logger.warning(
            "Redis profile access delete failed slug=%s error=%s",
            slug,
            type(exc).__name__,
        )
    finally:
        await client.aclose()


async def _purge_profile_access_prefixes(prefixes: tuple[str, ...]) -> int:
    """Remove a fixed set of profile-cache namespaces while writers are stopped.

    Release/restore callers own the service-stop and rollback locks. This
    helper accepts no caller-provided key or prefix, bounds the Redis scan,
    removes only discovered keys under the exact namespaces, and verifies that
    those namespaces are empty before returning.
    """

    client = redis_client(decode_responses=False)
    started_at = perf_counter()
    scanned_pages = 0
    discovered: set[bytes] = set()

    def remaining_seconds() -> float:
        remaining = PROFILE_ACCESS_PURGE_TIMEOUT_SECONDS - (perf_counter() - started_at)
        if remaining <= 0:
            raise RuntimeError("profile access cache purge exceeded its time bound")
        return remaining

    async def bounded(awaitable: Awaitable[Any]) -> Any:
        try:
            return await asyncio.wait_for(awaitable, timeout=remaining_seconds())
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                "profile access cache purge exceeded its time bound"
            ) from exc

    async def collect_namespace_keys() -> set[bytes]:
        nonlocal scanned_pages
        found: set[bytes] = set()
        for prefix in prefixes:
            expected_prefix = f"{prefix}:".encode("ascii")
            cursor: int | bytes | str = 0
            while True:
                scanned_pages += 1
                if (
                    scanned_pages > PROFILE_ACCESS_PURGE_MAX_SCAN_PAGES
                    or perf_counter() - started_at
                    > PROFILE_ACCESS_PURGE_TIMEOUT_SECONDS
                ):
                    raise RuntimeError("profile access cache purge exceeded its scan bound")
                cursor, keys = await bounded(
                    client.scan(
                        cursor=cursor,
                        match=f"{prefix}:*",
                        count=PROFILE_ACCESS_PURGE_SCAN_COUNT,
                    )
                )
                for key in keys:
                    raw_key = (
                        key if isinstance(key, bytes) else str(key).encode("utf-8")
                    )
                    if not raw_key.startswith(expected_prefix):
                        raise RuntimeError("profile access cache scan returned an unexpected key")
                    found.add(raw_key)
                    if len(discovered | found) > PROFILE_ACCESS_PURGE_MAX_KEYS:
                        raise RuntimeError(
                            "profile access cache purge exceeded its key bound"
                        )
                if cursor in (0, b"0", "0"):
                    break
        return found

    try:
        discovered = await collect_namespace_keys()
        keys = tuple(discovered)
        for offset in range(0, len(keys), PROFILE_ACCESS_PURGE_UNLINK_BATCH_SIZE):
            await bounded(
                client.unlink(
                    *keys[offset : offset + PROFILE_ACCESS_PURGE_UNLINK_BATCH_SIZE]
                )
            )

        remaining = await collect_namespace_keys()
        if remaining:
            raise RuntimeError(
                "profile access cache purge could not prove empty namespaces"
            )
        return len(discovered)
    except _REDIS_UNAVAILABLE as exc:
        raise RuntimeError("profile access cache purge failed") from exc
    finally:
        await client.aclose()


async def purge_legacy_profile_access_cache() -> int:
    """Purge only v1 profile-access keys before restoring the v1 application."""

    return await _purge_profile_access_prefixes(_V1_PROFILE_ACCESS_PREFIXES)


async def purge_all_tournament_profile_access_cache() -> int:
    """Purge v1 and v2 profile-access keys after restoring the database."""

    return await _purge_profile_access_prefixes(
        (*_V1_PROFILE_ACCESS_PREFIXES, *_V2_PROFILE_ACCESS_PREFIXES)
    )


async def read_tournament_profile_pipeline(
    *,
    slug: str,
    current_user_id: str,
    target_user_id: str,
    profile_key: str,
) -> TournamentProfilePipelineResult:
    """Read access, membership and profile state in one Redis pipeline."""

    client = redis_client(decode_responses=False)
    started_at = perf_counter()
    try:
        async with client.pipeline(transaction=False) as pipeline:
            pipeline.get(profile_access_key(slug))
            pipeline.sismember(profile_viewers_key(slug), current_user_id)
            pipeline.sismember(profile_roster_key(slug), target_user_id)
            pipeline.get(profile_key)
            values = await pipeline.execute()
        pipeline_ms = (perf_counter() - started_at) * 1000
        record_tournament_profile_access_event(
            "tournament_profile_access_pipeline",
            pipeline_ms=pipeline_ms,
        )
        return TournamentProfilePipelineResult(
            access_raw=values[0],
            requester_is_viewer=bool(values[1]),
            target_is_roster_member=bool(values[2]),
            profile_raw=values[3],
            redis_available=True,
            pipeline_ms=pipeline_ms,
        )
    except _REDIS_UNAVAILABLE as exc:
        pipeline_ms = (perf_counter() - started_at) * 1000
        record_tournament_profile_access_event(
            "tournament_profile_access_redis_error",
            pipeline_ms=pipeline_ms,
        )
        record_profile_read_model_event(
            "profile_read_model_redis_error",
            pipeline_ms=pipeline_ms,
        )
        logger.warning(
            "Redis tournament profile pipeline failed slug=%s error=%s",
            slug,
            type(exc).__name__,
        )
        return TournamentProfilePipelineResult(
            access_raw=None,
            requester_is_viewer=False,
            target_is_roster_member=False,
            profile_raw=None,
            redis_available=False,
            pipeline_ms=pipeline_ms,
        )
    finally:
        await client.aclose()
