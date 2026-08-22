from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import case, delete, exists, func, literal, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from apps.platform_api.app.api.schemas import TournamentDeadlockReadyRoundResponse
from apps.platform_api.app.services.player_commitments import release_active_commitments
from apps.platform_api.app.services.player_commitments import (
    PlayerCommitmentConflict,
    create_assignment_commitments,
    lock_commitment_users,
)
from python_packages.platform_domain.deadlock import (
    AutoAssignmentEngine,
    AutoAssignmentError,
    AutoAssignmentRunFreshness,
    CaptainRoundState,
    ReadyCheckRoundState,
    build_auto_assignment_input_fingerprint,
    build_captain_team_dream_slot_rows,
    calculate_player_strength,
    captain_priority_bucket,
    evaluate_auto_assignment_run_freshness,
    prepare_captain_round_entries,
    transition_auto_assignment_run_status,
)
from python_packages.platform_domain.tournaments import (
    ExistingBracketMatchState,
    TournamentWorkflowError,
    ensure_deadlock_roster_staging_allowed,
    ensure_tournament_completion_has_final_result,
    is_solo_tournament_format,
    transition_tournament_status as domain_transition_tournament_status,
)
from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.models import (
    DeadlockDreamSlot,
    DeadlockProfile,
    PlayerProfile,
    PlayerTournamentCommitment,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainEntry,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentDeadlockReadyVote,
    TournamentDeadlockReadyVoteCountShard,
    TournamentParticipant,
    User,
    TournamentMatch,
    new_uuid,
)
from python_packages.platform_infra.performance import measure_compute_block


INACTIVE_PARTICIPANT_STATUSES = ("withdrawn", "disqualified")


class TournamentStatusTransitionError(TournamentWorkflowError):
    """The requested status is not legal for the locked current state."""


class TournamentCompletionError(TournamentWorkflowError):
    """The bracket is not complete enough for a terminal completion."""


@dataclass(frozen=True, slots=True)
class TournamentStatusTransition:
    tournament: Tournament
    from_status: str
    to_status: str
    released_commitments: int
    has_locked_deadlock_roster: bool


async def lock_tournament_for_workflow(
    db_session: AsyncSession,
    tournament_id: str,
) -> Tournament:
    """Lock the authoritative tournament row for every workflow writer."""

    tournament = await db_session.scalar(
        select(Tournament)
        .where(Tournament.id == tournament_id)
        .with_for_update()
    )
    if tournament is None:
        raise TournamentWorkflowError("Tournament no longer exists.")
    return tournament


async def tournament_has_locked_deadlock_roster(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
) -> bool:
    if not is_solo_tournament_format(tournament.format_slug):
        return False
    locked_run_id = await db_session.scalar(
        select(TournamentDeadlockAssignmentRun.id)
        .where(
            TournamentDeadlockAssignmentRun.tournament_id == tournament.id,
            TournamentDeadlockAssignmentRun.status == "locked",
        )
        .order_by(
            TournamentDeadlockAssignmentRun.locked_at.desc().nullslast(),
            TournamentDeadlockAssignmentRun.created_at.desc(),
        )
    )
    return locked_run_id is not None


async def transition_locked_tournament_status(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    next_status: str,
    now: datetime,
    actor_user_id: str | None,
    audit_action: str,
    expected_status: str | None = None,
    audit_payload: dict[str, object] | None = None,
) -> TournamentStatusTransition:
    """Apply one status transition to a row already locked by this transaction."""

    if expected_status is not None and tournament.status != expected_status:
        raise TournamentStatusTransitionError(
            f"Tournament status changed from {expected_status} before this workflow step."
        )

    has_locked_deadlock_roster = await tournament_has_locked_deadlock_roster(
        db_session,
        tournament=tournament,
    )
    current_status = tournament.status
    try:
        resolved_status = domain_transition_tournament_status(
            current_status,
            next_status,
            format_slug=tournament.format_slug,
            has_locked_deadlock_roster=has_locked_deadlock_roster,
        )
    except TournamentWorkflowError as exc:
        raise TournamentStatusTransitionError(str(exc)) from exc

    if resolved_status == "completed":
        existing_match_states = tuple(
            ExistingBracketMatchState(
                round_number=int(round_number),
                status=str(match_status),
            )
            for round_number, match_status in (
                await db_session.execute(
                    select(TournamentMatch.round_number, TournamentMatch.status).where(
                        TournamentMatch.tournament_id == tournament.id
                    )
                )
            ).all()
        )
        unfinished_matches = sum(
            1
            for match in existing_match_states
            if match.status not in ("completed", "cancelled")
        )
        if unfinished_matches:
            raise TournamentCompletionError(
                "All matches must be completed or cancelled before finishing the tournament."
            )
        try:
            ensure_tournament_completion_has_final_result(existing_match_states)
        except TournamentWorkflowError as exc:
            raise TournamentCompletionError(str(exc)) from exc

    tournament.status = resolved_status
    if resolved_status == "registration_closed":
        tournament.registration_closes_at = now

    released_commitments = 0
    if resolved_status in {"completed", "cancelled"}:
        released_commitments = await release_active_commitments(
            db_session,
            tournament_id=tournament.id,
            released_at=now,
            release_reason=(
                "tournament_completed"
                if resolved_status == "completed"
                else "tournament_cancelled"
            ),
        )

    payload: dict[str, object] = {
        "tournament_slug": tournament.slug,
        "from_status": current_status,
        "to_status": resolved_status,
        "released_commitments": released_commitments,
    }
    if audit_payload:
        payload.update(audit_payload)
    await write_audit_log(
        db_session,
        actor_user_id=actor_user_id,
        action=audit_action,
        subject_type="tournament",
        subject_id=tournament.id,
        payload=payload,
    )
    return TournamentStatusTransition(
        tournament=tournament,
        from_status=current_status,
        to_status=resolved_status,
        released_commitments=released_commitments,
        has_locked_deadlock_roster=has_locked_deadlock_roster,
    )


async def transition_tournament_status(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    next_status: str,
    now: datetime,
    actor_user_id: str | None,
    audit_action: str,
    expected_status: str | None = None,
    audit_payload: dict[str, object] | None = None,
) -> TournamentStatusTransition:
    """Lock and apply a status transition for API and automation callers."""

    tournament = await lock_tournament_for_workflow(db_session, tournament_id)
    return await transition_locked_tournament_status(
        db_session,
        tournament=tournament,
        next_status=next_status,
        now=now,
        actor_user_id=actor_user_id,
        audit_action=audit_action,
        expected_status=expected_status,
        audit_payload=audit_payload,
    )
@dataclass(frozen=True, slots=True)
class DeadlockAutoAssignmentInputs:
    captain_round: TournamentDeadlockCaptainRound
    captain_rows: tuple[dict[str, Any], ...]
    ready_player_rows: tuple[dict[str, Any], ...]
    dream_slot_rows: tuple[dict[str, Any], ...]
    input_fingerprint: dict[str, list[dict[str, Any]]]

@dataclass(frozen=True, slots=True)
class ReadyRoundStateSnapshot:
    response: TournamentDeadlockReadyRoundResponse
    choices_by_user_id: dict[str, str]

@dataclass(frozen=True, slots=True)
class ReadyVotePreflight:
    active_round: TournamentDeadlockReadyRound | None
    has_participant: bool
    has_deadlock_profile: bool
    has_locked_roster: bool

@dataclass(frozen=True, slots=True)
class ReadyVoteRoutePreflight:
    tournament: Tournament
    active_round: TournamentDeadlockReadyRound | None
    has_participant: bool
    has_deadlock_profile: bool
    has_locked_roster: bool

@dataclass(frozen=True, slots=True)
class ReadyCheckReadPreflight:
    tournament: Tournament
    active_round: TournamentDeadlockReadyRound | None
    latest_round: TournamentDeadlockReadyRound | None
    has_participant: bool

def participant_status_is_inactive(participant_status: str) -> bool:
    return participant_status in INACTIVE_PARTICIPANT_STATUSES

def ready_vote_requires_automation(
    tournament: Tournament,
    active_round: TournamentDeadlockReadyRound | None,
    *,
    now: datetime,
) -> bool:
    if active_round is None:
        return True
    if tournament.ready_check_ends_at is not None and now >= tournament.ready_check_ends_at:
        return True
    if tournament.captain_selection_starts_at is not None and now >= tournament.captain_selection_starts_at:
        return True
    return False

async def deadlock_ready_round_for_tournament(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    active_only: bool = False,
) -> TournamentDeadlockReadyRound | None:
    stmt = (
        select(TournamentDeadlockReadyRound)
        .where(TournamentDeadlockReadyRound.tournament_id == tournament_id)
        .order_by(TournamentDeadlockReadyRound.created_at.desc(), TournamentDeadlockReadyRound.id.desc())
    )
    if active_only:
        stmt = stmt.where(TournamentDeadlockReadyRound.status == "active")
    return await db_session.scalar(stmt)

async def deadlock_ready_state_round_for_tournament(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> tuple[TournamentDeadlockReadyRound | None, TournamentDeadlockReadyRound | None]:
    round_row = await db_session.scalar(
        select(TournamentDeadlockReadyRound)
        .where(TournamentDeadlockReadyRound.tournament_id == tournament_id)
        .order_by(
            case(
                (TournamentDeadlockReadyRound.status == "active", 0),
                else_=1,
            ),
            TournamentDeadlockReadyRound.created_at.desc(),
            TournamentDeadlockReadyRound.id.desc(),
        )
        .limit(1)
    )
    if round_row is None:
        return None, None
    if round_row.status == "active":
        return round_row, round_row
    return None, round_row

async def deadlock_ready_check_read_preflight(
    db_session: AsyncSession,
    *,
    slug: str,
    user_id: str,
) -> ReadyCheckReadPreflight:
    ready_round = aliased(TournamentDeadlockReadyRound)
    selected_round_id = (
        select(TournamentDeadlockReadyRound.id)
        .where(TournamentDeadlockReadyRound.tournament_id == Tournament.id)
        .order_by(
            case(
                (TournamentDeadlockReadyRound.status == "active", 0),
                else_=1,
            ),
            TournamentDeadlockReadyRound.created_at.desc(),
            TournamentDeadlockReadyRound.id.desc(),
        )
        .limit(1)
        .correlate(Tournament)
        .scalar_subquery()
    )
    participant_exists = (
        select(TournamentParticipant.id)
        .where(
            TournamentParticipant.tournament_id == Tournament.id,
            TournamentParticipant.user_id == user_id,
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
        )
        .exists()
    )
    row = (
        await db_session.execute(
            select(
                Tournament,
                participant_exists.label("has_participant"),
                ready_round,
            )
            .outerjoin(ready_round, ready_round.id == selected_round_id)
            .where(Tournament.slug == slug)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tournament not found.")

    tournament = row[0]
    round_row = row[2]
    if round_row is None:
        active_round = None
        latest_round = None
    elif round_row.status == "active":
        active_round = round_row
        latest_round = round_row
    else:
        active_round = None
        latest_round = round_row
    return ReadyCheckReadPreflight(
        tournament=tournament,
        active_round=active_round,
        latest_round=latest_round,
        has_participant=bool(row.has_participant),
    )

def _ready_vote_preflight_columns(*, tournament_id: str, user_id: str) -> tuple[Any, Any, Any]:
    participant_exists = (
        select(TournamentParticipant.id)
        .where(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.user_id == user_id,
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
        )
        .exists()
    )
    profile_exists = (
        select(DeadlockProfile.user_id)
        .where(DeadlockProfile.user_id == user_id)
        .exists()
    )
    locked_roster_exists = (
        select(TournamentDeadlockAssignmentRun.id)
        .where(
            TournamentDeadlockAssignmentRun.tournament_id == tournament_id,
            TournamentDeadlockAssignmentRun.status == "locked",
        )
        .exists()
    )
    return participant_exists, profile_exists, locked_roster_exists

async def deadlock_ready_vote_preflight(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    user_id: str,
) -> ReadyVotePreflight:
    participant_exists, profile_exists, locked_roster_exists = _ready_vote_preflight_columns(
        tournament_id=tournament_id,
        user_id=user_id,
    )
    row = (
        await db_session.execute(
            select(
                TournamentDeadlockReadyRound,
                participant_exists.label("has_participant"),
                profile_exists.label("has_deadlock_profile"),
                locked_roster_exists.label("has_locked_roster"),
            )
            .where(
                TournamentDeadlockReadyRound.tournament_id == tournament_id,
                TournamentDeadlockReadyRound.status == "active",
            )
            .order_by(
                TournamentDeadlockReadyRound.created_at.desc(),
                TournamentDeadlockReadyRound.id.desc(),
            )
            .limit(1)
        )
    ).first()
    if row is not None:
        return ReadyVotePreflight(
            active_round=row[0],
            has_participant=bool(row.has_participant),
            has_deadlock_profile=bool(row.has_deadlock_profile),
            has_locked_roster=bool(row.has_locked_roster),
        )

    flags = (
        await db_session.execute(
            select(
                participant_exists.label("has_participant"),
                profile_exists.label("has_deadlock_profile"),
                locked_roster_exists.label("has_locked_roster"),
            )
        )
    ).one()
    return ReadyVotePreflight(
        active_round=None,
        has_participant=bool(flags.has_participant),
        has_deadlock_profile=bool(flags.has_deadlock_profile),
        has_locked_roster=bool(flags.has_locked_roster),
    )

async def deadlock_ready_vote_route_preflight(
    db_session: AsyncSession,
    *,
    slug: str,
    user_id: str,
) -> ReadyVoteRoutePreflight:
    active_round = aliased(TournamentDeadlockReadyRound)
    selected_active_round_id = (
        select(TournamentDeadlockReadyRound.id)
        .where(
            TournamentDeadlockReadyRound.tournament_id == Tournament.id,
            TournamentDeadlockReadyRound.status == "active",
        )
        .order_by(
            TournamentDeadlockReadyRound.created_at.desc(),
            TournamentDeadlockReadyRound.id.desc(),
        )
        .limit(1)
        .correlate(Tournament)
        .scalar_subquery()
    )
    participant_exists = (
        select(TournamentParticipant.id)
        .where(
            TournamentParticipant.tournament_id == Tournament.id,
            TournamentParticipant.user_id == user_id,
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
        )
        .exists()
    )
    profile_exists = (
        select(DeadlockProfile.user_id)
        .where(DeadlockProfile.user_id == user_id)
        .exists()
    )
    locked_roster_exists = (
        select(TournamentDeadlockAssignmentRun.id)
        .where(
            TournamentDeadlockAssignmentRun.tournament_id == Tournament.id,
            TournamentDeadlockAssignmentRun.status == "locked",
        )
        .exists()
    )
    row = (
        await db_session.execute(
            select(
                Tournament,
                active_round,
                participant_exists.label("has_participant"),
                profile_exists.label("has_deadlock_profile"),
                locked_roster_exists.label("has_locked_roster"),
            )
            .outerjoin(active_round, active_round.id == selected_active_round_id)
            .where(Tournament.slug == slug)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tournament not found.")
    return ReadyVoteRoutePreflight(
        tournament=row[0],
        active_round=row[1],
        has_participant=bool(row.has_participant),
        has_deadlock_profile=bool(row.has_deadlock_profile),
        has_locked_roster=bool(row.has_locked_roster),
    )

async def upsert_deadlock_ready_vote(
    db_session: AsyncSession,
    *,
    round_id: int,
    user_id: str,
    choice: str,
    responded_at: datetime,
) -> bool:
    insert_stmt = postgresql.insert(TournamentDeadlockReadyVote).values(
        id=new_uuid(),
        round_id=round_id,
        user_id=user_id,
        choice=choice,
        responded_at=responded_at,
    )
    changed_vote_id = await db_session.scalar(
        insert_stmt.on_conflict_do_update(
            constraint="uq_tournament_deadlock_ready_votes_round_user",
            set_={
                "choice": choice,
                "responded_at": responded_at,
                "updated_at": responded_at,
            },
            where=TournamentDeadlockReadyVote.choice != choice,
        ).returning(TournamentDeadlockReadyVote.id)
    )
    return changed_vote_id is not None

async def serialize_deadlock_ready_round(
    db_session: AsyncSession,
    round_row: TournamentDeadlockReadyRound,
    *,
    current_user_id: str | None = None,
) -> TournamentDeadlockReadyRoundResponse:
    ready_count = (
        select(func.coalesce(func.sum(TournamentDeadlockReadyVoteCountShard.vote_count), 0))
        .where(
            TournamentDeadlockReadyVoteCountShard.round_id == round_row.id,
            TournamentDeadlockReadyVoteCountShard.choice == "yes",
        )
        .scalar_subquery()
    )
    declined_count = (
        select(func.coalesce(func.sum(TournamentDeadlockReadyVoteCountShard.vote_count), 0))
        .where(
            TournamentDeadlockReadyVoteCountShard.round_id == round_row.id,
            TournamentDeadlockReadyVoteCountShard.choice == "no",
        )
        .scalar_subquery()
    )
    columns = [
        ready_count.label("ready_count"),
        declined_count.label("declined_count"),
    ]
    if current_user_id is not None:
        columns.append(
            (
                select(TournamentDeadlockReadyVote.choice)
                .where(
                    TournamentDeadlockReadyVote.round_id == round_row.id,
                    TournamentDeadlockReadyVote.user_id == current_user_id,
                )
                .limit(1)
                .scalar_subquery()
            ).label("current_user_choice")
        )
    aggregate_row = (await db_session.execute(select(*columns))).one()
    ready_count = aggregate_row[0]
    declined_count = aggregate_row[1]
    current_user_choice = aggregate_row[2] if current_user_id is not None else None
    return TournamentDeadlockReadyRoundResponse(
        id=round_row.id,
        tournament_id=round_row.tournament_id,
        status=round_row.status,
        eligible_participant_count=len(list(round_row.eligible_user_ids or [])),
        ready_count=int(ready_count or 0),
        declined_count=int(declined_count or 0),
        initiated_by_user_id=round_row.initiated_by_user_id,
        created_at=round_row.created_at,
        closed_at=round_row.closed_at,
        current_user_choice=current_user_choice,
    )

async def build_deadlock_ready_round_state_snapshot(
    db_session: AsyncSession,
    round_row: TournamentDeadlockReadyRound,
) -> ReadyRoundStateSnapshot:
    vote_rows = await db_session.execute(
        select(
            TournamentDeadlockReadyVote.user_id,
            TournamentDeadlockReadyVote.choice,
        ).where(TournamentDeadlockReadyVote.round_id == round_row.id)
    )
    choices_by_user_id = {
        str(row.user_id): str(row.choice)
        for row in vote_rows
    }
    ready_count = sum(1 for choice in choices_by_user_id.values() if choice == "yes")
    declined_count = sum(1 for choice in choices_by_user_id.values() if choice == "no")
    return ReadyRoundStateSnapshot(
        response=TournamentDeadlockReadyRoundResponse(
            id=round_row.id,
            tournament_id=round_row.tournament_id,
            status=round_row.status,
            eligible_participant_count=len(list(round_row.eligible_user_ids or [])),
            ready_count=ready_count,
            declined_count=declined_count,
            initiated_by_user_id=round_row.initiated_by_user_id,
            created_at=round_row.created_at,
            closed_at=round_row.closed_at,
            current_user_choice=None,
        ),
        choices_by_user_id=choices_by_user_id,
    )

async def deadlock_closed_ready_round_for_tournament(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> TournamentDeadlockReadyRound | None:
    return await db_session.scalar(
        select(TournamentDeadlockReadyRound)
        .where(
            TournamentDeadlockReadyRound.tournament_id == tournament_id,
            TournamentDeadlockReadyRound.status == "closed",
        )
        .order_by(TournamentDeadlockReadyRound.created_at.desc(), TournamentDeadlockReadyRound.id.desc())
    )

async def deadlock_captain_round_for_tournament(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    active_only: bool = False,
) -> TournamentDeadlockCaptainRound | None:
    stmt = (
        select(TournamentDeadlockCaptainRound)
        .where(TournamentDeadlockCaptainRound.tournament_id == tournament_id)
        .order_by(TournamentDeadlockCaptainRound.created_at.desc(), TournamentDeadlockCaptainRound.id.desc())
    )
    if active_only:
        stmt = stmt.where(TournamentDeadlockCaptainRound.status == "active")
    return await db_session.scalar(stmt)

async def deadlock_finalized_captain_round_for_tournament(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> TournamentDeadlockCaptainRound | None:
    return await db_session.scalar(
        select(TournamentDeadlockCaptainRound)
        .where(
            TournamentDeadlockCaptainRound.tournament_id == tournament_id,
            TournamentDeadlockCaptainRound.status == "finalized",
        )
        .order_by(
            TournamentDeadlockCaptainRound.finalized_at.desc(),
            TournamentDeadlockCaptainRound.id.desc(),
        )
    )

async def deadlock_captain_entries_for_round(
    db_session: AsyncSession,
    *,
    round_id: int,
    for_update: bool = False,
) -> list[TournamentDeadlockCaptainEntry]:
    stmt = (
        select(TournamentDeadlockCaptainEntry)
        .where(TournamentDeadlockCaptainEntry.round_id == round_id)
        .order_by(
            TournamentDeadlockCaptainEntry.offer_order.asc(),
            TournamentDeadlockCaptainEntry.created_at.asc(),
        )
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (
        await db_session.scalars(stmt)
    ).all()

async def prune_participant_from_active_ready_round(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    user_id: str,
    actor_user_id: str,
    now: datetime,
    participant_status: str,
) -> TournamentDeadlockReadyRound | None:
    active_round = await deadlock_ready_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_round is None:
        return None

    vote_rows = (
        await db_session.scalars(
            select(TournamentDeadlockReadyVote).where(
                TournamentDeadlockReadyVote.round_id == active_round.id
            )
        )
    ).all()
    round_state = ReadyCheckRoundState.active(
        round_id=active_round.id,
        eligible_user_ids=list(active_round.eligible_user_ids or []),
        votes=[
            {"user_id": row.user_id, "choice": row.choice}
            for row in vote_rows
        ],
    )
    next_state = round_state.exclude_user(user_id)
    if next_state == round_state:
        return None

    active_round.status = next_state.status
    active_round.eligible_user_ids = list(next_state.eligible_user_ids)
    active_round.closed_at = now if next_state.status != "active" else None
    await db_session.execute(
        delete(TournamentDeadlockReadyVote).where(
            TournamentDeadlockReadyVote.round_id == active_round.id,
            TournamentDeadlockReadyVote.user_id == user_id,
        )
    )
    await write_audit_log(
        db_session,
        actor_user_id=actor_user_id,
        action="tournament.deadlock.ready_check.exclude_participant",
        subject_type="tournament_deadlock_ready_round",
        subject_id=str(active_round.id),
        payload={
            "tournament_slug": tournament.slug,
            "user_id": user_id,
            "participant_status": participant_status,
            "round_status": active_round.status,
            "eligible_participant_count": len(active_round.eligible_user_ids or []),
        },
    )
    return active_round

async def prune_participant_from_active_captain_round(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    user_id: str,
    actor_user_id: str,
    now: datetime,
    participant_status: str,
) -> TournamentDeadlockCaptainRound | None:
    active_round = await deadlock_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_round is None:
        return None

    entry_rows = await deadlock_captain_entries_for_round(
        db_session,
        round_id=active_round.id,
        for_update=True,
    )
    round_state = CaptainRoundState.active(
        round_id=active_round.id,
        teams_count=active_round.teams_count,
        entries=[
            {
                "user_id": row.user_id,
                "offer_order": row.offer_order,
                "state": row.state,
                "assigned_team_id": row.assigned_team_id,
            }
            for row in entry_rows
        ],
    )
    next_state = round_state.exclude_user(user_id)
    if next_state == round_state:
        return None

    next_entries_by_user_id = {entry.user_id: entry for entry in next_state.entries}
    for row in entry_rows:
        if row.user_id == user_id:
            await db_session.execute(
                delete(TournamentDeadlockCaptainEntry).where(
                    TournamentDeadlockCaptainEntry.id == row.id
                )
            )
            continue
        next_entry = next_entries_by_user_id[row.user_id]
        if row.state != next_entry.state and next_entry.state == "cancelled":
            row.responded_at = now
        row.state = next_entry.state
        row.assigned_team_id = next_entry.assigned_team_id

    active_round.status = next_state.status
    active_round.closed_at = now if next_state.status != "active" else None
    await write_audit_log(
        db_session,
        actor_user_id=actor_user_id,
        action="tournament.deadlock.captain_round.exclude_participant",
        subject_type="tournament_deadlock_captain_round",
        subject_id=str(active_round.id),
        payload={
            "tournament_slug": tournament.slug,
            "user_id": user_id,
            "participant_status": participant_status,
            "round_status": active_round.status,
            "candidate_count": next_state.candidate_count,
            "accepted_count": next_state.accepted_count,
            "offered_count": next_state.offered_count,
        },
    )
    return active_round

async def deadlock_ready_candidate_rows_for_round(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    round_id: int,
) -> list[dict[str, object]]:
    rows = await db_session.execute(
        select(
            User.id.label("user_id"),
            func.coalesce(PlayerProfile.display_name, User.display_name).label("display_name"),
            DeadlockProfile.rank,
            DeadlockProfile.subrank,
            DeadlockProfile.playtime,
            DeadlockProfile.captain_priority,
        )
        .select_from(TournamentDeadlockReadyVote)
        .join(User, User.id == TournamentDeadlockReadyVote.user_id)
        .outerjoin(PlayerProfile, PlayerProfile.user_id == TournamentDeadlockReadyVote.user_id)
        .join(DeadlockProfile, DeadlockProfile.user_id == TournamentDeadlockReadyVote.user_id)
        .join(
            TournamentParticipant,
            (TournamentParticipant.tournament_id == tournament_id)
            & (TournamentParticipant.user_id == TournamentDeadlockReadyVote.user_id),
        )
        .where(
            TournamentDeadlockReadyVote.round_id == round_id,
            TournamentDeadlockReadyVote.choice == "yes",
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
            ~exists(
                select(1).where(
                    PlayerTournamentCommitment.user_id == TournamentDeadlockReadyVote.user_id,
                    PlayerTournamentCommitment.released_at.is_(None),
                )
            ),
        )
    )
    return [dict(row._mapping) for row in rows]

def prepare_deadlock_captain_candidate_rows(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    prepared_rows: list[dict[str, object]] = []
    for row in rows:
        rank = row.get("rank")
        subrank = row.get("subrank")
        playtime = row.get("playtime")
        prepared_row = dict(row)
        prepared_row["user_id"] = str(row["user_id"])
        prepared_row["captain_priority_bucket"] = captain_priority_bucket(
            str(rank) if rank is not None else None,
            str(row["captain_priority"]) if row.get("captain_priority") is not None else None,
        )
        prepared_row["strength"] = (
            calculate_player_strength(str(rank), int(subrank), str(playtime))
            if rank is not None and subrank is not None and playtime is not None
            else 0.0
        )
        prepared_rows.append(prepared_row)
    return prepared_rows

async def deadlock_assigned_captain_rows_for_round(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    round_id: int,
) -> list[dict[str, object]]:
    entry_rows = (
        await db_session.scalars(
            select(TournamentDeadlockCaptainEntry)
            .where(
                TournamentDeadlockCaptainEntry.round_id == round_id,
            )
            .order_by(TournamentDeadlockCaptainEntry.assigned_team_id.asc())
        )
    ).all()
    if not entry_rows:
        return []

    team_ids_by_user_id = {
        row.user_id: row.assigned_team_id or str(row.offer_order)
        for row in entry_rows
        if row.state == "assigned" or row.assigned_team_id is not None
    }
    if not team_ids_by_user_id:
        round_row = await db_session.scalar(
            select(TournamentDeadlockCaptainRound).where(TournamentDeadlockCaptainRound.id == round_id)
        )
        teams_count = int(round_row.teams_count if round_row is not None else 0)
        team_ids_by_user_id = {
            row.user_id: str(index)
            for index, row in enumerate(entry_rows[:teams_count], start=1)
        }
    rows = await db_session.execute(
        select(
            User.id.label("user_id"),
            func.coalesce(PlayerProfile.display_name, User.display_name).label("username"),
            DeadlockProfile.rank,
            DeadlockProfile.subrank,
            DeadlockProfile.playtime,
            DeadlockProfile.pool,
            DeadlockProfile.roles,
            PlayerProfile.captain_team_name.label("team_name"),
        )
        .select_from(User)
        .outerjoin(PlayerProfile, PlayerProfile.user_id == User.id)
        .join(DeadlockProfile, DeadlockProfile.user_id == User.id)
        .where(
            User.id.in_(list(team_ids_by_user_id)),
            ~exists(
                select(1).where(
                    PlayerTournamentCommitment.user_id == User.id,
                    PlayerTournamentCommitment.released_at.is_(None),
                )
            ),
        )
    )
    prepared_rows = []
    for row in rows:
        prepared_row = dict(row._mapping)
        prepared_row["team_id"] = team_ids_by_user_id[str(prepared_row["user_id"])]
        prepared_rows.append(prepared_row)
    return prepared_rows

async def deadlock_ready_player_rows_for_assignment(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    source_ready_round_id: int,
    exclude_user_ids: list[str],
) -> list[dict[str, object]]:
    stmt = (
        select(
            TournamentDeadlockReadyVote.user_id.label("user_id"),
            func.coalesce(PlayerProfile.display_name, User.display_name).label("username"),
            DeadlockProfile.rank,
            DeadlockProfile.subrank,
            DeadlockProfile.playtime,
            DeadlockProfile.pool,
            DeadlockProfile.roles,
        )
        .select_from(TournamentDeadlockReadyVote)
        .join(User, User.id == TournamentDeadlockReadyVote.user_id)
        .outerjoin(PlayerProfile, PlayerProfile.user_id == TournamentDeadlockReadyVote.user_id)
        .join(DeadlockProfile, DeadlockProfile.user_id == TournamentDeadlockReadyVote.user_id)
        .join(
            TournamentParticipant,
            (TournamentParticipant.tournament_id == tournament_id)
            & (TournamentParticipant.user_id == TournamentDeadlockReadyVote.user_id),
        )
        .where(
            TournamentDeadlockReadyVote.round_id == source_ready_round_id,
            TournamentDeadlockReadyVote.choice == "yes",
            TournamentParticipant.status.not_in(("withdrawn", "disqualified")),
            ~exists(
                select(1).where(
                    PlayerTournamentCommitment.user_id == TournamentDeadlockReadyVote.user_id,
                    PlayerTournamentCommitment.released_at.is_(None),
                )
            ),
        )
    )
    if exclude_user_ids:
        stmt = stmt.where(TournamentDeadlockReadyVote.user_id.not_in(exclude_user_ids))
    rows = await db_session.execute(stmt)
    return [dict(row._mapping) for row in rows]

async def deadlock_ready_user_ids_for_round(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    round_id: int,
) -> tuple[str, ...]:
    user_ids = (
        await db_session.scalars(
            select(TournamentDeadlockReadyVote.user_id)
            .join(
                TournamentParticipant,
                (TournamentParticipant.tournament_id == tournament_id)
                & (TournamentParticipant.user_id == TournamentDeadlockReadyVote.user_id),
            )
            .where(
                TournamentDeadlockReadyVote.round_id == round_id,
                TournamentDeadlockReadyVote.choice == "yes",
                TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
            )
            .order_by(TournamentDeadlockReadyVote.user_id.asc())
        )
    ).all()
    return tuple(str(user_id) for user_id in user_ids)

async def reconcile_finalized_captain_round_for_availability(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    captain_round: TournamentDeadlockCaptainRound,
    now: datetime,
) -> tuple[str, ...]:
    candidate_rows = await deadlock_ready_candidate_rows_for_round(
        db_session,
        tournament_id=tournament.id,
        round_id=captain_round.source_ready_round_id,
    )
    required_players = int(captain_round.teams_count) * 7
    if len(candidate_rows) < required_players:
        raise TournamentWorkflowError(
            "Not enough globally available ready players to fill every requested team. "
            f"required={required_players} available={len(candidate_rows)}"
        )

    prepared_entries = prepare_captain_round_entries(
        prepare_deadlock_captain_candidate_rows(candidate_rows),
        captain_round.teams_count,
        auto_assign=True,
    )
    prepared_by_user_id = {entry.user_id: entry for entry in prepared_entries}
    entry_rows = await deadlock_captain_entries_for_round(
        db_session,
        round_id=captain_round.id,
        for_update=True,
    )
    existing_by_user_id = {row.user_id: row for row in entry_rows}
    unavailable_user_ids: list[str] = []
    for row in entry_rows:
        prepared = prepared_by_user_id.get(row.user_id)
        if prepared is None:
            unavailable_user_ids.append(row.user_id)
            row.state = "cancelled"
            row.assigned_team_id = None
            row.responded_at = now
            continue
        row.offer_order = prepared.offer_order
        row.state = prepared.state
        row.assigned_team_id = prepared.assigned_team_id
        row.responded_at = now if prepared.state == "assigned" else None

    for prepared in prepared_entries:
        if prepared.user_id in existing_by_user_id:
            continue
        db_session.add(
            TournamentDeadlockCaptainEntry(
                round_id=captain_round.id,
                user_id=prepared.user_id,
                offer_order=prepared.offer_order,
                state=prepared.state,
                assigned_team_id=prepared.assigned_team_id,
                responded_at=now if prepared.state == "assigned" else None,
            )
        )

    captain_round.status = "finalized"
    captain_round.closed_at = captain_round.closed_at or now
    captain_round.finalized_at = captain_round.finalized_at or now
    await db_session.flush()
    return tuple(sorted(unavailable_user_ids))

async def deadlock_auto_assignment_run_for_tournament(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> TournamentDeadlockAssignmentRun | None:
    return await db_session.scalar(
        select(TournamentDeadlockAssignmentRun)
        .where(TournamentDeadlockAssignmentRun.tournament_id == tournament_id)
        .order_by(
            TournamentDeadlockAssignmentRun.created_at.desc(),
            TournamentDeadlockAssignmentRun.id.desc(),
        )
    )

async def deadlock_published_auto_assignment_run_for_tournament(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> TournamentDeadlockAssignmentRun | None:
    return await db_session.scalar(
        select(TournamentDeadlockAssignmentRun)
        .where(
            TournamentDeadlockAssignmentRun.tournament_id == tournament_id,
            TournamentDeadlockAssignmentRun.status.in_(("published", "locked")),
        )
        .order_by(
            TournamentDeadlockAssignmentRun.locked_at.desc().nullslast(),
            TournamentDeadlockAssignmentRun.published_at.desc().nullslast(),
            TournamentDeadlockAssignmentRun.created_at.desc(),
        )
    )

async def deadlock_auto_assignment_state_runs_for_tournament(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> tuple[TournamentDeadlockAssignmentRun | None, TournamentDeadlockAssignmentRun | None]:
    latest_id = (
        select(TournamentDeadlockAssignmentRun.id)
        .where(TournamentDeadlockAssignmentRun.tournament_id == tournament_id)
        .order_by(
            TournamentDeadlockAssignmentRun.created_at.desc(),
            TournamentDeadlockAssignmentRun.id.desc(),
        )
        .limit(1)
        .scalar_subquery()
    )
    published_id = (
        select(TournamentDeadlockAssignmentRun.id)
        .where(
            TournamentDeadlockAssignmentRun.tournament_id == tournament_id,
            TournamentDeadlockAssignmentRun.status.in_(("published", "locked")),
        )
        .order_by(
            TournamentDeadlockAssignmentRun.locked_at.desc().nullslast(),
            TournamentDeadlockAssignmentRun.published_at.desc().nullslast(),
            TournamentDeadlockAssignmentRun.created_at.desc(),
        )
        .limit(1)
        .scalar_subquery()
    )
    latest_run = aliased(TournamentDeadlockAssignmentRun)
    published_run = aliased(TournamentDeadlockAssignmentRun)
    anchor = select(literal(1).label("anchor")).subquery()
    row = (
        await db_session.execute(
            select(latest_run, published_run)
            .select_from(anchor)
            .outerjoin(latest_run, latest_run.id == latest_id)
            .outerjoin(published_run, published_run.id == published_id)
        )
    ).first()
    if row is None:
        return None, None
    return row[0], row[1]

async def deadlock_locked_auto_assignment_run_for_tournament(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> TournamentDeadlockAssignmentRun | None:
    return await db_session.scalar(
        select(TournamentDeadlockAssignmentRun)
        .where(
            TournamentDeadlockAssignmentRun.tournament_id == tournament_id,
            TournamentDeadlockAssignmentRun.status == "locked",
        )
        .order_by(
            TournamentDeadlockAssignmentRun.locked_at.desc().nullslast(),
            TournamentDeadlockAssignmentRun.created_at.desc(),
        )
    )

async def deadlock_assignment_run_by_id_for_tournament(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    run_id: str,
) -> TournamentDeadlockAssignmentRun | None:
    return await db_session.scalar(
        select(TournamentDeadlockAssignmentRun).where(
            TournamentDeadlockAssignmentRun.tournament_id == tournament_id,
            TournamentDeadlockAssignmentRun.id == run_id,
        )
    )

def deadlock_auto_assignment_stale_reason_text(reason: str) -> str:
    if reason == "captain_round_changed":
        return "captain teams changed after this run was generated"
    if reason == "ready_round_changed":
        return "the source ready-check round changed after this run was generated"
    if reason == "captains_changed":
        return "captain profile inputs no longer match this run"
    if reason == "ready_players_changed":
        return "ready-player inputs no longer match this run"
    if reason == "dream_slots_changed":
        return "captain dream-slot templates changed after this run was generated"
    if reason == "captain_round_missing":
        return "the finalized captain round for this tournament is missing"
    if reason == "ready_round_missing":
        return "the source ready-check round for this tournament is missing"
    return reason.replace("_", " ")

def deadlock_auto_assignment_stale_detail(stale_reasons: tuple[str, ...]) -> str:
    details = ", ".join(deadlock_auto_assignment_stale_reason_text(reason) for reason in stale_reasons)
    return f"This auto-assignment run is stale: {details}. Generate a fresh run first."

def deadlock_auto_assignment_input_fingerprint_from_run(
    run_row: TournamentDeadlockAssignmentRun,
) -> dict[str, Any] | None:
    snapshot = dict(run_row.result_snapshot or {})
    fingerprint = snapshot.get("input_fingerprint")
    if not isinstance(fingerprint, dict):
        return None
    return fingerprint

async def build_deadlock_auto_assignment_inputs(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    captain_round: TournamentDeadlockCaptainRound,
) -> DeadlockAutoAssignmentInputs:
    captain_rows = tuple(
        await deadlock_assigned_captain_rows_for_round(
            db_session,
            tournament_id=tournament_id,
            round_id=captain_round.id,
        )
    )
    assigned_user_ids = [str(row["user_id"]) for row in captain_rows]
    ready_player_rows = tuple(
        await deadlock_ready_player_rows_for_assignment(
            db_session,
            tournament_id=tournament_id,
            source_ready_round_id=captain_round.source_ready_round_id,
            exclude_user_ids=assigned_user_ids,
        )
    )

    profile_slot_rows = (
        await db_session.scalars(
            select(DeadlockDreamSlot).where(
                DeadlockDreamSlot.user_id.in_(assigned_user_ids),
            )
        )
    ).all()
    combined_slot_rows = [
        {
            "user_id": row.user_id,
            "slot_number": row.slot_number,
            "allowed_roles": list(row.allowed_roles or []),
            "desired_heroes": list(row.desired_heroes or []),
        }
        for row in profile_slot_rows
    ]
    dream_slot_rows = tuple(
        build_captain_team_dream_slot_rows(
            captain_rows,
            combined_slot_rows,
        )
    )

    return DeadlockAutoAssignmentInputs(
        captain_round=captain_round,
        captain_rows=captain_rows,
        ready_player_rows=ready_player_rows,
        dream_slot_rows=dream_slot_rows,
        input_fingerprint=build_auto_assignment_input_fingerprint(
            captain_rows,
            ready_player_rows,
            dream_slot_rows,
        ),
    )

async def deadlock_latest_auto_assignment_inputs_for_tournament(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> DeadlockAutoAssignmentInputs | None:
    captain_round = await deadlock_finalized_captain_round_for_tournament(
        db_session,
        tournament_id=tournament_id,
    )
    if captain_round is None:
        return None
    return await build_deadlock_auto_assignment_inputs(
        db_session,
        tournament_id=tournament_id,
        captain_round=captain_round,
    )

async def deadlock_auto_assignment_run_freshness(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    run_row: TournamentDeadlockAssignmentRun,
    current_inputs: DeadlockAutoAssignmentInputs | None = None,
) -> AutoAssignmentRunFreshness:
    inputs = current_inputs or await deadlock_latest_auto_assignment_inputs_for_tournament(
        db_session,
        tournament_id=tournament_id,
    )
    return evaluate_auto_assignment_run_freshness(
        run_source_captain_round_id=run_row.source_captain_round_id,
        current_source_captain_round_id=inputs.captain_round.id if inputs is not None else None,
        run_source_ready_round_id=run_row.source_ready_round_id,
        current_source_ready_round_id=inputs.captain_round.source_ready_round_id if inputs is not None else None,
        stored_input_fingerprint=deadlock_auto_assignment_input_fingerprint_from_run(run_row),
        current_input_fingerprint=inputs.input_fingerprint if inputs is not None else None,
    )

async def generate_deadlock_auto_assignment_run_for_tournament(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    actor_user_id: str | None,
) -> TournamentDeadlockAssignmentRun:
    try:
        ensure_deadlock_roster_staging_allowed(
            format_slug=tournament.format_slug,
            tournament_status=tournament.status,
            has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                db_session,
                tournament=tournament,
            ),
            action_name="Deadlock auto-assignment",
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    published_run = await deadlock_published_auto_assignment_run_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    if published_run is not None and published_run.status == "locked":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The current published roster is locked. Unlocking is not supported; generate changes in a new tournament state instead.",
        )

    active_captain_round = await deadlock_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_captain_round is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Finalize or close the active captain round before running auto-assignment.",
        )

    captain_round = await deadlock_finalized_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    if captain_round is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Finalize a captain round before running auto-assignment.",
        )

    try:
        await reconcile_finalized_captain_round_for_availability(
            db_session,
            tournament=tournament,
            captain_round=captain_round,
            now=datetime.now(UTC),
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    current_inputs = await build_deadlock_auto_assignment_inputs(
        db_session,
        tournament_id=tournament.id,
        captain_round=captain_round,
    )
    latest_run = await deadlock_auto_assignment_run_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    if latest_run is not None:
        latest_run_freshness = await deadlock_auto_assignment_run_freshness(
            db_session,
            tournament_id=tournament.id,
            run_row=latest_run,
            current_inputs=current_inputs,
        )
        if not latest_run_freshness.is_stale:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "The latest auto-assignment run already matches the current captain, player, "
                    "and dream-slot inputs. Publish or lock it instead of rerunning."
                ),
            )

    captain_rows = list(current_inputs.captain_rows)
    if len(captain_rows) != captain_round.teams_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The finalized captain round does not have the required assigned captains. "
                f"expected={captain_round.teams_count} actual={len(captain_rows)}"
            ),
        )

    engine = AutoAssignmentEngine()
    try:
        with measure_compute_block():
            run = engine.solve(
                captain_rows,
                list(current_inputs.ready_player_rows),
                list(current_inputs.dream_slot_rows),
            )
    except AutoAssignmentError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    result_snapshot = dict(run.result_snapshot)
    result_snapshot["input_fingerprint"] = current_inputs.input_fingerprint

    run_row = TournamentDeadlockAssignmentRun(
        tournament_id=tournament.id,
        source_captain_round_id=captain_round.id,
        source_ready_round_id=captain_round.source_ready_round_id,
        created_by_user_id=actor_user_id,
        status="generated",
        summary_text=run.summary_text,
        result_snapshot=result_snapshot,
        candidate_pool_user_ids=[str(player.user_id) for player in run.candidate_pool],
        leftover_user_ids=[str(player.user_id) for player in run.leftovers],
    )
    db_session.add(run_row)
    await db_session.flush()

    await write_audit_log(
        db_session,
        actor_user_id=actor_user_id,
        action="tournament.deadlock.auto_assignment.run",
        subject_type="tournament_deadlock_assignment_run",
        subject_id=run_row.id,
        payload={
            "tournament_slug": tournament.slug,
            "source_captain_round_id": captain_round.id,
            "source_ready_round_id": captain_round.source_ready_round_id,
            "candidate_pool_size": len(run.candidate_pool),
            "leftover_count": len(run.leftovers),
            "spread_percent": round(run.optimization_summary.spread, 4),
            "mad_percent": round(run.optimization_summary.mad_percent, 4),
        },
    )
    await db_session.commit()
    await db_session.refresh(run_row)
    return run_row

async def finalize_deadlock_assignment_with_commitments(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    run_row: TournamentDeadlockAssignmentRun,
    actor_user_id: str | None,
    now: datetime,
) -> tuple[bool, tuple[str, ...]]:
    locked_run = await db_session.scalar(
        select(TournamentDeadlockAssignmentRun)
        .where(
            TournamentDeadlockAssignmentRun.id == run_row.id,
            TournamentDeadlockAssignmentRun.tournament_id == tournament.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_run is None:
        raise TournamentWorkflowError("The assignment run no longer exists.")
    run_row = locked_run
    if run_row.status == "locked":
        return False, ()
    if run_row.status != "published":
        raise TournamentWorkflowError("Publish the roster before locking it.")

    captain_round = await db_session.scalar(
        select(TournamentDeadlockCaptainRound)
        .where(TournamentDeadlockCaptainRound.id == run_row.source_captain_round_id)
        .with_for_update()
    )
    if captain_round is None or captain_round.status != "finalized":
        raise TournamentWorkflowError("The finalized captain round for this roster is missing.")

    ready_user_ids = await deadlock_ready_user_ids_for_round(
        db_session,
        tournament_id=tournament.id,
        round_id=captain_round.source_ready_round_id,
    )
    locked_user_ids = await lock_commitment_users(db_session, ready_user_ids)
    if len(locked_user_ids) != len(ready_user_ids):
        raise TournamentWorkflowError("One or more ready players no longer exist.")

    unavailable_user_ids = await reconcile_finalized_captain_round_for_availability(
        db_session,
        tournament=tournament,
        captain_round=captain_round,
        now=now,
    )
    current_inputs = await build_deadlock_auto_assignment_inputs(
        db_session,
        tournament_id=tournament.id,
        captain_round=captain_round,
    )
    captain_rows = list(current_inputs.captain_rows)
    if len(captain_rows) != captain_round.teams_count:
        raise TournamentWorkflowError(
            "The available player pool cannot provide every required captain. "
            f"expected={captain_round.teams_count} actual={len(captain_rows)}"
        )

    freshness = await deadlock_auto_assignment_run_freshness(
        db_session,
        tournament_id=tournament.id,
        run_row=run_row,
        current_inputs=current_inputs,
    )
    rebalanced = freshness.is_stale
    if rebalanced:
        with measure_compute_block():
            assignment = AutoAssignmentEngine().solve(
                captain_rows,
                list(current_inputs.ready_player_rows),
                list(current_inputs.dream_slot_rows),
            )
        result_snapshot = dict(assignment.result_snapshot)
        result_snapshot["input_fingerprint"] = current_inputs.input_fingerprint
        run_row.summary_text = assignment.summary_text
        run_row.result_snapshot = result_snapshot
        run_row.candidate_pool_user_ids = [
            str(player.user_id) for player in assignment.candidate_pool
        ]
        run_row.leftover_user_ids = [str(player.user_id) for player in assignment.leftovers]
        await db_session.flush()

    try:
        commitments = await create_assignment_commitments(
            db_session,
            run_row=run_row,
            activated_at=now,
        )
    except PlayerCommitmentConflict as exc:
        conflict_names = ", ".join(
            f"{item.team_name} / {item.tournament_name}" for item in exc.commitments
        )
        raise TournamentWorkflowError(
            "Player availability changed during roster locking. Retry the lock to rebalance. "
            f"Conflicts: {conflict_names}"
        ) from exc
    except IntegrityError as exc:
        raise TournamentWorkflowError(
            "Player availability changed during roster locking. Retry the lock to rebalance."
        ) from exc

    run_row.status = transition_auto_assignment_run_status(run_row.status, "locked")
    run_row.published_at = run_row.published_at or now
    run_row.published_by_user_id = run_row.published_by_user_id or actor_user_id
    run_row.locked_at = now
    run_row.locked_by_user_id = actor_user_id
    await write_audit_log(
        db_session,
        actor_user_id=actor_user_id,
        action="tournament.deadlock.auto_assignment.commitments.activate",
        subject_type="tournament_deadlock_assignment_run",
        subject_id=run_row.id,
        payload={
            "tournament_slug": tournament.slug,
            "rebalanced": rebalanced,
            "unavailable_user_ids": list(unavailable_user_ids),
            "committed_players": len(commitments),
        },
    )
    return rebalanced, unavailable_user_ids
