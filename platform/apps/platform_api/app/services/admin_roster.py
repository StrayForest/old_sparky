from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isclose
from typing import Any, Literal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.services.player_commitments import lock_commitment_users
from apps.platform_api.app.services.tournament_teams import load_tournament_team_state
from apps.platform_api.app.services.tournament_versions import tournament_state_version
from apps.platform_api.app.services.tournament_workflow import lock_tournament_for_workflow
from python_packages.platform_domain.deadlock import ROLE_OPTIONS, calculate_player_strength
from python_packages.platform_domain.tournaments import (
    TournamentWorkflowError,
    ensure_tournament_rank_allows_join,
)
from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.models import (
    AuditLog,
    DeadlockProfile,
    PlayerProfile,
    PlayerTournamentCommitment,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentMatch,
    TournamentParticipant,
    TournamentTeam,
    TournamentTeamMember,
    User,
)


INACTIVE_PARTICIPANT_STATUSES = frozenset({"withdrawn", "disqualified"})
TERMINAL_TOURNAMENT_STATUSES = frozenset({"completed", "cancelled"})
RosterOperation = Literal[
    "player_added",
    "player_removed",
    "player_moved",
    "player_replaced",
    "captain_changed",
]


class AdminRosterError(TournamentWorkflowError):
    """A safe, user-facing admin roster domain conflict."""


@dataclass(frozen=True, slots=True)
class RosterMutationResult:
    operation: RosterOperation
    resulting_state_version: int


def _roster_role_for_slot(slot_number: int) -> str:
    if slot_number == 0:
        return "captain"
    if 1 <= slot_number <= 5:
        return "starter"
    if slot_number == 6:
        return "substitute"
    raise AdminRosterError("Roster slot must be between 0 and 6.")


def _validate_player_slot(slot_number: int, *, allow_captain: bool = False) -> None:
    minimum = 0 if allow_captain else 1
    if slot_number < minimum or slot_number > 6:
        if allow_captain:
            raise AdminRosterError("Roster slot must be between 0 and 6.")
        raise AdminRosterError("Player operations support starter slots 1-5 and substitute slot 6.")


def _touch_tournament(tournament: Tournament, now: datetime) -> None:
    candidate = now.astimezone(UTC)
    current = tournament.updated_at
    if current is not None:
        current = current.astimezone(UTC)
        if candidate <= current:
            # State versions use milliseconds. Keep rapid consecutive admin
            # mutations observable to a stale screen as well.
            candidate = current + timedelta(milliseconds=1)
    tournament.updated_at = candidate


def _current_run_statement(tournament_id: str):
    return (
        select(TournamentDeadlockAssignmentRun)
        .where(
            TournamentDeadlockAssignmentRun.tournament_id == tournament_id,
            TournamentDeadlockAssignmentRun.status.in_(("published", "locked")),
        )
        .order_by(
            TournamentDeadlockAssignmentRun.locked_at.desc().nullslast(),
            TournamentDeadlockAssignmentRun.published_at.desc().nullslast(),
            TournamentDeadlockAssignmentRun.created_at.desc(),
            TournamentDeadlockAssignmentRun.id.desc(),
        )
    )


async def _current_assignment_run(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    for_update: bool = False,
) -> TournamentDeadlockAssignmentRun | None:
    statement = _current_run_statement(tournament_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return await db_session.scalar(statement)


async def _bracket_impact(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> dict[str, int | bool]:
    row = (
        await db_session.execute(
            select(
                func.count(TournamentMatch.id).label("match_count"),
                func.coalesce(
                    func.sum(
                        case(
                            (TournamentMatch.status.in_(("live", "completed")), 1),
                            else_=0,
                        )
                    ),
                    0,
                ).label("started_count"),
                func.coalesce(
                    func.sum(
                        case((TournamentMatch.status == "completed", 1), else_=0)
                    ),
                    0,
                ).label("completed_count"),
            ).where(TournamentMatch.tournament_id == tournament_id)
        )
    ).one()
    match_count = int(row.match_count or 0)
    return {
        "exists": match_count > 0,
        "revision": 0,
        "match_count": match_count,
        "started_count": int(row.started_count or 0),
        "completed_count": int(row.completed_count or 0),
    }


def _roster_capabilities(
    *,
    tournament: Tournament,
    run: TournamentDeadlockAssignmentRun | None,
    bracket: dict[str, int | bool],
    role_slugs: Iterable[str],
) -> dict[str, Any]:
    roles = {str(role) for role in role_slugs}
    is_superadmin = "superadmin" in roles
    is_admin = "admin" in roles or is_superadmin
    started = int(bracket["started_count"] or 0) > 0
    bracket_exists = bool(bracket["exists"])
    normal_window = bool(
        run is not None
        and run.status == "published"
        and tournament.status == "registration_closed"
        and not bracket_exists
        and not started
    )
    high_risk_window = bool(
        run is not None
        and not started
        and tournament.status not in TERMINAL_TOURNAMENT_STATUSES
        and (run.status == "locked" or bracket_exists or tournament.status != "registration_closed")
    )
    requires_override = not normal_window
    can_mutate = bool(is_admin and normal_window) or bool(is_superadmin and high_risk_window)

    reason: str | None = None
    if run is None:
        reason = "Current roster is not materialized. Complete the standard assignment workflow first."
    elif started:
        reason = "Roster changes are blocked after a live or completed match."
    elif tournament.status in TERMINAL_TOURNAMENT_STATUSES:
        reason = "Roster changes are blocked for completed or cancelled tournaments."
    elif not is_admin:
        reason = "Admin role is required for roster operations."
    elif requires_override and not is_superadmin:
        reason = "This roster requires an explicit superadmin override."
    elif not can_mutate:
        reason = "Roster changes are unavailable in the current tournament lifecycle state."

    return {
        "can_add_player": can_mutate,
        "can_remove_player": can_mutate,
        "can_move_player": can_mutate,
        "can_replace_player": can_mutate,
        "can_change_captain": can_mutate,
        "requires_override": requires_override,
        "can_override": bool(is_superadmin and high_risk_window),
        "blocked_reason": reason,
    }


def _member_fragment(member: TournamentTeamMember) -> dict[str, Any]:
    return {
        "user_id": str(member.user_id),
        "slot_number": int(member.slot_number),
        "roster_role": str(member.roster_role),
        "assigned_role": member.assigned_role,
        "strength": round(float(member.strength or 0.0), 4),
        "rank": member.rank,
        "subrank": member.subrank,
    }


def _team_fragments(
    teams: Iterable[TournamentTeam],
    members: Iterable[TournamentTeamMember],
) -> dict[str, Any]:
    members_by_team: dict[str, list[dict[str, Any]]] = {}
    team_key_by_id = {str(team.id): str(team.team_key) for team in teams}
    for member in members:
        team_key = team_key_by_id.get(str(member.team_id), str(member.team_id))
        members_by_team.setdefault(team_key, []).append(_member_fragment(member))
    return {
        str(team.team_key): {
            "team_key": str(team.team_key),
            "captain_user_id": str(team.captain_user_id) if team.captain_user_id else None,
            "starter_strength": round(float(team.starter_strength or 0.0), 4),
            "starter_average_strength": round(float(team.starter_average_strength or 0.0), 4),
            "members": sorted(
                members_by_team.get(str(team.team_key), []),
                key=lambda item: int(item["slot_number"]),
            ),
        }
        for team in sorted(teams, key=lambda item: (str(item.team_key), str(item.id)))
    }


async def _user_details(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    member_user_ids: set[str],
) -> dict[str, dict[str, Any]]:
    participant_join = and_(
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.user_id == User.id,
    )
    rows = await db_session.execute(
        select(
            User.id.label("user_id"),
            User.status.label("user_status"),
            User.display_name.label("user_display_name"),
            PlayerProfile.display_name.label("profile_display_name"),
            PlayerProfile.handle,
            TournamentParticipant.id.label("participant_id"),
            TournamentParticipant.status.label("participant_status"),
            DeadlockProfile.rank,
            DeadlockProfile.subrank,
            DeadlockProfile.playtime,
            DeadlockProfile.roles,
        )
        .select_from(User)
        .outerjoin(TournamentParticipant, participant_join)
        .outerjoin(PlayerProfile, PlayerProfile.user_id == User.id)
        .outerjoin(DeadlockProfile, DeadlockProfile.user_id == User.id)
        .where(
            or_(
                TournamentParticipant.id.is_not(None),
                User.id.in_(sorted(member_user_ids)),
            )
        )
    )
    return {
        str(row.user_id): dict(row._mapping)
        for row in rows
    }


async def load_admin_roster_snapshot(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    role_slugs: Iterable[str],
) -> dict[str, Any]:
    run = await _current_assignment_run(db_session, tournament_id=tournament.id)
    teams, members = await load_tournament_team_state(
        db_session,
        tournament_id=tournament.id,
        include_members=True,
    )
    bracket = await _bracket_impact(db_session, tournament_id=tournament.id)
    bracket["revision"] = int(tournament.bracket_revision or 0)
    member_user_ids = {str(member.user_id) for member in members}
    details = await _user_details(
        db_session,
        tournament_id=tournament.id,
        member_user_ids=member_user_ids,
    )
    active_participant_count = sum(
        1
        for item in details.values()
        if item.get("participant_id") is not None
        and item.get("participant_status") not in INACTIVE_PARTICIPANT_STATUSES
    )
    members_by_team: dict[str, list[dict[str, Any]]] = {}
    for member in members:
        detail = details.get(str(member.user_id), {})
        members_by_team.setdefault(str(member.team_id), []).append(
            {
                "id": str(member.id),
                "user_id": str(member.user_id),
                "display_name": str(
                    detail.get("profile_display_name")
                    or detail.get("user_display_name")
                    or "Игрок"
                ),
                "handle": detail.get("handle"),
                "participant_status": detail.get("participant_status"),
                "slot_number": int(member.slot_number),
                "roster_role": str(member.roster_role),
                "assigned_role": member.assigned_role,
                "strength": round(float(member.strength or 0.0), 4),
                "rank": member.rank,
                "subrank": member.subrank,
            }
        )
    serialized_teams = []
    for team in teams:
        serialized_teams.append(
            {
                "id": str(team.id),
                "team_key": str(team.team_key),
                "name": team.name,
                "captain_user_id": str(team.captain_user_id) if team.captain_user_id else None,
                "starter_strength": round(float(team.starter_strength or 0.0), 4),
                "starter_average_strength": round(float(team.starter_average_strength or 0.0), 4),
                "members": sorted(
                    members_by_team.get(str(team.id), []),
                    key=lambda item: int(item["slot_number"]),
                ),
            }
        )
    unassigned = []
    for user_id, detail in sorted(
        details.items(),
        key=lambda item: (
            str(item[1].get("profile_display_name") or item[1].get("user_display_name") or "").casefold(),
            item[0],
        ),
    ):
        if detail.get("participant_id") is None:
            continue
        if detail.get("participant_status") in INACTIVE_PARTICIPANT_STATUSES:
            continue
        if user_id in member_user_ids:
            continue
        rank = detail.get("rank")
        subrank = detail.get("subrank")
        playtime = detail.get("playtime")
        strength = None
        if rank is not None and subrank is not None and playtime is not None:
            strength = round(float(calculate_player_strength(str(rank), int(subrank), str(playtime))), 4)
        unassigned.append(
            {
                "participant_id": str(detail["participant_id"]),
                "user_id": user_id,
                "display_name": str(
                    detail.get("profile_display_name")
                    or detail.get("user_display_name")
                    or "Игрок"
                ),
                "handle": detail.get("handle"),
                "status": detail.get("participant_status"),
                "rank": rank,
                "subrank": int(subrank) if subrank is not None else None,
                "playtime": playtime,
                "strength": strength,
            }
        )
    last_manual_change = await db_session.scalar(
        select(AuditLog)
        .where(
            AuditLog.subject_type == "tournament",
            AuditLog.subject_id == tournament.id,
            AuditLog.action.like("admin.tournament.roster.%"),
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
    )
    capabilities = _roster_capabilities(
        tournament=tournament,
        run=run,
        bracket=bracket,
        role_slugs=role_slugs,
    )
    return {
        "tournament_id": tournament.id,
        "tournament_slug": tournament.slug,
        "tournament_status": tournament.status,
        "active_participant_count": active_participant_count,
        "source_assignment_run_id": str(run.id) if run is not None else None,
        "source_assignment_status": run.status if run is not None else None,
        "locked": bool(run is not None and run.status == "locked"),
        "manually_modified": last_manual_change is not None,
        "last_modified_at": last_manual_change.created_at if last_manual_change else None,
        "bracket": bracket,
        "teams": serialized_teams,
        "unassigned_participants": unassigned,
        "capabilities": capabilities,
        "state_version": tournament_state_version(
            tournament,
            participant_count=active_participant_count,
        ),
        # Kept internal to make mutation validation/audit diagnostics cheap;
        # it is removed by the API serializer below.
        "_team_fragments": _team_fragments(teams, members),
    }


def public_roster_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if not key.startswith("_")}


async def _load_locked_state(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    lock_user_ids: Iterable[str],
) -> tuple[
    Tournament,
    TournamentDeadlockAssignmentRun,
    list[TournamentTeam],
    list[TournamentTeamMember],
    dict[str, TournamentParticipant],
    dict[str, User],
]:
    tournament = await lock_tournament_for_workflow(db_session, tournament_id)
    run = await _current_assignment_run(
        db_session,
        tournament_id=tournament.id,
        for_update=True,
    )
    if run is None:
        raise AdminRosterError(
            "Current roster is not materialized. Complete the standard assignment workflow first."
        )
    teams = list(
        (
            await db_session.scalars(
                select(TournamentTeam)
                .where(
                    TournamentTeam.tournament_id == tournament.id,
                    TournamentTeam.source_assignment_run_id == run.id,
                )
                .order_by(TournamentTeam.team_key.asc(), TournamentTeam.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if not teams:
        raise AdminRosterError("The current assignment has no materialized teams.")
    team_ids = [team.id for team in teams]
    members = list(
        (
            await db_session.scalars(
                select(TournamentTeamMember)
                .where(
                    TournamentTeamMember.tournament_id == tournament.id,
                    TournamentTeamMember.team_id.in_(team_ids),
                )
                .order_by(TournamentTeamMember.team_id.asc(), TournamentTeamMember.slot_number.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    normalized_user_ids = sorted(
        {
            str(user_id).strip()
            for user_id in lock_user_ids
            if str(user_id).strip()
        }
        | {str(member.user_id) for member in members}
    )
    await lock_commitment_users(db_session, normalized_user_ids)
    participant_rows = list(
        (
            await db_session.scalars(
                select(TournamentParticipant)
                .where(
                    TournamentParticipant.tournament_id == tournament.id,
                    TournamentParticipant.user_id.in_(normalized_user_ids),
                )
                .order_by(TournamentParticipant.user_id.asc(), TournamentParticipant.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    user_rows = list(
        (
            await db_session.scalars(
                select(User)
                .where(User.id.in_(normalized_user_ids))
                .order_by(User.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    return (
        tournament,
        run,
        teams,
        members,
        {str(row.user_id): row for row in participant_rows},
        {str(row.id): row for row in user_rows},
    )


async def _load_matches(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> list[TournamentMatch]:
    # The tournament workflow lock already serializes all supported match and
    # bracket writers. Keep this bounded read out of the row-lock set so an
    # admin roster correction does not hold every historical match row.
    return list(
        (
            await db_session.scalars(
                select(TournamentMatch)
                .where(TournamentMatch.tournament_id == tournament_id)
                .order_by(TournamentMatch.round_number.asc(), TournamentMatch.sequence_number.asc())
            )
        ).all()
    )


async def _profile_for_player(
    db_session: AsyncSession,
    *,
    user_id: str,
    participant_by_user_id: dict[str, TournamentParticipant],
    user_by_id: dict[str, User],
    tournament: Tournament,
    assigned_role: str | None,
) -> dict[str, Any]:
    user = user_by_id.get(user_id)
    if user is None:
        raise AdminRosterError("The selected player no longer exists.")
    if user.status != "active":
        raise AdminRosterError("Inactive users cannot be assigned to a tournament roster.")
    participant = participant_by_user_id.get(user_id)
    if participant is None:
        raise AdminRosterError("The selected player is not a participant in this tournament.")
    if participant.status in INACTIVE_PARTICIPANT_STATUSES:
        raise AdminRosterError("Withdrawn or disqualified participants cannot be assigned.")
    profile = await db_session.scalar(
        select(DeadlockProfile)
        .where(DeadlockProfile.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if profile is None:
        raise AdminRosterError("The selected player needs a complete Deadlock profile first.")
    if not 1 <= int(profile.subrank) <= 6:
        raise AdminRosterError("The selected player's Deadlock subrank is invalid.")
    try:
        ensure_tournament_rank_allows_join(
            allowed_ranks=tournament.allowed_ranks,
            player_rank=profile.rank,
        )
    except TournamentWorkflowError as exc:
        raise AdminRosterError(str(exc)) from exc
    effective_roles = tuple(profile.roles or ROLE_OPTIONS)
    resolved_role = assigned_role.strip() if assigned_role else None
    if resolved_role is not None:
        if resolved_role not in ROLE_OPTIONS or resolved_role not in effective_roles:
            raise AdminRosterError("The requested assigned role is not available for this player's profile.")
    else:
        resolved_role = next(
            (role for role in ROLE_OPTIONS if role in effective_roles),
            ROLE_OPTIONS[0],
        )
    return {
        "user_id": user_id,
        "assigned_role": resolved_role,
        "strength": round(
            float(calculate_player_strength(profile.rank, int(profile.subrank), profile.playtime)),
            4,
        ),
        "rank": profile.rank,
        "subrank": int(profile.subrank),
    }


def _member_by_user(
    teams: Iterable[TournamentTeam],
    members: Iterable[TournamentTeamMember],
) -> dict[str, TournamentTeamMember]:
    return {str(member.user_id): member for member in members}


def _team_by_key(teams: Iterable[TournamentTeam], team_key: str) -> TournamentTeam:
    normalized = str(team_key).strip()
    for team in teams:
        if str(team.team_key).strip() == normalized:
            return team
    raise AdminRosterError("The selected team does not belong to this tournament.")


def _member_for_slot(
    members: Iterable[TournamentTeamMember],
    *,
    team_id: str,
    slot_number: int,
) -> TournamentTeamMember:
    for member in members:
        if str(member.team_id) == str(team_id) and int(member.slot_number) == slot_number:
            return member
    raise AdminRosterError("The selected roster slot is empty.")


def _ensure_slot_free(
    members: Iterable[TournamentTeamMember],
    *,
    team_id: str,
    slot_number: int,
) -> None:
    if any(
        str(member.team_id) == str(team_id)
        and int(member.slot_number) == slot_number
        for member in members
    ):
        raise AdminRosterError("The destination roster slot is already occupied.")


def _recompute_team(team: TournamentTeam, members: Iterable[TournamentTeamMember]) -> None:
    team_members = [member for member in members if str(member.team_id) == str(team.id)]
    starters = [member for member in team_members if int(member.slot_number) <= 5]
    starter_strength = sum(float(member.strength or 0.0) for member in starters)
    team.starter_strength = round(starter_strength, 4)
    team.starter_average_strength = round(starter_strength / max(1, len(starters)), 4)


def _validate_resulting_roster(
    *,
    tournament: Tournament,
    run: TournamentDeadlockAssignmentRun,
    teams: list[TournamentTeam],
    members: list[TournamentTeamMember],
    participant_by_user_id: dict[str, TournamentParticipant],
    user_by_id: dict[str, User],
    matches: list[TournamentMatch],
) -> None:
    team_keys = {str(team.team_key).strip() for team in teams}
    if len(team_keys) != len(teams) or not team_keys:
        raise AdminRosterError("The resulting roster must contain unique teams.")
    if any(str(team.source_assignment_run_id) != str(run.id) for team in teams):
        raise AdminRosterError("Roster provenance cannot be changed by an admin roster operation.")
    member_users: set[str] = set()
    members_by_team: dict[str, list[TournamentTeamMember]] = {}
    for member in members:
        user_id = str(member.user_id)
        if user_id in member_users:
            raise AdminRosterError("A player cannot belong to more than one team in this tournament.")
        member_users.add(user_id)
        participant = participant_by_user_id.get(user_id)
        user = user_by_id.get(user_id)
        if participant is None or participant.status in INACTIVE_PARTICIPANT_STATUSES:
            raise AdminRosterError("Inactive or missing participants cannot remain on a roster.")
        if user is None or user.status != "active":
            raise AdminRosterError("Inactive users cannot remain on a roster.")
        expected_role = _roster_role_for_slot(int(member.slot_number))
        if member.roster_role != expected_role:
            raise AdminRosterError("Roster role and slot are inconsistent.")
        if member.assigned_role is not None and member.assigned_role not in ROLE_OPTIONS:
            raise AdminRosterError("Roster contains an unsupported assigned role.")
        members_by_team.setdefault(str(member.team_id), []).append(member)

    for team in teams:
        team_members = members_by_team.get(str(team.id), [])
        if len(team_members) > 7:
            raise AdminRosterError("A team cannot contain more than seven roster slots.")
        captains = [member for member in team_members if member.roster_role == "captain"]
        if len(captains) != 1 or int(captains[0].slot_number) != 0:
            raise AdminRosterError("Every team must have exactly one captain in slot 0.")
        if str(team.captain_user_id) != str(captains[0].user_id):
            raise AdminRosterError("Captain membership and captain_user_id are inconsistent.")
        starters = [member for member in team_members if int(member.slot_number) <= 5]
        starter_strength = round(sum(float(member.strength or 0.0) for member in starters), 4)
        starter_average = round(starter_strength / max(1, len(starters)), 4)
        if not isclose(float(team.starter_strength or 0.0), starter_strength, abs_tol=0.0002):
            raise AdminRosterError("A team's starter strength is stale or invalid.")
        if not isclose(
            float(team.starter_average_strength or 0.0),
            starter_average,
            abs_tol=0.0002,
        ):
            raise AdminRosterError("A team's average starter strength is stale or invalid.")

    for match in matches:
        for team_id in (match.home_team_id, match.away_team_id, match.winner_team_id):
            if team_id is not None and str(team_id).strip() not in team_keys:
                raise AdminRosterError("The roster cannot invalidate an existing bracket team reference.")


async def _sync_locked_commitments(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    run: TournamentDeadlockAssignmentRun,
    teams: list[TournamentTeam],
    members: list[TournamentTeamMember],
    removed_user_ids: set[str],
    now: datetime,
) -> None:
    user_ids = {str(member.user_id) for member in members} | set(removed_user_ids)
    if not user_ids:
        raise AdminRosterError("A locked roster cannot become empty.")
    commitments = list(
        (
            await db_session.scalars(
                select(PlayerTournamentCommitment)
                .where(
                    PlayerTournamentCommitment.user_id.in_(sorted(user_ids)),
                    PlayerTournamentCommitment.released_at.is_(None),
                )
                .order_by(PlayerTournamentCommitment.user_id.asc(), PlayerTournamentCommitment.id.asc())
                .with_for_update()
            )
        ).all()
    )
    commitment_by_user = {str(item.user_id): item for item in commitments}
    team_by_id = {str(team.id): team for team in teams}
    current_ids = {str(member.user_id) for member in members}
    for member in members:
        commitment = commitment_by_user.get(str(member.user_id))
        team = team_by_id.get(str(member.team_id))
        if team is None:
            raise AdminRosterError("The member team no longer exists.")
        if commitment is not None and str(commitment.tournament_id) != str(tournament.id):
            raise AdminRosterError("The replacement player is committed to another tournament.")
        if commitment is None:
            db_session.add(
                PlayerTournamentCommitment(
                    user_id=str(member.user_id),
                    tournament_id=tournament.id,
                    assignment_run_id=run.id,
                    team_id=team.team_key,
                    team_name=team.name,
                    activated_at=now,
                )
            )
        else:
            commitment.assignment_run_id = run.id
            commitment.team_id = team.team_key
            commitment.team_name = team.name
    for user_id in removed_user_ids - current_ids:
        commitment = commitment_by_user.get(user_id)
        if commitment is not None:
            if str(commitment.tournament_id) != str(tournament.id):
                raise AdminRosterError("The removed player is committed to another tournament.")
            commitment.released_at = now
            commitment.release_reason = "admin_tournament_roster_removed"
    await db_session.flush()


def _new_member(
    *,
    tournament_id: str,
    team_id: str,
    slot_number: int,
    player: dict[str, Any],
) -> TournamentTeamMember:
    return TournamentTeamMember(
        tournament_id=tournament_id,
        team_id=team_id,
        user_id=str(player["user_id"]),
        slot_number=slot_number,
        roster_role=_roster_role_for_slot(slot_number),
        assigned_role=player["assigned_role"],
        strength=float(player["strength"]),
        rank=player["rank"],
        subrank=int(player["subrank"]),
    )


async def mutate_admin_roster(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    actor_user_id: str,
    role_slugs: Iterable[str],
    operation: RosterOperation,
    command: dict[str, Any],
    expected_state_version: int,
    reason: str,
    override: bool,
    now: datetime,
) -> RosterMutationResult:
    command_user_ids = {
        str(command.get("user_id") or "").strip(),
        str(command.get("replacement_user_id") or "").strip(),
    }
    (
        tournament,
        run,
        teams,
        members,
        participant_by_user_id,
        user_by_id,
    ) = await _load_locked_state(
        db_session,
        tournament_id=tournament_id,
        lock_user_ids=command_user_ids,
    )
    matches = await _load_matches(db_session, tournament_id=tournament.id)
    bracket = await _bracket_impact(db_session, tournament_id=tournament.id)
    bracket["revision"] = int(tournament.bracket_revision or 0)
    capabilities = _roster_capabilities(
        tournament=tournament,
        run=run,
        bracket=bracket,
        role_slugs=role_slugs,
    )
    if not any(capabilities[key] for key in (
        "can_add_player",
        "can_remove_player",
        "can_move_player",
        "can_replace_player",
        "can_change_captain",
    )):
        raise AdminRosterError(str(capabilities["blocked_reason"] or "Roster changes are unavailable."))
    if override and not capabilities["can_override"]:
        raise AdminRosterError("Only a superadmin may use an override for this roster state.")
    if capabilities["requires_override"] and not override:
        raise AdminRosterError("This roster operation requires an explicit superadmin override.")
    active_count = int(
        await db_session.scalar(
            select(func.count())
            .select_from(TournamentParticipant)
            .where(
                TournamentParticipant.tournament_id == tournament.id,
                TournamentParticipant.status.not_in(tuple(INACTIVE_PARTICIPANT_STATUSES)),
            )
        )
        or 0
    )
    current_state_version = tournament_state_version(
        tournament,
        participant_count=active_count,
    )
    if int(expected_state_version) != current_state_version:
        raise AdminRosterError("Tournament state changed in another session. Refresh the roster and retry.")

    _validate_resulting_roster(
        tournament=tournament,
        run=run,
        teams=teams,
        members=members,
        participant_by_user_id=participant_by_user_id,
        user_by_id=user_by_id,
        matches=matches,
    )
    before = _team_fragments(teams, members)
    members_by_user = _member_by_user(teams, members)
    affected_user_ids = set(command_user_ids)
    removed_user_ids: set[str] = set()
    affected_team_keys: set[str] = set()

    if operation == "player_added":
        team = _team_by_key(teams, str(command.get("team_key") or ""))
        slot_number = int(command.get("slot_number"))
        _validate_player_slot(slot_number)
        user_id = str(command.get("user_id") or "").strip()
        if user_id in members_by_user:
            raise AdminRosterError("The selected player is already assigned to a team.")
        _ensure_slot_free(members, team_id=team.id, slot_number=slot_number)
        player = await _profile_for_player(
            db_session,
            user_id=user_id,
            participant_by_user_id=participant_by_user_id,
            user_by_id=user_by_id,
            tournament=tournament,
            assigned_role=command.get("assigned_role"),
        )
        db_session.add(
            _new_member(
                tournament_id=tournament.id,
                team_id=team.id,
                slot_number=slot_number,
                player=player,
            )
        )
        affected_team_keys.add(str(team.team_key))
        operation_audit_name = "player_added"
    elif operation == "player_removed":
        team = _team_by_key(teams, str(command.get("team_key") or ""))
        user_id = str(command.get("user_id") or "").strip()
        member = members_by_user.get(user_id)
        if member is None or str(member.team_id) != str(team.id):
            raise AdminRosterError("The selected player is not in the selected team.")
        if member.roster_role == "captain":
            raise AdminRosterError("Change the captain before removing the current captain.")
        await db_session.delete(member)
        removed_user_ids.add(user_id)
        affected_team_keys.add(str(team.team_key))
        operation_audit_name = "player_removed"
    elif operation == "player_moved":
        source_team = _team_by_key(teams, str(command.get("team_key") or ""))
        destination_team = _team_by_key(teams, str(command.get("destination_team_key") or ""))
        user_id = str(command.get("user_id") or "").strip()
        destination_slot = int(command.get("destination_slot"))
        _validate_player_slot(destination_slot)
        member = members_by_user.get(user_id)
        if member is None or str(member.team_id) != str(source_team.id):
            raise AdminRosterError("The selected player is not in the selected team.")
        if member.roster_role == "captain":
            raise AdminRosterError("Use the captain operation to change a captain's position.")
        if str(member.team_id) == str(destination_team.id) and int(member.slot_number) == destination_slot:
            raise AdminRosterError("The player is already in the requested roster slot.")
        _ensure_slot_free(members, team_id=destination_team.id, slot_number=destination_slot)
        member.team_id = destination_team.id
        member.slot_number = destination_slot
        member.roster_role = _roster_role_for_slot(destination_slot)
        affected_team_keys.update((str(source_team.team_key), str(destination_team.team_key)))
        operation_audit_name = "player_moved"
    elif operation == "player_replaced":
        team = _team_by_key(teams, str(command.get("team_key") or ""))
        slot_number = int(command.get("slot_number"))
        _validate_player_slot(slot_number, allow_captain=True)
        source = _member_for_slot(members, team_id=team.id, slot_number=slot_number)
        replacement_user_id = str(command.get("replacement_user_id") or "").strip()
        if replacement_user_id in members_by_user:
            raise AdminRosterError("The replacement player is already assigned to a team.")
        replacement = await _profile_for_player(
            db_session,
            user_id=replacement_user_id,
            participant_by_user_id=participant_by_user_id,
            user_by_id=user_by_id,
            tournament=tournament,
            assigned_role=command.get("assigned_role"),
        )
        old_user_id = str(source.user_id)
        if source.roster_role == "captain":
            await db_session.delete(source)
            await db_session.flush()
            team.captain_user_id = replacement_user_id
            db_session.add(
                _new_member(
                    tournament_id=tournament.id,
                    team_id=team.id,
                    slot_number=0,
                    player=replacement,
                )
            )
        else:
            source.user_id = replacement_user_id
            source.assigned_role = replacement["assigned_role"]
            source.strength = replacement["strength"]
            source.rank = replacement["rank"]
            source.subrank = replacement["subrank"]
        removed_user_ids.add(old_user_id)
        affected_user_ids.update((old_user_id, replacement_user_id))
        affected_team_keys.add(str(team.team_key))
        operation_audit_name = "player_replaced"
    elif operation == "captain_changed":
        team = _team_by_key(teams, str(command.get("team_key") or ""))
        target_user_id = str(command.get("user_id") or "").strip()
        current_captain = _member_for_slot(members, team_id=team.id, slot_number=0)
        target = members_by_user.get(target_user_id)
        if target is None or str(target.team_id) != str(team.id):
            raise AdminRosterError("The new captain must already belong to the selected team.")
        if target_user_id == str(current_captain.user_id):
            raise AdminRosterError("The selected player is already the captain.")
        requested_target_role = command.get("assigned_role")
        await _profile_for_player(
            db_session,
            user_id=target_user_id,
            participant_by_user_id=participant_by_user_id,
            user_by_id=user_by_id,
            tournament=tournament,
            assigned_role=requested_target_role or target.assigned_role,
        )
        target_slot = int(target.slot_number)
        old_captain_user_id = str(current_captain.user_id)
        old_captain_fragment = _member_fragment(current_captain)
        old_captain_member_id = str(current_captain.id)
        target_member_id = str(target.id)
        target_strength = target.strength
        target_rank = target.rank
        target_subrank = target.subrank
        target_assigned_role = (
            str(requested_target_role).strip()
            if requested_target_role is not None
            else target.assigned_role
        )
        await db_session.delete(current_captain)
        await db_session.delete(target)
        await db_session.flush()
        db_session.add(
            TournamentTeamMember(
                id=old_captain_member_id,
                tournament_id=tournament.id,
                team_id=team.id,
                user_id=old_captain_user_id,
                slot_number=target_slot,
                roster_role=_roster_role_for_slot(target_slot),
                assigned_role=old_captain_fragment["assigned_role"],
                strength=old_captain_fragment["strength"],
                rank=old_captain_fragment["rank"],
                subrank=old_captain_fragment["subrank"],
            )
        )
        db_session.add(
            TournamentTeamMember(
                id=target_member_id,
                tournament_id=tournament.id,
                team_id=team.id,
                user_id=target_user_id,
                slot_number=0,
                roster_role="captain",
                assigned_role=target_assigned_role,
                strength=target_strength,
                rank=target_rank,
                subrank=target_subrank,
            )
        )
        team.captain_user_id = target_user_id
        affected_user_ids.update((old_captain_user_id, target_user_id))
        affected_team_keys.add(str(team.team_key))
        operation_audit_name = "captain_changed"
    else:  # pragma: no cover - the typed route surface makes this unreachable.
        raise AdminRosterError("Unsupported roster operation.")

    await db_session.flush()
    current_teams, current_members = await load_tournament_team_state(
        db_session,
        tournament_id=tournament.id,
        include_members=True,
    )
    for team in current_teams:
        if str(team.team_key) in affected_team_keys:
            _recompute_team(team, current_members)
    await db_session.flush()
    participant_by_user_id = {
        str(row.user_id): row
        for row in (
            await db_session.scalars(
                select(TournamentParticipant).where(
                    TournamentParticipant.tournament_id == tournament.id,
                    TournamentParticipant.user_id.in_(
                        sorted({str(member.user_id) for member in current_members} | removed_user_ids)
                    ),
                )
            )
        ).all()
    }
    user_by_id = {
        str(row.id): row
        for row in (
            await db_session.scalars(
                select(User).where(
                    User.id.in_(
                        sorted({str(member.user_id) for member in current_members} | removed_user_ids)
                    )
                )
            )
        ).all()
    }
    _validate_resulting_roster(
        tournament=tournament,
        run=run,
        teams=current_teams,
        members=current_members,
        participant_by_user_id=participant_by_user_id,
        user_by_id=user_by_id,
        matches=matches,
    )
    if run.status == "locked":
        await _sync_locked_commitments(
            db_session,
            tournament=tournament,
            run=run,
            teams=current_teams,
            members=current_members,
            removed_user_ids=removed_user_ids,
            now=now,
        )
    _touch_tournament(tournament, now)
    await db_session.flush()
    resulting_state_version = tournament_state_version(
        tournament,
        participant_count=active_count,
    )
    after = _team_fragments(current_teams, current_members)
    await write_audit_log(
        db_session,
        actor_user_id=actor_user_id,
        action=f"admin.tournament.roster.{operation_audit_name}",
        subject_type="tournament",
        subject_id=tournament.id,
        payload={
            "tournament_slug": tournament.slug,
            "source_assignment_run_id": run.id,
            "operation": operation_audit_name,
            "affected_user_ids": sorted(affected_user_ids),
            "affected_team_keys": sorted(affected_team_keys),
            "reason": reason.strip(),
            "before": before,
            "after": after,
            "expected_state_version": int(expected_state_version),
            "resulting_state_version": resulting_state_version,
            "bracket_revision": int(tournament.bracket_revision or 0),
            "bracket_state": bracket,
            "override": bool(override),
        },
    )
    return RosterMutationResult(
        operation=operation,
        resulting_state_version=resulting_state_version,
    )
