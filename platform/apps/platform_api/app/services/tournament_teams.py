from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_domain.tournaments import TournamentWorkflowError
from python_packages.platform_infra.models import (
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentTeam,
    TournamentTeamMember,
    new_uuid,
)


class TournamentTeamMaterializationError(TournamentWorkflowError):
    """The assignment snapshot cannot become a valid current team state."""


@dataclass(frozen=True, slots=True)
class _ParsedTeam:
    team_key: str
    name: str
    captain_user_id: str | None
    starter_strength: float
    starter_average_strength: float
    members: tuple[dict[str, Any], ...]


def _required_team_key(raw_team: dict[str, Any]) -> str:
    team_key = str(raw_team.get("team_id") or "").strip()
    if not team_key:
        raise TournamentTeamMaterializationError(
            "Every assignment team must have a stable team id."
        )
    return team_key


def _number(value: Any, *, field_name: str, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise TournamentTeamMaterializationError(
            f"Assignment team field {field_name} must be numeric."
        ) from exc
    if not isfinite(parsed) or parsed < 0:
        raise TournamentTeamMaterializationError(
            f"Assignment team field {field_name} must be finite and nonnegative."
        )
    return parsed


def _integer(value: Any, *, field_name: str, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise TournamentTeamMaterializationError(
            f"Assignment team field {field_name} must be an integer."
        ) from exc


def _player_user_id(player: Any, *, field_name: str) -> str | None:
    if player is None:
        return None
    if not isinstance(player, dict):
        raise TournamentTeamMaterializationError(
            f"Assignment team field {field_name} must contain a player object."
        )
    user_id = str(player.get("user_id") or "").strip()
    if not user_id:
        raise TournamentTeamMaterializationError(
            f"Assignment team field {field_name} must contain a user id."
        )
    return user_id


def _member_payload(
    player: dict[str, Any],
    *,
    team_key: str,
    slot_number: int,
    roster_role: str,
    assigned_role: Any,
) -> dict[str, Any]:
    user_id = _player_user_id(player, field_name=f"{team_key}.{roster_role}")
    assert user_id is not None
    subrank = _integer(
        player.get("subrank"),
        field_name=f"{team_key}.{roster_role}.subrank",
    )
    if subrank is not None and subrank <= 0:
        raise TournamentTeamMaterializationError(
            f"Assignment team field {team_key}.{roster_role}.subrank must be positive."
        )
    return {
        "user_id": user_id,
        "slot_number": slot_number,
        "roster_role": roster_role,
        "assigned_role": (
            str(assigned_role).strip() if assigned_role is not None else None
        )
        or None,
        "strength": _number(
            player.get("strength"),
            field_name=f"{team_key}.{roster_role}.strength",
        ),
        "rank": str(player.get("rank") or "").strip() or None,
        "subrank": subrank,
    }


def _parse_snapshot_teams(run_row: TournamentDeadlockAssignmentRun) -> tuple[_ParsedTeam, ...]:
    raw_snapshot = run_row.result_snapshot
    if raw_snapshot is None:
        snapshot: dict[str, Any] = {}
    elif isinstance(raw_snapshot, dict):
        snapshot = dict(raw_snapshot)
    else:
        raise TournamentTeamMaterializationError(
            "The assignment result snapshot must be an object."
        )
    raw_teams = snapshot.get("teams")
    if not isinstance(raw_teams, list) or not raw_teams:
        raise TournamentTeamMaterializationError(
            "The assignment result does not contain any teams."
        )

    parsed: list[_ParsedTeam] = []
    seen_team_keys: set[str] = set()
    seen_user_ids: set[str] = set()
    for raw_team in raw_teams:
        if not isinstance(raw_team, dict):
            raise TournamentTeamMaterializationError(
                "Every assignment team must be an object."
            )
        team_key = _required_team_key(raw_team)
        if team_key in seen_team_keys:
            raise TournamentTeamMaterializationError(
                f"Assignment team id {team_key!r} is duplicated."
            )
        seen_team_keys.add(team_key)
        team_name = str(raw_team.get("team_name") or "").strip() or f"Team {team_key}"

        raw_captain = raw_team.get("captain")
        captain_user_id = _player_user_id(raw_captain, field_name=f"{team_key}.captain")
        members: list[dict[str, Any]] = []
        if isinstance(raw_captain, dict):
            members.append(
                _member_payload(
                    raw_captain,
                    team_key=team_key,
                    slot_number=0,
                    roster_role="captain",
                    assigned_role=raw_captain.get("assigned_role"),
                )
            )

        raw_starter_slots = raw_team.get("starter_slots") or []
        if not isinstance(raw_starter_slots, list):
            raise TournamentTeamMaterializationError(
                f"Assignment team {team_key!r} starter_slots must be a list."
            )
        for position, raw_slot in enumerate(raw_starter_slots, start=1):
            if not isinstance(raw_slot, dict):
                raise TournamentTeamMaterializationError(
                    f"Assignment team {team_key!r} starter slot must be an object."
                )
            slot_number = _integer(
                raw_slot.get("slot_number"),
                field_name=f"{team_key}.starter_slots.slot_number",
                default=position,
            )
            assert slot_number is not None
            if slot_number not in range(1, 6):
                raise TournamentTeamMaterializationError(
                    f"Assignment team {team_key!r} starter slot must be between 1 and 5."
                )
            player = raw_slot.get("assigned_player")
            if player is None:
                continue
            if not isinstance(player, dict):
                raise TournamentTeamMaterializationError(
                    f"Assignment team {team_key!r} starter slot player must be an object."
                )
            members.append(
                _member_payload(
                    player,
                    team_key=team_key,
                    slot_number=slot_number,
                    roster_role="starter",
                    assigned_role=raw_slot.get("assigned_role"),
                )
            )

        raw_reserve_slot = raw_team.get("reserve_slot")
        if raw_reserve_slot is not None:
            if not isinstance(raw_reserve_slot, dict):
                raise TournamentTeamMaterializationError(
                    f"Assignment team {team_key!r} reserve_slot must be an object."
                )
            reserve_player = raw_reserve_slot.get("assigned_player")
            if reserve_player is not None:
                if not isinstance(reserve_player, dict):
                    raise TournamentTeamMaterializationError(
                        f"Assignment team {team_key!r} reserve player must be an object."
                    )
                members.append(
                    _member_payload(
                        reserve_player,
                        team_key=team_key,
                        slot_number=6,
                        roster_role="substitute",
                        assigned_role=raw_reserve_slot.get("assigned_role"),
                    )
                )

        seen_slots: set[int] = set()
        for member in members:
            slot_number = int(member["slot_number"])
            if slot_number in seen_slots:
                raise TournamentTeamMaterializationError(
                    f"Assignment team {team_key!r} has duplicate slot {slot_number}."
                )
            seen_slots.add(slot_number)
            user_id = str(member["user_id"])
            if user_id in seen_user_ids:
                raise TournamentTeamMaterializationError(
                    f"User {user_id!r} is assigned to more than one team slot."
                )
            seen_user_ids.add(user_id)

        parsed.append(
            _ParsedTeam(
                team_key=team_key,
                name=team_name,
                captain_user_id=captain_user_id,
                starter_strength=_number(
                    raw_team.get("starter_strength"),
                    field_name=f"{team_key}.starter_strength",
                ),
                starter_average_strength=_number(
                    raw_team.get("starter_average_strength"),
                    field_name=f"{team_key}.starter_average_strength",
                ),
                members=tuple(members),
            )
        )
    return tuple(parsed)


async def materialize_assignment_run_teams(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    run_row: TournamentDeadlockAssignmentRun,
    now: datetime | None = None,
) -> tuple[tuple[TournamentTeam, ...], tuple[TournamentTeamMember, ...]]:
    """Replace the current team state from one authoritative assignment run.

    Callers must already hold the tournament workflow lock. The function
    intentionally does not commit: run status, teams, commitments, audit and
    bracket changes share the caller's transaction.
    """

    if run_row.tournament_id != tournament.id:
        raise TournamentTeamMaterializationError(
            "The assignment run does not belong to the tournament."
        )
    if run_row.status not in {"published", "locked"}:
        raise TournamentTeamMaterializationError(
            "Only a published or locked assignment can become current team state."
        )

    parsed_teams = _parse_snapshot_teams(run_row)

    # The tournament row is the primary lock. Locking existing teams in key
    # order keeps direct/recovery callers deterministic as well.
    locked_team_ids = await db_session.scalars(
        select(TournamentTeam.id)
        .where(TournamentTeam.tournament_id == tournament.id)
        .order_by(TournamentTeam.id.asc())
        .with_for_update()
    )
    locked_team_ids.all()
    await db_session.execute(
        delete(TournamentTeamMember).where(
            TournamentTeamMember.tournament_id == tournament.id
        )
    )
    await db_session.execute(
        delete(TournamentTeam).where(TournamentTeam.tournament_id == tournament.id)
    )

    team_rows = tuple(
        TournamentTeam(
            id=new_uuid(),
            tournament_id=tournament.id,
            source_assignment_run_id=run_row.id,
            team_key=team.team_key,
            name=team.name,
            captain_user_id=team.captain_user_id,
            starter_strength=team.starter_strength,
            starter_average_strength=team.starter_average_strength,
        )
        for team in parsed_teams
    )
    db_session.add_all(team_rows)
    try:
        await db_session.flush()
    except IntegrityError as exc:
        raise TournamentTeamMaterializationError(
            "The assignment contains users or team data that cannot be materialized."
        ) from exc

    member_rows = tuple(
        TournamentTeamMember(
            id=new_uuid(),
            tournament_id=tournament.id,
            team_id=team_row.id,
            user_id=str(member["user_id"]),
            slot_number=int(member["slot_number"]),
            roster_role=str(member["roster_role"]),
            assigned_role=member["assigned_role"],
            strength=float(member["strength"]),
            rank=member["rank"],
            subrank=member["subrank"],
        )
        for team_row, parsed_team in zip(team_rows, parsed_teams, strict=True)
        for member in parsed_team.members
    )
    db_session.add_all(member_rows)
    try:
        await db_session.flush()
    except IntegrityError as exc:
        raise TournamentTeamMaterializationError(
            "The assignment contains invalid roster membership or slots."
        ) from exc

    tournament.updated_at = (now or datetime.now(UTC)).astimezone(UTC)
    return team_rows, member_rows


async def load_tournament_team_state(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    include_members: bool = True,
) -> tuple[list[TournamentTeam], list[TournamentTeamMember]]:
    teams = (
        await db_session.scalars(
            select(TournamentTeam)
            .where(TournamentTeam.tournament_id == tournament_id)
            .order_by(TournamentTeam.team_key.asc(), TournamentTeam.id.asc())
        )
    ).all()
    if not include_members or not teams:
        return list(teams), []
    members = (
        await db_session.scalars(
            select(TournamentTeamMember)
            .where(
                TournamentTeamMember.tournament_id == tournament_id,
                TournamentTeamMember.team_id.in_([team.id for team in teams]),
            )
            .order_by(
                TournamentTeamMember.team_id.asc(),
                TournamentTeamMember.slot_number.asc(),
            )
        )
    ).all()
    return list(teams), list(members)


async def materialized_roster_members(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    assignment_run_id: str,
) -> tuple[tuple[str, str, str], ...]:
    rows = (
        await db_session.execute(
            select(
                TournamentTeamMember.user_id,
                TournamentTeam.team_key,
                TournamentTeam.name,
            )
            .join(TournamentTeam, TournamentTeam.id == TournamentTeamMember.team_id)
            .where(
                TournamentTeamMember.tournament_id == tournament_id,
                TournamentTeam.source_assignment_run_id == assignment_run_id,
            )
            .order_by(TournamentTeamMember.user_id.asc())
        )
    ).all()
    return tuple(
        (str(user_id), str(team_key), str(team_name))
        for user_id, team_key, team_name in rows
    )
