from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.services.brackets import create_full_bracket_graph
from apps.platform_api.app.services.tournament_workflow import (
    INACTIVE_PARTICIPANT_STATUSES,
    build_deadlock_auto_assignment_inputs,
    deadlock_auto_assignment_run_for_tournament,
    deadlock_auto_assignment_run_freshness,
    deadlock_captain_entries_for_round,
    deadlock_captain_round_for_tournament,
    deadlock_closed_ready_round_for_tournament,
    deadlock_finalized_captain_round_for_tournament,
    deadlock_published_auto_assignment_run_for_tournament,
    deadlock_ready_candidate_rows_for_round,
    deadlock_ready_round_for_tournament,
    finalize_deadlock_assignment_with_commitments,
    lock_tournament_for_workflow,
    prepare_deadlock_captain_candidate_rows,
    reconcile_finalized_captain_round_for_availability,
    supersede_published_deadlock_assignment_run_for_tournament,
    tournament_has_locked_deadlock_roster,
    transition_locked_tournament_status,
)
from apps.platform_api.app.services.tournament_read_models import (
    refresh_tournament_read_models,
)
from python_packages.platform_domain.deadlock import (
    AutoAssignmentEngine,
    CaptainRoundState,
    assign_captain_team_numbers,
    prepare_captain_round_entries,
    prepare_ready_check_start,
    resolve_effective_teams_count,
)
from python_packages.platform_domain.tournaments import (
    SOLO_TOURNAMENT_FORMAT,
    TournamentWorkflowError,
    ensure_deadlock_roster_staging_allowed,
    is_solo_tournament_format,
)
from python_packages.platform_domain.deadlock import (
    transition_auto_assignment_run_status,
    AutoAssignmentRunWorkflowError,
    AutoAssignmentError,
)
from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import session_factory
from python_packages.platform_infra.models import (
    DeadlockProfile,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainEntry,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentMatch,
    TournamentParticipant,
    User,
)
from python_packages.platform_infra.performance import measure_compute_block


AUTOMATION_ACTOR_USER_ID: str | None = None
AUTOMATION_TERMINAL_STATUSES = ("in_progress", "completed", "cancelled")


@dataclass(frozen=True, slots=True)
class DeadlockAutomationResult:
    scanned: int = 0
    deferred: int = 0
    registration_opened: int = 0
    registration_closed: int = 0
    ready_started: int = 0
    ready_closed: int = 0
    captain_started: int = 0
    captain_offers_expired: int = 0
    captain_finalized: int = 0
    assignment_generated: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "deferred": self.deferred,
            "registration_opened": self.registration_opened,
            "registration_closed": self.registration_closed,
            "ready_started": self.ready_started,
            "ready_closed": self.ready_closed,
            "captain_started": self.captain_started,
            "captain_offers_expired": self.captain_offers_expired,
            "captain_finalized": self.captain_finalized,
            "assignment_generated": self.assignment_generated,
            "errors": self.errors,
        }


def _increment(result: DeadlockAutomationResult, field: str, amount: int = 1) -> DeadlockAutomationResult:
    values = result.as_dict()
    values[field] += amount
    return DeadlockAutomationResult(**values)


def _aware_now() -> datetime:
    return datetime.now(UTC)


def reset_automation_failure(tournament: Tournament) -> None:
    tournament.automation_last_error = None
    tournament.automation_failure_count = 0
    tournament.automation_retry_after = None


def _record_automation_failure(
    tournament: Tournament,
    *,
    error: Exception,
    now: datetime,
) -> None:
    settings = get_settings()
    failure_count = int(tournament.automation_failure_count or 0) + 1
    exponent = min(failure_count - 1, 20)
    delay_minutes = min(
        settings.platform_deadlock_automation_retry_max_minutes,
        settings.platform_deadlock_automation_retry_base_minutes * (2**exponent),
    )
    tournament.automation_last_error = str(error)[:2000]
    tournament.automation_failure_count = failure_count
    tournament.automation_retry_after = now + timedelta(minutes=delay_minutes)


@dataclass(frozen=True, slots=True)
class _DeadlockAutomationCandidate:
    tournament_id: str
    due_at: datetime
    workload_estimate: int
    created_at: datetime


def _deadlock_automation_cohort_statement(*, now: datetime, limit: int) -> Any:
    registration_open_due = and_(
        Tournament.status == "registration_closed",
        Tournament.registration_starts_at.is_not(None),
        Tournament.registration_starts_at <= now,
        or_(
            Tournament.registration_closes_at.is_(None),
            now < Tournament.registration_closes_at,
        ),
    )
    registration_close_due = and_(
        Tournament.status == "registration_open",
        Tournament.registration_closes_at.is_not(None),
        Tournament.registration_closes_at <= now,
    )
    ready_start_due = and_(
        Tournament.automation_ready_check_started_at.is_(None),
        Tournament.ready_check_starts_at.is_not(None),
        Tournament.ready_check_starts_at <= now,
    )
    ready_close_due = and_(
        Tournament.automation_ready_check_started_at.is_not(None),
        Tournament.automation_ready_check_closed_at.is_(None),
        Tournament.ready_check_ends_at.is_not(None),
        Tournament.ready_check_ends_at <= now,
    )
    ready_close_for_captains_due = and_(
        Tournament.automation_ready_check_started_at.is_not(None),
        Tournament.automation_ready_check_closed_at.is_(None),
        Tournament.captain_selection_starts_at.is_not(None),
        Tournament.captain_selection_starts_at <= now,
    )
    captain_start_due = and_(
        Tournament.automation_ready_check_closed_at.is_not(None),
        Tournament.automation_captain_round_started_at.is_(None),
        Tournament.captain_selection_starts_at.is_not(None),
        Tournament.captain_selection_starts_at <= now,
    )
    captain_finalize_due = and_(
        Tournament.automation_captain_round_started_at.is_not(None),
        Tournament.automation_captain_round_started_at <= now,
        Tournament.automation_captain_round_finalized_at.is_(None),
    )
    assignment_due = and_(
        Tournament.automation_captain_round_finalized_at.is_not(None),
        Tournament.automation_captain_round_finalized_at <= now,
        Tournament.automation_assignment_generated_at.is_(None),
    )

    due_at = func.least(
        case((registration_open_due, Tournament.registration_starts_at)),
        case((registration_close_due, Tournament.registration_closes_at)),
        case((ready_start_due, Tournament.ready_check_starts_at)),
        case((ready_close_due, Tournament.ready_check_ends_at)),
        case(
            (
                ready_close_for_captains_due,
                Tournament.captain_selection_starts_at,
            )
        ),
        case((captain_start_due, Tournament.captain_selection_starts_at)),
        case(
            (
                captain_finalize_due,
                Tournament.automation_captain_round_started_at,
            )
        ),
        case(
            (
                assignment_due,
                Tournament.automation_captain_round_finalized_at,
            )
        ),
    )
    locked_roster_exists = (
        select(TournamentDeadlockAssignmentRun.id)
        .where(
            TournamentDeadlockAssignmentRun.tournament_id == Tournament.id,
            TournamentDeadlockAssignmentRun.status == "locked",
        )
        .exists()
    )
    candidates = (
        select(
            Tournament.id.label("tournament_id"),
            due_at.label("due_at"),
            Tournament.created_at.label("created_at"),
            Tournament.teams_count.label("teams_count"),
        )
        .select_from(Tournament)
        .where(
            Tournament.format_slug == SOLO_TOURNAMENT_FORMAT,
            Tournament.status.not_in(AUTOMATION_TERMINAL_STATUSES),
            Tournament.automation_assignment_generated_at.is_(None),
            or_(
                Tournament.automation_retry_after.is_(None),
                Tournament.automation_retry_after <= now,
            ),
            ~locked_roster_exists,
        )
        .cte("automation_candidates")
    )
    total_candidate_count = (
        select(func.count())
        .select_from(candidates)
        .where(candidates.c.due_at.is_not(None))
        .scalar_subquery()
    )
    selected_cohort = (
        select(
            candidates.c.tournament_id,
            candidates.c.due_at,
            candidates.c.created_at,
            candidates.c.teams_count,
            total_candidate_count.label("candidate_count"),
        )
        .where(
            candidates.c.due_at.is_not(None),
        )
        .order_by(
            candidates.c.due_at.asc(),
            candidates.c.created_at.asc(),
            candidates.c.tournament_id.asc(),
        )
        .limit(limit)
        .cte("selected_automation_cohort")
    )
    active_participant_counts = (
        select(
            TournamentParticipant.tournament_id.label("tournament_id"),
            func.count().label("active_participant_count"),
        )
        .select_from(TournamentParticipant)
        .join(
            selected_cohort,
            selected_cohort.c.tournament_id
            == TournamentParticipant.tournament_id,
        )
        .where(
            TournamentParticipant.status.not_in(
                INACTIVE_PARTICIPANT_STATUSES
            )
        )
        .group_by(TournamentParticipant.tournament_id)
        .cte("active_participant_counts")
    )
    workload_estimate = func.coalesce(
        func.nullif(active_participant_counts.c.active_participant_count, 0),
        selected_cohort.c.teams_count * 6,
        0,
    )
    return (
        select(
            selected_cohort.c.tournament_id,
            selected_cohort.c.due_at,
            workload_estimate.label("workload_estimate"),
            selected_cohort.c.created_at,
            selected_cohort.c.candidate_count,
        )
        .select_from(selected_cohort)
        .outerjoin(
            active_participant_counts,
            active_participant_counts.c.tournament_id
            == selected_cohort.c.tournament_id,
        )
        .order_by(
            selected_cohort.c.due_at.asc(),
            selected_cohort.c.created_at.asc(),
            selected_cohort.c.tournament_id.asc(),
        )
    )


async def _select_deadlock_automation_cohort(
    db_session: AsyncSession,
    *,
    now: datetime,
    limit: int,
) -> tuple[list[_DeadlockAutomationCandidate], int]:
    rows = (
        await db_session.execute(
            _deadlock_automation_cohort_statement(now=now, limit=limit)
        )
    ).mappings().all()
    if not rows:
        return [], 0

    total_candidates = int(rows[0]["candidate_count"])
    selected = [
        _DeadlockAutomationCandidate(
            tournament_id=str(row["tournament_id"]),
            due_at=row["due_at"],
            workload_estimate=int(row["workload_estimate"] or 0),
            created_at=row["created_at"],
        )
        for row in rows
    ]
    selected.sort(
        key=lambda candidate: (
            candidate.workload_estimate,
            candidate.due_at,
            candidate.created_at,
            candidate.tournament_id,
        )
    )
    return selected, max(total_candidates - len(selected), 0)


async def _lock_tournament_for_failure_state(
    db_session: AsyncSession,
    tournament_id: str,
) -> Tournament | None:
    try:
        return await lock_tournament_for_workflow(db_session, tournament_id)
    except TournamentWorkflowError:
        return None


async def run_deadlock_automation_once(now: datetime | None = None) -> dict[str, int]:
    async with session_factory()() as db_session:
        result = await run_deadlock_automation_tick(db_session, now=now)
        return result.as_dict()


async def run_deadlock_automation_tick(
    db_session: AsyncSession,
    *,
    now: datetime | None = None,
    max_tournaments: int | None = None,
) -> DeadlockAutomationResult:
    current_time = (now or _aware_now()).astimezone(UTC)
    cohort_limit = (
        max_tournaments
        if max_tournaments is not None
        else get_settings().platform_deadlock_automation_max_tournaments_per_tick
    )
    if cohort_limit < 1:
        raise ValueError("Deadlock automation cohort limit must be positive.")
    candidates, deferred = await _select_deadlock_automation_cohort(
        db_session,
        now=current_time,
        limit=cohort_limit,
    )

    result = DeadlockAutomationResult(
        scanned=len(candidates),
        deferred=deferred,
    )
    for candidate in candidates:
        tournament_id = candidate.tournament_id
        try:
            tournament = await lock_tournament_for_workflow(
                db_session,
                tournament_id,
            )
            step_result = await _advance_tournament(
                db_session,
                tournament=tournament,
                now=current_time,
            )
            await db_session.commit()
            await refresh_tournament_read_models(
                tournament_id,
                (
                    "teams",
                    "workspace_detail",
                    "bracket_summary",
                    "bracket_full",
                )
                if step_result.assignment_generated
                else (
                    "workspace_detail",
                    "bracket_summary",
                    "bracket_full",
                ),
            )
            result = DeadlockAutomationResult(
                scanned=result.scanned,
                deferred=result.deferred,
                registration_opened=result.registration_opened + step_result.registration_opened,
                registration_closed=result.registration_closed + step_result.registration_closed,
                ready_started=result.ready_started + step_result.ready_started,
                ready_closed=result.ready_closed + step_result.ready_closed,
                captain_started=result.captain_started + step_result.captain_started,
                captain_offers_expired=result.captain_offers_expired + step_result.captain_offers_expired,
                captain_finalized=result.captain_finalized + step_result.captain_finalized,
                assignment_generated=result.assignment_generated + step_result.assignment_generated,
                errors=result.errors,
            )
        except Exception as exc:  # pragma: no cover - defensive boundary for worker loop
            await db_session.rollback()
            fresh_tournament = await _lock_tournament_for_failure_state(db_session, tournament_id)
            if fresh_tournament is not None:
                _record_automation_failure(
                    fresh_tournament,
                    error=exc,
                    now=current_time,
                )
                await db_session.commit()
            result = _increment(result, "errors")
    return result


async def advance_deadlock_tournament_automation(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    now: datetime | None = None,
    allow_assignment_generation: bool = True,
) -> DeadlockAutomationResult:
    tournament_id = tournament.id
    tournament = await lock_tournament_for_workflow(
        db_session,
        tournament_id,
    )
    try:
        result = await _advance_tournament(
            db_session,
            tournament=tournament,
            now=(now or _aware_now()).astimezone(UTC),
            allow_assignment_generation=allow_assignment_generation,
        )
    except (AutoAssignmentError, TournamentWorkflowError) as exc:
        await db_session.rollback()
        fresh_tournament = await _lock_tournament_for_failure_state(db_session, tournament_id)
        if fresh_tournament is not None:
            _record_automation_failure(
                fresh_tournament,
                error=exc,
                now=(now or _aware_now()).astimezone(UTC),
            )
            await db_session.commit()
            await db_session.refresh(fresh_tournament)
        return _increment(DeadlockAutomationResult(), "errors")
    if result != DeadlockAutomationResult():
        await db_session.commit()
        await db_session.refresh(tournament)
        await refresh_tournament_read_models(
            tournament_id,
            (
                "teams",
                "workspace_detail",
                "bracket_summary",
                "bracket_full",
            )
            if result.assignment_generated
            else (
                "workspace_detail",
                "bracket_summary",
                "bracket_full",
            ),
        )
    return result


async def _advance_tournament(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    now: datetime,
    allow_assignment_generation: bool = True,
) -> DeadlockAutomationResult:
    if not is_solo_tournament_format(tournament.format_slug):
        return DeadlockAutomationResult()
    if await tournament_has_locked_deadlock_roster(db_session, tournament=tournament):
        return DeadlockAutomationResult()

    result = DeadlockAutomationResult()
    if (
        tournament.registration_starts_at is not None
        and now >= tournament.registration_starts_at
        and (
            tournament.registration_closes_at is None
            or now < tournament.registration_closes_at
        )
        and tournament.status == "registration_closed"
    ):
        await transition_locked_tournament_status(
            db_session,
            tournament=tournament,
            next_status="registration_open",
            now=now,
            actor_user_id=AUTOMATION_ACTOR_USER_ID,
            audit_action="tournament.automation.registration.open",
            expected_status="registration_closed",
        )
        result = _increment(result, "registration_opened")

    if (
        tournament.registration_closes_at is not None
        and now >= tournament.registration_closes_at
        and tournament.status == "registration_open"
    ):
        if await _ensure_registration_closed(db_session, tournament=tournament, now=now):
            result = _increment(result, "registration_closed")

    if tournament.ready_check_starts_at is not None and now >= tournament.ready_check_starts_at:
        if tournament.automation_ready_check_started_at is None:
            if await _ensure_ready_check_started(db_session, tournament=tournament, now=now):
                result = _increment(result, "ready_started")

    if tournament.ready_check_ends_at is not None and now >= tournament.ready_check_ends_at:
        if tournament.automation_ready_check_closed_at is None:
            if await _ensure_ready_check_closed(db_session, tournament=tournament, now=now):
                result = _increment(result, "ready_closed")

    if (
        tournament.captain_selection_starts_at is not None
        and now >= tournament.captain_selection_starts_at
        and tournament.automation_ready_check_started_at is not None
        and tournament.automation_ready_check_closed_at is None
    ):
        if await _ensure_ready_check_closed(db_session, tournament=tournament, now=now):
            result = _increment(result, "ready_closed")

    if (
        tournament.captain_selection_starts_at is not None
        and now >= tournament.captain_selection_starts_at
        and tournament.automation_ready_check_closed_at is not None
    ):
        if tournament.automation_captain_round_started_at is None:
            if await _ensure_captain_round_started(db_session, tournament=tournament, now=now):
                result = _increment(result, "captain_started")

    expired_count = await _expire_stale_captain_offers(db_session, tournament=tournament, now=now)
    if expired_count:
        result = _increment(result, "captain_offers_expired", expired_count)

    if tournament.automation_captain_round_started_at is not None:
        if tournament.automation_captain_round_finalized_at is None:
            if await _ensure_captain_round_finalized(db_session, tournament=tournament, now=now):
                result = _increment(result, "captain_finalized")

    if tournament.automation_captain_round_finalized_at is not None:
        if (
            allow_assignment_generation
            and tournament.automation_assignment_generated_at is None
        ):
            if await _ensure_assignment_generated(db_session, tournament=tournament, now=now):
                result = _increment(result, "assignment_generated")

    if result != DeadlockAutomationResult():
        reset_automation_failure(tournament)
    return result


async def _ensure_registration_closed(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    now: datetime,
) -> bool:
    if tournament.status != "registration_open":
        return False
    await transition_locked_tournament_status(
        db_session,
        tournament=tournament,
        next_status="registration_closed",
        now=now,
        actor_user_id=AUTOMATION_ACTOR_USER_ID,
        audit_action="tournament.automation.registration.close",
        expected_status="registration_open",
    )
    return True


async def _ensure_ready_check_started(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    now: datetime,
) -> bool:
    if tournament.status == "registration_open":
        await _ensure_registration_closed(db_session, tournament=tournament, now=now)

    ensure_deadlock_roster_staging_allowed(
        format_slug=tournament.format_slug,
        tournament_status=tournament.status,
        has_locked_deadlock_roster=False,
        action_name="Deadlock ready-check",
    )

    active_round = await deadlock_ready_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_round is not None:
        tournament.automation_ready_check_started_at = now
        return False

    participant_user_ids = [
        str(user_id)
        for user_id in (
            await db_session.scalars(
                select(TournamentParticipant.user_id).where(
                    TournamentParticipant.tournament_id == tournament.id,
                    TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
                )
            )
        ).all()
    ]
    decision = prepare_ready_check_start(participant_user_ids, has_active_round=False)
    if not decision.should_create_round:
        raise TournamentWorkflowError("No participants are available for automated ready-check.")

    round_row = TournamentDeadlockReadyRound(
        tournament_id=tournament.id,
        status="active",
        eligible_user_ids=list(decision.user_ids),
        initiated_by_user_id=None,
    )
    db_session.add(round_row)
    await db_session.flush()
    tournament.automation_ready_check_started_at = now
    await write_audit_log(
        db_session,
        actor_user_id=AUTOMATION_ACTOR_USER_ID,
        action="tournament.deadlock.ready_check.auto_start",
        subject_type="tournament_deadlock_ready_round",
        subject_id=str(round_row.id),
        payload={
            "tournament_slug": tournament.slug,
            "eligible_participant_count": len(decision.user_ids),
        },
    )
    return True


async def _ensure_ready_check_closed(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    now: datetime,
) -> bool:
    active_round = await deadlock_ready_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_round is None:
        latest_round = await deadlock_ready_round_for_tournament(
            db_session,
            tournament_id=tournament.id,
            active_only=False,
        )
        if latest_round is not None and latest_round.status == "closed":
            tournament.automation_ready_check_closed_at = latest_round.closed_at or now
        return False

    active_round.status = "closed"
    active_round.closed_at = now
    tournament.automation_ready_check_closed_at = now
    await write_audit_log(
        db_session,
        actor_user_id=AUTOMATION_ACTOR_USER_ID,
        action="tournament.deadlock.ready_check.auto_close",
        subject_type="tournament_deadlock_ready_round",
        subject_id=str(active_round.id),
        payload={"tournament_slug": tournament.slug},
    )
    return True


async def _ensure_captain_round_started(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    now: datetime,
) -> bool:
    active_round = await deadlock_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_round is not None:
        tournament.automation_captain_round_started_at = now
        return False

    source_ready_round = await deadlock_closed_ready_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    if source_ready_round is None:
        raise TournamentWorkflowError("Close a ready-check round before automated captain selection.")

    candidate_rows = await deadlock_ready_candidate_rows_for_round(
        db_session,
        tournament_id=tournament.id,
        round_id=source_ready_round.id,
    )
    teams_count = resolve_effective_teams_count(
        requested_teams_count=tournament.teams_count,
        ready_player_count=len(candidate_rows),
    )

    round_row = TournamentDeadlockCaptainRound(
        tournament_id=tournament.id,
        source_ready_round_id=source_ready_round.id,
        teams_count=teams_count,
        status="finalized",
        initiated_by_user_id=None,
        closed_at=now,
        finalized_at=now,
    )
    db_session.add(round_row)
    await db_session.flush()

    prepared_entries = prepare_captain_round_entries(
        prepare_deadlock_captain_candidate_rows(candidate_rows),
        teams_count,
        auto_assign=True,
    )
    for entry in prepared_entries:
        db_session.add(
            TournamentDeadlockCaptainEntry(
                round_id=round_row.id,
                user_id=entry.user_id,
                offer_order=entry.offer_order,
                state=entry.state,
                assigned_team_id=entry.assigned_team_id,
                responded_at=now if entry.state == "assigned" else None,
            )
        )

    tournament.automation_captain_round_started_at = now
    await write_audit_log(
        db_session,
        actor_user_id=AUTOMATION_ACTOR_USER_ID,
        action="tournament.deadlock.captain_round.auto_start",
        subject_type="tournament_deadlock_captain_round",
        subject_id=str(round_row.id),
        payload={
            "tournament_slug": tournament.slug,
            "source_ready_round_id": source_ready_round.id,
            "requested_teams_count": tournament.teams_count,
            "teams_count": teams_count,
            "candidate_count": len(candidate_rows),
        },
    )
    return True


async def _expire_stale_captain_offers(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    now: datetime,
) -> int:
    deadline_minutes = int(tournament.captain_response_deadline_minutes or 0)
    if deadline_minutes < 1:
        return 0

    active_round = await deadlock_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_round is None:
        return 0

    entry_rows = await deadlock_captain_entries_for_round(db_session, round_id=active_round.id)
    expires_before = now - timedelta(minutes=deadline_minutes)
    expired_user_ids = [
        row.user_id
        for row in entry_rows
        if row.state == "offered" and (row.updated_at or row.created_at) <= expires_before
    ]
    if not expired_user_ids:
        return 0

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
    expired_count = 0
    for user_id in expired_user_ids:
        current_entry = next((entry for entry in round_state.entries if entry.user_id == user_id), None)
        if current_entry is None or current_entry.state != "offered":
            continue
        round_state, decision = round_state.respond(user_id, "decline")
        if decision.status == "updated":
            expired_count += 1

    if not expired_count:
        return 0

    rows_by_user_id = {row.user_id: row for row in entry_rows}
    next_entries_by_user_id = {entry.user_id: entry for entry in round_state.entries}
    for user_id, row in rows_by_user_id.items():
        next_entry = next_entries_by_user_id[user_id]
        if row.state == next_entry.state and row.assigned_team_id == next_entry.assigned_team_id:
            continue
        row.state = next_entry.state
        row.assigned_team_id = next_entry.assigned_team_id
        if next_entry.state in {"declined", "cancelled"}:
            row.responded_at = now

    await write_audit_log(
        db_session,
        actor_user_id=AUTOMATION_ACTOR_USER_ID,
        action="tournament.deadlock.captain_round.auto_expire_offers",
        subject_type="tournament_deadlock_captain_round",
        subject_id=str(active_round.id),
        payload={
            "tournament_slug": tournament.slug,
            "expired_user_ids": expired_user_ids,
            "expired_count": expired_count,
            "deadline_minutes": deadline_minutes,
        },
    )
    return expired_count


async def _ensure_captain_round_finalized(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    now: datetime,
) -> bool:
    active_round = await deadlock_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_round is None:
        finalized_round = await deadlock_finalized_captain_round_for_tournament(
            db_session,
            tournament_id=tournament.id,
        )
        if finalized_round is not None:
            tournament.automation_captain_round_finalized_at = finalized_round.finalized_at or now
        return False

    entry_rows = await deadlock_captain_entries_for_round(db_session, round_id=active_round.id)
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
    if not round_state.can_finalize:
        return False

    accepted_user_ids = [entry.user_id for entry in round_state.entries if entry.state == "accepted"]
    accepted_candidates = await db_session.execute(
        select(
            User.id.label("user_id"),
            DeadlockProfile.rank,
            DeadlockProfile.subrank,
            DeadlockProfile.playtime,
        )
        .select_from(User)
        .join(DeadlockProfile, DeadlockProfile.user_id == User.id)
        .where(User.id.in_(accepted_user_ids))
    )
    assignments = assign_captain_team_numbers(
        [dict(row._mapping) for row in accepted_candidates]
    )
    assigned_team_by_user_id = {assignment.user_id: assignment.team_id for assignment in assignments}

    for row in entry_rows:
        if row.user_id in assigned_team_by_user_id:
            row.state = "assigned"
            row.assigned_team_id = assigned_team_by_user_id[row.user_id]
            row.responded_at = row.responded_at or now
        elif row.state in {"queued", "offered"}:
            row.state = "cancelled"
            row.responded_at = now

    active_round.status = "finalized"
    active_round.finalized_at = now
    active_round.closed_at = active_round.closed_at or now
    tournament.automation_captain_round_finalized_at = now
    await write_audit_log(
        db_session,
        actor_user_id=AUTOMATION_ACTOR_USER_ID,
        action="tournament.deadlock.captain_round.auto_finalize",
        subject_type="tournament_deadlock_captain_round",
        subject_id=str(active_round.id),
        payload={
            "tournament_slug": tournament.slug,
            "assigned_captains": [
                {"user_id": user_id, "team_id": team_id}
                for user_id, team_id in sorted(assigned_team_by_user_id.items(), key=lambda item: int(item[1]))
            ],
        },
    )
    return True


async def _ensure_assignment_generated(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    now: datetime,
) -> bool:
    published_run = await deadlock_published_auto_assignment_run_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    if published_run is not None and published_run.status == "locked":
        return False

    active_captain_round = await deadlock_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_captain_round is not None:
        return False

    captain_round = await deadlock_finalized_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    if captain_round is None:
        return False

    await reconcile_finalized_captain_round_for_availability(
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
            await _ensure_assignment_handoff_completed(
                db_session,
                tournament=tournament,
                run_row=latest_run,
                now=now,
            )
            tournament.automation_assignment_generated_at = latest_run.created_at or now
            return False

    captain_rows = list(current_inputs.captain_rows)
    if len(captain_rows) != captain_round.teams_count:
        raise TournamentWorkflowError("The finalized captain round does not have the required assigned captains.")

    engine = AutoAssignmentEngine()
    with measure_compute_block():
        run = engine.solve(
            captain_rows,
            list(current_inputs.ready_player_rows),
            list(current_inputs.dream_slot_rows),
        )

    result_snapshot = dict(run.result_snapshot)
    result_snapshot["input_fingerprint"] = current_inputs.input_fingerprint
    run_row = TournamentDeadlockAssignmentRun(
        tournament_id=tournament.id,
        source_captain_round_id=captain_round.id,
        source_ready_round_id=captain_round.source_ready_round_id,
        created_by_user_id=None,
        status="generated",
        summary_text=run.summary_text,
        result_snapshot=result_snapshot,
        candidate_pool_user_ids=[str(player.user_id) for player in run.candidate_pool],
        leftover_user_ids=[str(player.user_id) for player in run.leftovers],
    )
    db_session.add(run_row)
    await db_session.flush()
    await _ensure_assignment_handoff_completed(
        db_session,
        tournament=tournament,
        run_row=run_row,
        now=now,
    )
    tournament.automation_assignment_generated_at = now
    await write_audit_log(
        db_session,
        actor_user_id=AUTOMATION_ACTOR_USER_ID,
        action="tournament.deadlock.auto_assignment.auto_run",
        subject_type="tournament_deadlock_assignment_run",
        subject_id=run_row.id,
        payload={
            "tournament_slug": tournament.slug,
            "source_captain_round_id": captain_round.id,
            "source_ready_round_id": captain_round.source_ready_round_id,
            "candidate_pool_size": len(run.candidate_pool),
            "leftover_count": len(run.leftovers),
        },
    )
    return True


async def _ensure_assignment_handoff_completed(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    run_row: TournamentDeadlockAssignmentRun,
    now: datetime,
) -> bool:
    changed = False
    if run_row.status == "generated":
        await supersede_published_deadlock_assignment_run_for_tournament(
            db_session,
            tournament_id=tournament.id,
            replacement_run_id=run_row.id,
        )
        try:
            run_row.status = transition_auto_assignment_run_status(
                run_row.status,
                "published",
            )
        except AutoAssignmentRunWorkflowError as exc:
            raise TournamentWorkflowError(str(exc)) from exc
        run_row.published_at = run_row.published_at or now
        run_row.published_by_user_id = run_row.published_by_user_id or AUTOMATION_ACTOR_USER_ID
        await write_audit_log(
            db_session,
            actor_user_id=AUTOMATION_ACTOR_USER_ID,
            action="tournament.deadlock.auto_assignment.auto_publish",
            subject_type="tournament_deadlock_assignment_run",
            subject_id=run_row.id,
            payload={"tournament_slug": tournament.slug},
        )
        changed = True

    if run_row.status == "published":
        try:
            rebalanced, unavailable_user_ids = (
                await finalize_deadlock_assignment_with_commitments(
                    db_session,
                    tournament=tournament,
                    run_row=run_row,
                    actor_user_id=AUTOMATION_ACTOR_USER_ID,
                    now=now,
                )
            )
        except (
            AutoAssignmentError,
            AutoAssignmentRunWorkflowError,
            TournamentWorkflowError,
        ) as exc:
            raise TournamentWorkflowError(str(exc)) from exc
        await write_audit_log(
            db_session,
            actor_user_id=AUTOMATION_ACTOR_USER_ID,
            action="tournament.deadlock.auto_assignment.auto_lock",
            subject_type="tournament_deadlock_assignment_run",
            subject_id=run_row.id,
            payload={
                "tournament_slug": tournament.slug,
                "rebalanced": rebalanced,
                "unavailable_user_ids": list(unavailable_user_ids),
            },
        )
        changed = True

    if run_row.status != "locked":
        return changed

    existing_match_count = int(
        await db_session.scalar(
            select(func.count()).select_from(TournamentMatch).where(
                TournamentMatch.tournament_id == tournament.id
            )
        )
        or 0
    )
    if existing_match_count:
        return changed

    created_matches, opening_matches = await create_full_bracket_graph(
        db_session,
        tournament=tournament,
        locked_run=run_row,
    )
    await write_audit_log(
        db_session,
        actor_user_id=AUTOMATION_ACTOR_USER_ID,
        action="match.seed_opening_round.auto",
        subject_type="tournament",
        subject_id=tournament.id,
        payload={
            "tournament_slug": tournament.slug,
            "source_run_id": run_row.id,
            "match_count": len(created_matches),
            "opening_match_count": len(opening_matches),
            "revision": tournament.bracket_revision,
            "matches": [
                {
                    "round_number": match.round_number,
                    "sequence_number": match.sequence_number,
                    "home_label": match.home_label,
                    "away_label": match.away_label,
                    "title": match.title,
                }
                for match in created_matches
            ],
        },
    )
    return True
