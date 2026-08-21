from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.models import (
    PlayerTournamentCommitment,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentMatch,
    User,
)


@dataclass(frozen=True, slots=True)
class AssignmentRosterMember:
    user_id: str
    team_id: str
    team_name: str


@dataclass(frozen=True, slots=True)
class ActiveCommitmentView:
    id: str
    tournament_id: str
    tournament_slug: str
    tournament_name: str
    assignment_run_id: str
    team_id: str
    team_name: str
    activated_at: datetime


@dataclass(frozen=True, slots=True)
class CommitmentReconciliationResult:
    terminal_released: int
    eliminated_released: int
    mismatched_released: int

    @property
    def released_total(self) -> int:
        return self.terminal_released + self.eliminated_released + self.mismatched_released


class PlayerCommitmentConflict(RuntimeError):
    def __init__(self, commitments: Iterable[ActiveCommitmentView]) -> None:
        self.commitments = tuple(commitments)
        super().__init__("One or more players already have an active tournament commitment.")


def assignment_roster_members(run_row: TournamentDeadlockAssignmentRun) -> tuple[AssignmentRosterMember, ...]:
    snapshot = dict(run_row.result_snapshot or {})
    members: dict[str, AssignmentRosterMember] = {}
    for raw_team in list(snapshot.get("teams") or []):
        if not isinstance(raw_team, dict):
            continue
        team_id = str(raw_team.get("team_id") or "").strip()
        if not team_id:
            continue
        team_name = str(raw_team.get("team_name") or f"Team {team_id}").strip() or f"Team {team_id}"
        raw_members: list[Any] = [raw_team.get("captain")]
        raw_members.extend(
            slot.get("assigned_player")
            for slot in list(raw_team.get("starter_slots") or [])
            if isinstance(slot, dict)
        )
        reserve_slot = raw_team.get("reserve_slot")
        if isinstance(reserve_slot, dict):
            raw_members.append(reserve_slot.get("assigned_player"))
        for raw_member in raw_members:
            if not isinstance(raw_member, dict):
                continue
            user_id = str(raw_member.get("user_id") or "").strip()
            if not user_id:
                continue
            members[user_id] = AssignmentRosterMember(
                user_id=user_id,
                team_id=team_id,
                team_name=team_name,
            )
    return tuple(sorted(members.values(), key=lambda item: item.user_id))


async def lock_commitment_users(db_session: AsyncSession, user_ids: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({str(user_id) for user_id in user_ids if str(user_id)}))
    if not normalized:
        return ()
    locked = (
        await db_session.scalars(
            select(User.id)
            .where(User.id.in_(normalized))
            .order_by(User.id.asc())
            .with_for_update()
        )
    ).all()
    return tuple(str(user_id) for user_id in locked)


async def active_commitments_by_user(
    db_session: AsyncSession,
    user_ids: Iterable[str],
) -> dict[str, ActiveCommitmentView]:
    normalized = tuple(sorted({str(user_id) for user_id in user_ids if str(user_id)}))
    if not normalized:
        return {}
    stmt = (
        select(PlayerTournamentCommitment, Tournament.slug, Tournament.name)
        .join(Tournament, Tournament.id == PlayerTournamentCommitment.tournament_id)
        .where(
            PlayerTournamentCommitment.user_id.in_(normalized),
            PlayerTournamentCommitment.released_at.is_(None),
        )
    )
    rows = (await db_session.execute(stmt)).all()
    return {
        commitment.user_id: ActiveCommitmentView(
            id=commitment.id,
            tournament_id=commitment.tournament_id,
            tournament_slug=str(tournament_slug),
            tournament_name=str(tournament_name),
            assignment_run_id=commitment.assignment_run_id,
            team_id=commitment.team_id,
            team_name=commitment.team_name,
            activated_at=commitment.activated_at,
        )
        for commitment, tournament_slug, tournament_name in rows
    }


async def create_assignment_commitments(
    db_session: AsyncSession,
    *,
    run_row: TournamentDeadlockAssignmentRun,
    activated_at: datetime,
) -> tuple[PlayerTournamentCommitment, ...]:
    roster = assignment_roster_members(run_row)
    conflicts = await active_commitments_by_user(
        db_session,
        [member.user_id for member in roster],
    )
    if conflicts:
        raise PlayerCommitmentConflict(conflicts.values())

    commitments = tuple(
        PlayerTournamentCommitment(
            user_id=member.user_id,
            tournament_id=run_row.tournament_id,
            assignment_run_id=run_row.id,
            team_id=member.team_id,
            team_name=member.team_name,
            activated_at=activated_at,
        )
        for member in roster
    )
    db_session.add_all(commitments)
    await db_session.flush()
    return commitments


async def release_active_commitments(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    released_at: datetime,
    release_reason: str,
    team_ids: Iterable[str] | None = None,
    user_ids: Iterable[str] | None = None,
) -> int:
    stmt = (
        update(PlayerTournamentCommitment)
        .where(
            PlayerTournamentCommitment.tournament_id == tournament_id,
            PlayerTournamentCommitment.released_at.is_(None),
        )
        .values(released_at=released_at, release_reason=release_reason)
    )
    normalized_team_ids = tuple(sorted({str(team_id) for team_id in team_ids or () if str(team_id)}))
    normalized_user_ids = tuple(sorted({str(user_id) for user_id in user_ids or () if str(user_id)}))
    if team_ids is not None and not normalized_team_ids:
        return 0
    if user_ids is not None and not normalized_user_ids:
        return 0
    if normalized_team_ids:
        stmt = stmt.where(PlayerTournamentCommitment.team_id.in_(normalized_team_ids))
    if normalized_user_ids:
        stmt = stmt.where(PlayerTournamentCommitment.user_id.in_(normalized_user_ids))
    result = await db_session.execute(stmt)
    return int(result.rowcount or 0)


async def reactivate_team_commitments(
    db_session: AsyncSession,
    *,
    run_row: TournamentDeadlockAssignmentRun,
    team_id: str,
    activated_at: datetime,
) -> int:
    team_roster = tuple(
        member for member in assignment_roster_members(run_row) if member.team_id == str(team_id)
    )
    return await reactivate_roster_members(
        db_session,
        run_row=run_row,
        roster_members=team_roster,
        activated_at=activated_at,
    )


async def reactivate_roster_members(
    db_session: AsyncSession,
    *,
    run_row: TournamentDeadlockAssignmentRun,
    roster_members: Iterable[AssignmentRosterMember],
    activated_at: datetime,
) -> int:
    roster = tuple(roster_members)
    await lock_commitment_users(db_session, [member.user_id for member in roster])
    existing = await active_commitments_by_user(db_session, [member.user_id for member in roster])
    roster_by_user_id = {member.user_id: member for member in roster}
    conflicts = {
        user_id: commitment
        for user_id, commitment in existing.items()
        if commitment.tournament_id != run_row.tournament_id
        or commitment.assignment_run_id != run_row.id
        or commitment.team_id != roster_by_user_id[user_id].team_id
    }
    if conflicts:
        raise PlayerCommitmentConflict(conflicts.values())
    missing = [member for member in roster if member.user_id not in existing]
    db_session.add_all(
        PlayerTournamentCommitment(
            user_id=member.user_id,
            tournament_id=run_row.tournament_id,
            assignment_run_id=run_row.id,
            team_id=member.team_id,
            team_name=member.team_name,
            activated_at=activated_at,
        )
        for member in missing
    )
    await db_session.flush()
    return len(missing)


async def reactivate_viable_tournament_commitments(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    activated_at: datetime,
) -> int:
    run_row = await db_session.scalar(
        select(TournamentDeadlockAssignmentRun)
        .where(
            TournamentDeadlockAssignmentRun.tournament_id == tournament_id,
            TournamentDeadlockAssignmentRun.status == "locked",
        )
        .order_by(
            TournamentDeadlockAssignmentRun.locked_at.desc().nullslast(),
            TournamentDeadlockAssignmentRun.created_at.desc(),
            TournamentDeadlockAssignmentRun.id.desc(),
        )
        .limit(1)
    )
    if run_row is None:
        return 0

    eliminated_team_ids: set[str] = set()
    completed_matches = (
        await db_session.scalars(
            select(TournamentMatch).where(
                TournamentMatch.tournament_id == tournament_id,
                TournamentMatch.status == "completed",
            )
        )
    ).all()
    for match in completed_matches:
        team_ids = (match.home_team_id, match.away_team_id)
        if match.winner_team_id in team_ids:
            eliminated_team_ids.update(
                str(team_id)
                for team_id in team_ids
                if team_id and team_id != match.winner_team_id
            )

    viable_roster = tuple(
        member
        for member in assignment_roster_members(run_row)
        if member.team_id not in eliminated_team_ids
    )
    return await reactivate_roster_members(
        db_session,
        run_row=run_row,
        roster_members=viable_roster,
        activated_at=activated_at,
    )


async def reconcile_player_commitments(
    db_session: AsyncSession,
    *,
    now: datetime,
) -> CommitmentReconciliationResult:
    terminal_result = await db_session.execute(
        update(PlayerTournamentCommitment)
        .where(
            PlayerTournamentCommitment.released_at.is_(None),
            exists(
                select(1).where(
                    Tournament.id == PlayerTournamentCommitment.tournament_id,
                    Tournament.status.in_(("completed", "cancelled")),
                )
            ),
        )
        .values(
            released_at=now,
            release_reason="tournament_terminal_reconciled",
        )
    )
    eliminated_result = await db_session.execute(
        update(PlayerTournamentCommitment)
        .where(
            PlayerTournamentCommitment.released_at.is_(None),
            exists(
                select(1).where(
                    TournamentMatch.tournament_id == PlayerTournamentCommitment.tournament_id,
                    TournamentMatch.status == "completed",
                    or_(
                        TournamentMatch.home_team_id == PlayerTournamentCommitment.team_id,
                        TournamentMatch.away_team_id == PlayerTournamentCommitment.team_id,
                    ),
                    TournamentMatch.winner_team_id.is_not(None),
                    or_(
                        TournamentMatch.winner_team_id == TournamentMatch.home_team_id,
                        TournamentMatch.winner_team_id == TournamentMatch.away_team_id,
                    ),
                    TournamentMatch.winner_team_id.is_distinct_from(PlayerTournamentCommitment.team_id),
                )
            ),
        )
        .values(released_at=now, release_reason="team_eliminated_reconciled")
    )

    active_rows = (
        await db_session.execute(
            select(
                PlayerTournamentCommitment.id,
                PlayerTournamentCommitment.user_id,
                PlayerTournamentCommitment.team_id,
                PlayerTournamentCommitment.assignment_run_id,
            ).where(PlayerTournamentCommitment.released_at.is_(None))
        )
    ).all()
    run_ids = sorted({str(row.assignment_run_id) for row in active_rows})
    run_rows = []
    if run_ids:
        run_rows = (
            await db_session.scalars(
                select(TournamentDeadlockAssignmentRun).where(
                    TournamentDeadlockAssignmentRun.id.in_(run_ids)
                )
            )
        ).all()
    run_state = {
        run_row.id: (
            run_row.status,
            {
                (member.user_id, member.team_id)
                for member in assignment_roster_members(run_row)
            },
        )
        for run_row in run_rows
    }
    mismatched_ids: list[str] = []
    for row in active_rows:
        status_and_roster = run_state.get(str(row.assignment_run_id))
        if (
            status_and_roster is None
            or status_and_roster[0] != "locked"
            or (str(row.user_id), str(row.team_id)) not in status_and_roster[1]
        ):
            mismatched_ids.append(str(row.id))
    mismatched_count = 0
    if mismatched_ids:
        mismatch_result = await db_session.execute(
            update(PlayerTournamentCommitment)
            .where(PlayerTournamentCommitment.id.in_(mismatched_ids))
            .values(released_at=now, release_reason="roster_mismatch_reconciled")
        )
        mismatched_count = int(mismatch_result.rowcount or 0)

    return CommitmentReconciliationResult(
        terminal_released=int(terminal_result.rowcount or 0),
        eliminated_released=int(eliminated_result.rowcount or 0),
        mismatched_released=mismatched_count,
    )
