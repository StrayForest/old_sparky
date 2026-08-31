from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
import json
import logging
from time import perf_counter
from typing import Any, Literal

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.db import session_factory
from python_packages.platform_infra.models import Tournament
from python_packages.platform_infra.performance import record_redis_read_model_event
from python_packages.platform_infra.redis import redis_client

logger = logging.getLogger(__name__)

ReadModelKind = Literal[
    "teams",
    "workspace_detail",
    "bracket_summary",
    "bracket_full",
]

READ_MODEL_KEY_PREFIX = "platform:tournament:read-model:v1"
# This is a safety bound only. Consistency is controlled by the revision in
# the envelope and by the CAS write, never by TTL expiry.
READ_MODEL_SAFETY_TTL_SECONDS = 7 * 24 * 60 * 60
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
class ReadModelEnvelope:
    revision: int
    payload: bytes


def read_model_key(
    tournament_id: str,
    model: ReadModelKind,
) -> str:
    return f"{READ_MODEL_KEY_PREFIX}:{model}:{tournament_id}"


def _encode_envelope(*, revision: int, payload: bytes) -> bytes:
    return f"{int(revision)}\n".encode("ascii") + payload


def _decode_envelope(raw: bytes | str | None) -> ReadModelEnvelope | None:
    if raw is None:
        return None
    raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    revision_bytes, separator, payload = raw_bytes.partition(b"\n")
    if not separator:
        return None
    try:
        revision = int(revision_bytes)
    except (TypeError, ValueError):
        return None
    return ReadModelEnvelope(revision=revision, payload=payload)


def _serialize_payload(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    model_dump_json = getattr(value, "model_dump_json", None)
    if callable(model_dump_json):
        return str(model_dump_json()).encode("utf-8")
    def json_default(item: Any) -> Any:
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="json")
        return str(item)

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=json_default,
    ).encode("utf-8")


async def _redis_get(
    *,
    tournament_id: str,
    model: ReadModelKind,
    revision: int,
) -> tuple[ReadModelEnvelope | None, float, bool]:
    key = read_model_key(tournament_id, model)
    started_at = perf_counter()
    client = redis_client(decode_responses=False)
    try:
        envelope = _decode_envelope(await client.get(key))
        get_ms = (perf_counter() - started_at) * 1000
        if envelope is not None and envelope.revision == int(revision):
            record_redis_read_model_event(
                model=model,
                outcome="hit",
                get_ms=get_ms,
                payload_bytes=len(envelope.payload),
                revision=envelope.revision,
            )
            return envelope, get_ms, True
        record_redis_read_model_event(
            model=model,
            outcome="miss",
            get_ms=get_ms,
            payload_bytes=len(envelope.payload) if envelope is not None else 0,
            revision=revision,
        )
        return envelope, get_ms, False
    except _REDIS_UNAVAILABLE as exc:
        get_ms = (perf_counter() - started_at) * 1000
        logger.warning(
            "Redis read-model GET failed model=%s tournament_id=%s error=%s",
            model,
            tournament_id,
            type(exc).__name__,
        )
        record_redis_read_model_event(
            model=model,
            outcome="error",
            get_ms=get_ms,
            revision=revision,
        )
        return None, get_ms, False
    finally:
        await client.aclose()


async def _redis_set_if_newer(
    *,
    tournament_id: str,
    model: ReadModelKind,
    revision: int,
    payload: bytes,
) -> tuple[bool, float]:
    key = read_model_key(tournament_id, model)
    envelope = _encode_envelope(revision=revision, payload=payload)
    started_at = perf_counter()
    client = redis_client(decode_responses=False)
    try:
        stored = bool(
            await client.eval(
                _SET_IF_NEWER_SCRIPT,
                1,
                key,
                str(int(revision)),
                envelope,
                str(READ_MODEL_SAFETY_TTL_SECONDS),
            )
        )
        set_ms = (perf_counter() - started_at) * 1000
        record_redis_read_model_event(
            model=model,
            outcome="write" if stored else "stale_write_skipped",
            set_ms=set_ms,
            payload_bytes=len(payload),
            revision=revision,
        )
        return stored, set_ms
    except _REDIS_UNAVAILABLE as exc:
        set_ms = (perf_counter() - started_at) * 1000
        logger.warning(
            "Redis read-model SET failed model=%s tournament_id=%s error=%s",
            model,
            tournament_id,
            type(exc).__name__,
        )
        record_redis_read_model_event(
            model=model,
            outcome="error",
            set_ms=set_ms,
            payload_bytes=len(payload),
            revision=revision,
        )
        return False, set_ms
    finally:
        await client.aclose()


async def read_model_read_or_build(
    *,
    tournament_id: str,
    model: ReadModelKind,
    revision: int,
    builder: Callable[[], Awaitable[Any]],
) -> bytes:
    """Read one serialized representation, falling back to PostgreSQL.

    Authentication and authorization are intentionally performed by callers
    before invoking this function. A revision-matching Redis hit never calls
    the DB builder.
    """

    envelope, _get_ms, hit = await _redis_get(
        tournament_id=tournament_id,
        model=model,
        revision=revision,
    )
    if hit and envelope is not None:
        return envelope.payload

    build_started_at = perf_counter()
    value = await builder()
    payload = _serialize_payload(value)
    build_ms = (perf_counter() - build_started_at) * 1000
    record_redis_read_model_event(
        model=model,
        outcome="build",
        build_ms=build_ms,
        payload_bytes=len(payload),
        revision=revision,
    )
    stored, _set_ms = await _redis_set_if_newer(
        tournament_id=tournament_id,
        model=model,
        revision=revision,
        payload=payload,
    )
    if not stored:
        # A Redis outage is a normal DB-backed response path. Do not make a
        # read fail merely because the shared cache cannot be written.
        record_redis_read_model_event(
            model=model,
            outcome="fallback_db",
            payload_bytes=len(payload),
            revision=revision,
        )
    return payload


async def delete_tournament_read_models(
    tournament_id: str,
    models: Iterable[ReadModelKind],
) -> None:
    client = redis_client(decode_responses=False)
    keys = [read_model_key(tournament_id, model) for model in models]
    if not keys:
        await client.aclose()
        return
    try:
        await client.delete(*keys)
    except _REDIS_UNAVAILABLE as exc:
        logger.warning(
            "Redis read-model DELETE failed tournament_id=%s error=%s",
            tournament_id,
            type(exc).__name__,
        )
    finally:
        await client.aclose()


async def _build_selected_read_models_from_authoritative_db(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    selected: tuple[ReadModelKind, ...],
) -> dict[ReadModelKind, Any]:
    """Build selected projections from one bounded team/match/profile load."""

    from apps.platform_api.app.api.routes import tournaments as tournament_routes
    from apps.platform_api.app.api.schemas import TournamentBracketResponse
    from apps.platform_api.app.services.tournament_teams import load_tournament_team_state

    needs_teams = bool(set(selected) & {"teams", "workspace_detail", "bracket_summary", "bracket_full"})
    team_rows = []
    member_rows = []
    member_profiles: dict[str, Any] = {}
    if needs_teams:
        team_rows, member_rows = await load_tournament_team_state(
            db_session,
            tournament_id=tournament.id,
            include_members=True,
        )
        if member_rows:
            member_profiles = await tournament_routes.deadlock_assignment_member_profiles(
                db_session,
                member_rows,
            )
    full_teams = tournament_routes.serialize_deadlock_bracket_teams(
        team_rows,
        member_rows,
        include_members=True,
        member_profiles=member_profiles,
    )
    summary_teams = tournament_routes.serialize_deadlock_bracket_teams(
        team_rows,
        (),
        include_members=False,
    )
    match_rows = []
    if set(selected) & {"bracket_summary", "bracket_full"}:
        match_rows = await tournament_routes.tournament_matches_in_order(
            db_session,
            tournament_id=tournament.id,
        )
    total_rounds = max((match.round_number for match in match_rows), default=1)
    bracket_status = "ready" if match_rows else "teams_ready" if full_teams else "pending"
    capabilities = tournament_routes.bracket_capabilities(
        tournament_status=tournament.status,
        can_manage=False,
    )
    result: dict[ReadModelKind, Any] = {}
    if "teams" in selected:
        result["teams"] = full_teams
    if "workspace_detail" in selected:
        result["workspace_detail"] = TournamentBracketResponse(
            tournament_id=tournament.id,
            tournament_status=tournament.status,
            status=(
                "pending"
                if tournament.status == "registration_open"
                else ("ready" if int(tournament.bracket_revision or 0) > 0 else "teams_ready" if full_teams else "pending")
            ),
            revision=int(tournament.bracket_revision or 0),
            can_manage=False,
            capabilities=capabilities,
            teams=[] if tournament.status == "registration_open" else full_teams,
            matches=[],
        )
    for model, teams in (("bracket_summary", summary_teams), ("bracket_full", full_teams)):
        if model not in selected:
            continue
        result[model] = TournamentBracketResponse(
            tournament_id=tournament.id,
            tournament_status=tournament.status,
            status=bracket_status,
            revision=int(tournament.bracket_revision or 0),
            can_manage=False,
            capabilities=capabilities,
            teams=teams,
            matches=[
                tournament_routes.serialize_bracket_match_projection(
                    match,
                    tournament=tournament,
                    total_rounds=total_rounds,
                )
                for match in match_rows
            ],
        )
    return result


async def refresh_tournament_read_models(
    tournament_id: str,
    models: Iterable[ReadModelKind],
) -> None:
    """Rebuild selected models after a successful authoritative commit."""

    selected = tuple(dict.fromkeys(models))
    if not selected:
        return
    try:
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(
                select(Tournament).where(Tournament.id == str(tournament_id))
            )
            if tournament is None:
                await delete_tournament_read_models(tournament_id, selected)
                return
            from apps.platform_api.app.api.routes.tournaments import tournament_state_version

            revision = tournament_state_version(tournament)
            values = await _build_selected_read_models_from_authoritative_db(
                db_session,
                tournament=tournament,
                selected=selected,
            )
            for model, value in values.items():
                payload = _serialize_payload(value)
                await _redis_set_if_newer(
                    tournament_id=tournament_id,
                    model=model,
                    revision=revision,
                    payload=payload,
                )
    except Exception:
        logger.exception(
            "Failed to open authoritative DB for tournament read-model refresh "
            "tournament_id=%s models=%s",
            tournament_id,
            selected,
        )
