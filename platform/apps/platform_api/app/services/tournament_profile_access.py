from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from time import perf_counter
from typing import Any

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

PROFILE_ACCESS_KEY_PREFIX = "platform:tournament:profile-access:v1"
PROFILE_VIEWERS_KEY_PREFIX = "platform:tournament:profile-viewers:v1"
PROFILE_ROSTER_KEY_PREFIX = "platform:tournament:profile-roster:v1"
PROFILE_ACCESS_SAFETY_TTL_SECONDS = 7 * 24 * 60 * 60
INACTIVE_PARTICIPANT_STATUSES = ("withdrawn", "disqualified")
_REDIS_UNAVAILABLE = (RedisError, OSError, asyncio.TimeoutError)

_SET_ACCESS_IF_NEWER_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current then
    local current_revision = string.match(current, '"revision":(%d+)')
    if current_revision and tonumber(current_revision) > tonumber(ARGV[1]) then
        return 0
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


@dataclass(frozen=True, slots=True)
class TournamentProfileAccessState:
    tournament_id: str
    organizer_user_id: str
    roster_ready: bool
    revision: int
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
            "revision": state.revision,
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
            revision=int(value["revision"]),
            viewer_user_ids=frozenset(),
            roster_user_ids=frozenset(),
        )
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _ids_from_aggregate(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    return frozenset(str(item) for item in value if item is not None)


def _state_revision(
    tournament: Tournament,
    *,
    active_participant_count: int,
) -> int:
    updated_at = tournament.updated_at or tournament.created_at
    updated_ms = int(updated_at.timestamp() * 1000) if updated_at is not None else 0
    return (
        updated_ms
        + int(tournament.bracket_revision or 0) * 1_000_000
        + max(0, int(active_participant_count))
    )


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
    active_participant_count = (
        select(func.count(TournamentParticipant.id))
        .where(
            TournamentParticipant.tournament_id == Tournament.id,
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
        )
        .correlate(Tournament)
        .scalar_subquery()
    )
    row = (
        await db_session.execute(
            select(
                Tournament,
                active_viewer_ids.label("active_viewer_ids"),
                roster_user_ids.label("roster_user_ids"),
                roster_ready.label("roster_ready"),
                active_participant_count.label("active_participant_count"),
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
        revision=_state_revision(
            tournament,
            active_participant_count=int(row.active_participant_count or 0),
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
                str(state.revision),
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
            revision=state.revision,
        )
    except _REDIS_UNAVAILABLE as exc:
        record_tournament_profile_access_event(
            "tournament_profile_access_redis_error",
            pipeline_ms=(perf_counter() - started_at) * 1000,
            revision=state.revision,
        )
        logger.warning(
            "Redis profile access refresh failed slug=%s error=%s",
            slug,
            type(exc).__name__,
        )
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
            revision=state.revision if state is not None else None,
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
