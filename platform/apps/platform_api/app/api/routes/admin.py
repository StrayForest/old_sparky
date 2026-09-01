from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool
from sqlalchemy import and_, case, delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.pagination import (
    TOURNAMENT_LIST_DEFAULT_LIMIT,
    TOURNAMENT_LIST_MAX_LIMIT,
    set_pagination_headers,
)
from apps.platform_api.app.api.schemas import (
    AdminAuditLogResponse,
    AdminOverviewResponse,
    AdminPreprodCleanupRequest,
    AdminPreprodCleanupResponse,
    AdminPreprodTestRunResponse,
    AdminRosterAddPlayerRequest,
    AdminRosterChangeCaptainRequest,
    AdminRosterMovePlayerRequest,
    AdminRosterRemovePlayerRequest,
    AdminRosterReplacePlayerRequest,
    AdminRosterResponse,
    AdminTournamentResponse,
    AdminTournamentDeleteRequest,
    AdminTournamentOverrideRequest,
    AdminUserResponse,
    AdminUserTournamentCreditsUpdateRequest,
    AdminUserRoleUpdateRequest,
)
from python_packages.platform_domain.tournaments import (
    available_tournament_statuses,
)
from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.media.hard_delete import (
    MediaCleanupRequired,
    purge_deleted_media_metadata,
)
from python_packages.platform_infra.models import (
    AuditLog,
    PreprodTestRun,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentMatch,
    TournamentParticipant,
    Role,
    User,
    UserRole,
)
from python_packages.platform_infra.object_storage import get_object_storage, object_key_from_upload_url
from python_packages.platform_infra.security import (
    get_authenticated_session,
    invalidate_user_session_cache,
    invalidate_user_sessions,
)
from python_packages.platform_infra.tournament_names import (
    lock_tournament_name,
    public_tournament_name_exists,
)
from apps.platform_api.app.services.tournament_workflow import (
    mark_ready_check_closed,
    supersede_published_deadlock_assignment_run_for_tournament,
)
from apps.platform_api.app.services.player_commitments import (
    PlayerCommitmentConflict,
    reactivate_viable_tournament_commitments,
    release_active_commitments,
)
from apps.platform_api.app.services.tournament_read_models import (
    delete_tournament_read_models,
    refresh_tournament_read_models,
)
from apps.platform_api.app.services.tournament_catalog_read_models import (
    refresh_tournament_list_read_model_after_commit,
)
from apps.platform_api.app.services.tournament_profile_access import (
    delete_tournament_profile_access_state,
)
from apps.platform_api.app.services.admin_roster import (
    AdminRosterError,
    load_admin_roster_snapshot,
    mutate_admin_roster,
    public_roster_snapshot,
)
from apps.platform_api.app.services.mutation_idempotency import (
    bind_mutation_idempotency_resource,
    mutation_payload_fingerprint,
    request_idempotency_key,
    reserve_mutation_idempotency,
)
from apps.platform_api.app.services.tournament_runtime_cache import (
    invalidate_tournament_runtime_caches,
)

router = APIRouter()
PREPROD_CLEANUP_CHUNK_SIZE = 10_000
INACTIVE_PARTICIPANT_STATUSES = ("withdrawn", "disqualified")


def ensure_admin_role(auth_session) -> None:
    if "admin" not in auth_session.role_slugs and "superadmin" not in auth_session.role_slugs:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role is required.")


def ensure_superadmin_role(auth_session) -> None:
    if "superadmin" not in auth_session.role_slugs:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Superadmin role is required.")


def admin_tournament_attention_filter():
    has_unfinished_match = exists(
        select(1).where(
            TournamentMatch.tournament_id == Tournament.id,
            TournamentMatch.status.not_in(("completed", "cancelled")),
        )
    )
    has_locked_roster = exists(
        select(1).where(
            TournamentDeadlockAssignmentRun.tournament_id == Tournament.id,
            TournamentDeadlockAssignmentRun.status == "locked",
        )
    )
    return or_(
        Tournament.visibility == "invite_only",
        has_unfinished_match,
        and_(Tournament.status == "registration_open", has_locked_roster),
    )


async def role_slugs_for_users(
    db_session: AsyncSession,
    user_ids: list[str],
) -> dict[str, list[str]]:
    if not user_ids:
        return {}
    rows = (
        await db_session.execute(
            select(UserRole.user_id, Role.slug)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id.in_(user_ids))
        )
    ).all()
    roles_by_user = {user_id: [] for user_id in user_ids}
    for user_id, role_slug in rows:
        roles_by_user.setdefault(str(user_id), []).append(str(role_slug))
    return {user_id: sorted(role_slugs) for user_id, role_slugs in roles_by_user.items()}


def preprod_run_user_ids(run: PreprodTestRun) -> set[str]:
    report = dict(run.report or {})
    raw_ids = report.get("user_ids") or []
    return {str(user_id) for user_id in raw_ids if str(user_id).strip()}


def preprod_run_tournament_ids(run: PreprodTestRun) -> set[str]:
    report = dict(run.report or {})
    raw_ids = set(report.get("tournament_ids") or [])
    for key in ("tournament_id", "targeted_tournament_id", "scale_tournament_id"):
        value = report.get(key)
        if value:
            raw_ids.add(value)
    return {str(tournament_id) for tournament_id in raw_ids if str(tournament_id).strip()}


def preprod_cleanup_chunks(values: set[str]) -> list[list[str]]:
    ordered = sorted(values)
    return [
        ordered[index:index + PREPROD_CLEANUP_CHUNK_SIZE]
        for index in range(0, len(ordered), PREPROD_CLEANUP_CHUNK_SIZE)
    ]


async def count_existing_ids(
    db_session: AsyncSession,
    model,
    ids: set[str],
) -> int:
    total = 0
    for chunk in preprod_cleanup_chunks(ids):
        total += int(
            await db_session.scalar(
                select(func.count()).select_from(model).where(model.id.in_(chunk))
            )
            or 0
        )
    return total


async def delete_by_ids(
    db_session: AsyncSession,
    model,
    ids: set[str],
) -> int:
    total = 0
    for chunk in preprod_cleanup_chunks(ids):
        result = await db_session.execute(delete(model).where(model.id.in_(chunk)))
        total += int(result.rowcount or 0)
    return total


def serialize_preprod_run(run: PreprodTestRun) -> AdminPreprodTestRunResponse:
    return AdminPreprodTestRunResponse(
        marker=run.marker,
        status=run.status,
        origin=run.origin,
        requested_users=int(run.requested_users or 0),
        created_users=int(run.created_users or 0),
        tournaments_created=int(run.tournaments_created or 0),
        active_participants=int(run.active_participants or 0),
        teams_count=int(run.teams_count or 0),
        matches_count=int(run.matches_count or 0),
        report_path=run.report_path,
        report=dict(run.report or {}),
        cleanup_state=dict(run.cleanup_state or {}),
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def serialize_user(user: User, role_slugs: list[str]) -> AdminUserResponse:
    is_admin = "admin" in role_slugs or "superadmin" in role_slugs
    return AdminUserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        created_at=user.created_at,
        roles=sorted(role_slugs),
        can_create_public_tournaments=is_admin or int(user.public_tournament_credits or 0) > 0,
        public_tournament_credits=int(user.public_tournament_credits or 0),
        private_tournament_credits=int(user.private_tournament_credits or 0),
    )


def admin_tournament_recovery_hint(
    *,
    tournament: Tournament,
    match_count: int,
    unfinished_match_count: int,
    has_locked_deadlock_roster: bool,
) -> str | None:
    if tournament.status in {"completed", "cancelled"} and match_count:
        return "Reopen the tournament to in_progress if the organizer needs to repair results or unwind the bracket."
    if tournament.status == "in_progress" and match_count:
        return "Organizer-side match reporting, bracket progression, and latest-round recovery are available from tournament detail."
    if tournament.status == "registration_closed" and has_locked_deadlock_roster:
        return "The locked Deadlock roster can be handed off into match staging from tournament detail."
    if tournament.status == "registration_open" and has_locked_deadlock_roster:
        return "The Deadlock roster is already locked, so organizer-side registration and roster edits remain frozen despite the earlier tournament status."
    if tournament.status == "completed" and not unfinished_match_count and not match_count:
        return "This tournament completed without staged matches. Reopen only if the organizer needs to add bracket data retroactively."
    return None


def admin_tournament_override_warning(
    *,
    tournament: Tournament,
    unfinished_match_count: int,
    has_locked_deadlock_roster: bool,
) -> str | None:
    if tournament.status in {"completed", "cancelled"} and unfinished_match_count:
        return f"Terminal state is currently freezing organizer workflow while {unfinished_match_count} match(es) are still unresolved."
    if has_locked_deadlock_roster and tournament.status == "registration_open":
        return "A locked Deadlock roster still blocks organizer-side registration and roster edits even though the tournament status was moved earlier."
    if tournament.visibility == "invite_only":
        return "Invite-only visibility removes the tournament from the public hub and limits roster/bracket reads to participants, the organizer, or platform admins."
    return None


def admin_tournament_with_counts_stmt(tournament_page=None):
    participant_counts_stmt = select(
        TournamentParticipant.tournament_id.label("tournament_id"),
        func.count(TournamentParticipant.id).label("participant_count"),
    ).where(
        TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES)
    )
    locked_roster_counts_stmt = select(
        TournamentDeadlockAssignmentRun.tournament_id.label("tournament_id"),
        func.count(TournamentDeadlockAssignmentRun.id).label("locked_roster_count"),
    ).where(TournamentDeadlockAssignmentRun.status == "locked")
    match_stats_stmt = select(
        TournamentMatch.tournament_id.label("tournament_id"),
        func.count(TournamentMatch.id).label("match_count"),
        func.max(TournamentMatch.round_number).label("latest_round_number"),
        func.coalesce(
            func.sum(case((TournamentMatch.status == "completed", 1), else_=0)),
            0,
        ).label("completed_match_count"),
        func.coalesce(
            func.sum(case((TournamentMatch.status == "cancelled", 1), else_=0)),
            0,
        ).label("cancelled_match_count"),
        func.coalesce(
            func.sum(
                case(
                    (TournamentMatch.status.not_in(("completed", "cancelled")), 1),
                    else_=0,
                )
            ),
            0,
        ).label("unfinished_match_count"),
    )
    if tournament_page is not None:
        page_ids = select(tournament_page.c.id)
        participant_counts_stmt = participant_counts_stmt.where(
            TournamentParticipant.tournament_id.in_(page_ids)
        )
        locked_roster_counts_stmt = locked_roster_counts_stmt.where(
            TournamentDeadlockAssignmentRun.tournament_id.in_(page_ids)
        )
        match_stats_stmt = match_stats_stmt.where(
            TournamentMatch.tournament_id.in_(page_ids)
        )

    participant_counts = (
        participant_counts_stmt
        .group_by(TournamentParticipant.tournament_id)
        .subquery()
    )
    locked_roster_counts = (
        locked_roster_counts_stmt
        .group_by(TournamentDeadlockAssignmentRun.tournament_id)
        .subquery()
    )
    match_stats = (
        match_stats_stmt
        .group_by(TournamentMatch.tournament_id)
        .subquery()
    )
    stmt = (
        select(
            Tournament,
            User.display_name.label("organizer_display_name"),
            func.coalesce(participant_counts.c.participant_count, 0).label("participant_count"),
            func.coalesce(locked_roster_counts.c.locked_roster_count, 0).label("locked_roster_count"),
            func.coalesce(match_stats.c.match_count, 0).label("match_count"),
            match_stats.c.latest_round_number.label("latest_round_number"),
            func.coalesce(match_stats.c.completed_match_count, 0).label("completed_match_count"),
            func.coalesce(match_stats.c.cancelled_match_count, 0).label("cancelled_match_count"),
            func.coalesce(match_stats.c.unfinished_match_count, 0).label("unfinished_match_count"),
        )
        .join(User, User.id == Tournament.organizer_user_id)
        .outerjoin(participant_counts, participant_counts.c.tournament_id == Tournament.id)
        .outerjoin(locked_roster_counts, locked_roster_counts.c.tournament_id == Tournament.id)
        .outerjoin(match_stats, match_stats.c.tournament_id == Tournament.id)
    )
    if tournament_page is not None:
        stmt = stmt.join(tournament_page, tournament_page.c.id == Tournament.id)
    return stmt


def serialize_tournament(
    tournament: Tournament,
    organizer_display_name: str,
    participant_count: int,
    *,
    has_locked_deadlock_roster: bool = False,
    match_count: int = 0,
    latest_round_number: int | None = None,
    completed_match_count: int = 0,
    cancelled_match_count: int = 0,
    unfinished_match_count: int = 0,
) -> AdminTournamentResponse:
    return AdminTournamentResponse(
        id=tournament.id,
        slug=tournament.slug,
        name=tournament.name,
        description=tournament.description,
        visibility=tournament.visibility,
        status=tournament.status,
        format_slug=tournament.format_slug,
        organizer_user_id=tournament.organizer_user_id,
        organizer_display_name=organizer_display_name,
        participant_count=participant_count,
        allowed_ranks=list(tournament.allowed_ranks or []),
        max_participants=tournament.max_participants,
        has_locked_deadlock_roster=has_locked_deadlock_roster,
        registration_starts_at=tournament.registration_starts_at,
        registration_closes_at=tournament.registration_closes_at,
        ready_check_starts_at=tournament.ready_check_starts_at,
        ready_check_ends_at=tournament.ready_check_ends_at,
        captain_selection_starts_at=tournament.captain_selection_starts_at,
        starts_at=tournament.starts_at,
        match_format=tournament.match_format,
        final_format=tournament.final_format,
        captain_response_deadline_minutes=tournament.captain_response_deadline_minutes,
        teams_count=tournament.teams_count,
        automation_ready_check_started_at=tournament.automation_ready_check_started_at,
        automation_ready_check_closed_at=tournament.automation_ready_check_closed_at,
        automation_captain_round_started_at=tournament.automation_captain_round_started_at,
        automation_captain_round_finalized_at=tournament.automation_captain_round_finalized_at,
        automation_assignment_generated_at=tournament.automation_assignment_generated_at,
        automation_last_error=tournament.automation_last_error,
        automation_failure_count=int(tournament.automation_failure_count or 0),
        automation_retry_after=tournament.automation_retry_after,
        created_at=tournament.created_at,
        available_next_statuses=list(
            available_tournament_statuses(
                tournament.status,
                format_slug=tournament.format_slug,
                has_locked_deadlock_roster=has_locked_deadlock_roster,
            )
        ),
        match_count=match_count,
        latest_round_number=latest_round_number,
        unfinished_match_count=unfinished_match_count,
        completed_match_count=completed_match_count,
        cancelled_match_count=cancelled_match_count,
        admin_override_warning=admin_tournament_override_warning(
            tournament=tournament,
            unfinished_match_count=unfinished_match_count,
            has_locked_deadlock_roster=has_locked_deadlock_roster,
        ),
        admin_recovery_hint=admin_tournament_recovery_hint(
            tournament=tournament,
            match_count=match_count,
            unfinished_match_count=unfinished_match_count,
            has_locked_deadlock_roster=has_locked_deadlock_roster,
        ),
    )


@router.get("/overview", response_model=AdminOverviewResponse)
async def admin_overview(
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AdminOverviewResponse:
    ensure_admin_role(auth_session)

    users_total = int(await db_session.scalar(select(func.count()).select_from(User)) or 0)
    tournaments_total = int(await db_session.scalar(select(func.count()).select_from(Tournament)) or 0)
    preprod_test_runs_total = int(
        await db_session.scalar(select(func.count()).select_from(PreprodTestRun))
        or 0
    )
    preprod_test_users_total = int(
        await db_session.scalar(
            select(func.coalesce(func.sum(PreprodTestRun.created_users), 0))
            .where(PreprodTestRun.status != "cleaned")
        )
        or 0
    )
    tournaments_attention_total = int(
        await db_session.scalar(
            select(func.count())
            .select_from(Tournament)
            .where(admin_tournament_attention_filter())
        )
        or 0
    )
    audit_events_total = int(await db_session.scalar(select(func.count()).select_from(AuditLog)) or 0)
    return AdminOverviewResponse(
        users_total=users_total,
        tournaments_total=tournaments_total,
        tournaments_attention_total=tournaments_attention_total,
        audit_events_total=audit_events_total,
        preprod_test_runs_total=preprod_test_runs_total,
        preprod_test_users_total=preprod_test_users_total,
    )


@router.get("/audit-logs", response_model=list[AdminAuditLogResponse])
async def admin_list_audit_logs(
    search: str | None = Query(default=None, max_length=120),
    action: str | None = Query(default=None, max_length=120),
    subject_type: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[AdminAuditLogResponse]:
    ensure_admin_role(auth_session)

    stmt = (
        select(AuditLog, User.display_name, User.email)
        .outerjoin(User, User.id == AuditLog.actor_user_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
    )
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        stmt = stmt.where(
            AuditLog.action.ilike(pattern)
            | AuditLog.subject_type.ilike(pattern)
            | AuditLog.subject_id.ilike(pattern)
            | User.display_name.ilike(pattern)
            | User.email.ilike(pattern)
        )
    if action:
        stmt = stmt.where(AuditLog.action == action.strip())
    if subject_type:
        stmt = stmt.where(AuditLog.subject_type == subject_type.strip())

    rows = (await db_session.execute(stmt)).all()
    return [
        AdminAuditLogResponse(
            id=audit_log.id,
            action=audit_log.action,
            subject_type=audit_log.subject_type,
            subject_id=audit_log.subject_id,
            payload=audit_log.payload,
            created_at=audit_log.created_at,
            actor_display_name=actor_display_name,
            actor_email=actor_email,
        )
        for audit_log, actor_display_name, actor_email in rows
    ]


@router.get("/preprod-test-runs", response_model=list[AdminPreprodTestRunResponse])
async def admin_list_preprod_test_runs(
    limit: int = Query(default=20, ge=1, le=100),
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[AdminPreprodTestRunResponse]:
    ensure_admin_role(auth_session)

    runs = list(
        (
            await db_session.scalars(
                select(PreprodTestRun)
                .order_by(PreprodTestRun.created_at.desc(), PreprodTestRun.marker.desc())
                .limit(limit)
            )
        ).all()
    )
    return [serialize_preprod_run(run) for run in runs]


async def cleanup_preprod_runs(
    db_session: AsyncSession,
    *,
    runs: list[PreprodTestRun],
    auth_session,
    note: str,
) -> AdminPreprodCleanupResponse:
    markers = [run.marker for run in runs]
    user_ids: set[str] = set()
    tournament_ids: set[str] = set()
    for run in runs:
        user_ids.update(preprod_run_user_ids(run))
        tournament_ids.update(preprod_run_tournament_ids(run))

    try:
        await purge_deleted_media_metadata(
            db_session,
            owner_user_ids=user_ids,
            tournament_ids=tournament_ids,
        )
    except MediaCleanupRequired as exc:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "media_cleanup_required",
                "message": "Дождитесь безопасного удаления медиа перед очисткой тестовых данных.",
                "statuses": exc.status_counts,
            },
        ) from exc

    subject_ids = set(user_ids) | set(tournament_ids)
    audit_logs_deleted = 0
    for user_chunk in preprod_cleanup_chunks(user_ids):
        audit_result = await db_session.execute(
            delete(AuditLog).where(AuditLog.actor_user_id.in_(user_chunk))
        )
        audit_logs_deleted += int(audit_result.rowcount or 0)
    for subject_chunk in preprod_cleanup_chunks(subject_ids):
        audit_result = await db_session.execute(
            delete(AuditLog).where(AuditLog.subject_id.in_(subject_chunk))
        )
        audit_logs_deleted += int(audit_result.rowcount or 0)

    tournaments_deleted = await delete_by_ids(db_session, Tournament, tournament_ids) if tournament_ids else 0
    users_deleted = await delete_by_ids(db_session, User, user_ids) if user_ids else 0

    remaining_users = await count_existing_ids(db_session, User, user_ids) if user_ids else 0
    remaining_tournaments = await count_existing_ids(db_session, Tournament, tournament_ids) if tournament_ids else 0
    ok = remaining_users == 0 and remaining_tournaments == 0
    cleanup_state = {
        "ok": ok,
        "cleaned_at": auth_session.now.isoformat(),
        "cleaned_by_user_id": auth_session.user.id,
        "note": note,
        "tournaments_deleted": tournaments_deleted,
        "users_deleted": users_deleted,
        "audit_logs_deleted": audit_logs_deleted,
        "remaining_users": remaining_users,
        "remaining_tournaments": remaining_tournaments,
    }
    for run in runs:
        run.cleanup_state = cleanup_state
        if ok:
            run.status = "cleaned"

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="admin.preprod_test_data.cleanup",
        subject_type="preprod_test_runs",
        subject_id=None,
        payload={
            "markers": markers,
            "note": note,
            "tournaments_deleted": tournaments_deleted,
            "users_deleted": users_deleted,
            "audit_logs_deleted": audit_logs_deleted,
            "remaining_users": remaining_users,
            "remaining_tournaments": remaining_tournaments,
        },
    )
    await db_session.commit()
    return AdminPreprodCleanupResponse(
        ok=ok,
        runs_updated=len(runs),
        tournaments_deleted=tournaments_deleted,
        users_deleted=users_deleted,
        audit_logs_deleted=audit_logs_deleted,
        markers=markers,
        remaining_users=remaining_users,
        remaining_tournaments=remaining_tournaments,
    )


@router.post("/preprod-test-runs/cleanup", response_model=AdminPreprodCleanupResponse)
async def admin_cleanup_all_preprod_test_data(
    payload: AdminPreprodCleanupRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AdminPreprodCleanupResponse:
    ensure_superadmin_role(auth_session)

    runs = list(
        (
            await db_session.scalars(
                select(PreprodTestRun)
                .where(PreprodTestRun.status != "cleaned")
                .order_by(PreprodTestRun.created_at.desc())
            )
        ).all()
    )
    if not runs:
        return AdminPreprodCleanupResponse(ok=True)
    return await cleanup_preprod_runs(
        db_session,
        runs=runs,
        auth_session=auth_session,
        note=payload.note.strip(),
    )


@router.post("/preprod-test-runs/{marker}/cleanup", response_model=AdminPreprodCleanupResponse)
async def admin_cleanup_preprod_test_run(
    marker: str,
    payload: AdminPreprodCleanupRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AdminPreprodCleanupResponse:
    ensure_superadmin_role(auth_session)

    run = await db_session.scalar(select(PreprodTestRun).where(PreprodTestRun.marker == marker))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preprod test run not found.")
    return await cleanup_preprod_runs(
        db_session,
        runs=[run],
        auth_session=auth_session,
        note=payload.note.strip(),
    )


@router.get("/tournaments", response_model=list[AdminTournamentResponse])
async def admin_list_tournaments(
    response: Response,
    search: str | None = Query(default=None, max_length=120),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        pattern="^(registration_open|registration_closed|in_progress|completed|cancelled)$",
    ),
    visibility_filter: str | None = Query(
        default=None,
        alias="visibility",
        pattern="^(public|invite_only)$",
    ),
    attention: bool = Query(default=False),
    limit: int = Query(
        default=TOURNAMENT_LIST_DEFAULT_LIMIT,
        ge=1,
        le=TOURNAMENT_LIST_MAX_LIMIT,
    ),
    offset: int = Query(default=0, ge=0),
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[AdminTournamentResponse]:
    ensure_admin_role(auth_session)

    filters = []
    normalized_search = (search or "").strip()
    if normalized_search:
        search_pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                Tournament.name.ilike(search_pattern),
                Tournament.slug.ilike(search_pattern),
                User.display_name.ilike(search_pattern),
            )
        )
    if status_filter:
        filters.append(Tournament.status == status_filter)
    if visibility_filter:
        filters.append(Tournament.visibility == visibility_filter)
    if attention:
        filters.append(admin_tournament_attention_filter())

    total = int(
        await db_session.scalar(
            select(func.count())
            .select_from(Tournament)
            .join(User, User.id == Tournament.organizer_user_id)
            .where(*filters)
        )
        or 0
    )
    tournament_page = (
        select(
            Tournament.id.label("id"),
            Tournament.created_at.label("created_at"),
        )
        .join(User, User.id == Tournament.organizer_user_id)
        .where(*filters)
        .order_by(Tournament.created_at.desc(), Tournament.id.desc())
        .limit(limit)
        .offset(offset)
        .cte("tournament_page")
    )
    rows = (
        await db_session.execute(
            admin_tournament_with_counts_stmt(tournament_page).order_by(
                tournament_page.c.created_at.desc(),
                tournament_page.c.id.desc(),
            )
        )
    ).all()
    serialized = [
        serialize_tournament(
            tournament,
            organizer_display_name,
            int(participant_count),
            has_locked_deadlock_roster=bool(int(locked_roster_count)),
            match_count=int(match_count),
            latest_round_number=int(latest_round_number) if latest_round_number is not None else None,
            completed_match_count=int(completed_match_count),
            cancelled_match_count=int(cancelled_match_count),
            unfinished_match_count=int(unfinished_match_count),
        )
        for (
            tournament,
            organizer_display_name,
            participant_count,
            locked_roster_count,
            match_count,
            latest_round_number,
            completed_match_count,
            cancelled_match_count,
            unfinished_match_count,
        ) in rows
    ]
    set_pagination_headers(
        response,
        total=total,
        limit=limit,
        offset=offset,
        returned=len(serialized),
    )
    return serialized


async def _admin_roster_response(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    role_slugs: list[str],
) -> AdminRosterResponse:
    snapshot = await load_admin_roster_snapshot(
        db_session,
        tournament=tournament,
        role_slugs=role_slugs,
    )
    return AdminRosterResponse.model_validate(public_roster_snapshot(snapshot))


async def _admin_roster_mutation(
    request: Request,
    *,
    slug: str,
    payload,
    operation: str,
    auth_session,
    db_session: AsyncSession,
) -> AdminRosterResponse:
    ensure_admin_role(auth_session)

    tournament = await db_session.scalar(
        select(Tournament).where(Tournament.slug == slug)
    )
    if tournament is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tournament not found.")

    payload_data = payload.model_dump(mode="json")
    reservation = await reserve_mutation_idempotency(
        db_session,
        actor_user_id=auth_session.user.id,
        scope=f"admin.tournament.roster:{tournament.id}:{operation}",
        key=request_idempotency_key(request),
        request_fingerprint=mutation_payload_fingerprint(
            {
                "tournament_id": tournament.id,
                "operation": operation,
                "payload": payload_data,
            }
        ),
    )
    if reservation is not None and reservation.replay:
        if reservation.record.resource_id != tournament.id:
            await db_session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The idempotency reservation has no matching tournament resource.",
            )
        return await _admin_roster_response(
            db_session,
            tournament=tournament,
            role_slugs=list(auth_session.role_slugs),
        )

    try:
        await mutate_admin_roster(
            db_session,
            tournament_id=tournament.id,
            actor_user_id=auth_session.user.id,
            role_slugs=auth_session.role_slugs,
            operation=operation,
            command=payload.model_dump(
                exclude={"expected_state_version", "reason", "override"}
            ),
            expected_state_version=payload.expected_state_version,
            reason=payload.reason.strip(),
            override=payload.override,
            now=auth_session.now,
        )
        bind_mutation_idempotency_resource(reservation, tournament.id)
        await db_session.commit()
    except AdminRosterError as exc:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrityError as exc:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The roster changed concurrently or violates a roster invariant.",
        ) from exc

    invalidate_tournament_runtime_caches(tournament.id)
    await refresh_tournament_read_models(
        tournament.id,
        ("teams", "workspace_detail", "bracket_summary", "bracket_full"),
    )
    await refresh_tournament_list_read_model_after_commit(tournament.id)
    return await _admin_roster_response(
        db_session,
        tournament=tournament,
        role_slugs=list(auth_session.role_slugs),
    )


@router.get("/tournaments/{slug}/roster", response_model=AdminRosterResponse)
async def admin_get_tournament_roster(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AdminRosterResponse:
    ensure_admin_role(auth_session)
    tournament = await db_session.scalar(
        select(Tournament).where(Tournament.slug == slug)
    )
    if tournament is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tournament not found.")
    return await _admin_roster_response(
        db_session,
        tournament=tournament,
        role_slugs=list(auth_session.role_slugs),
    )


@router.post("/tournaments/{slug}/roster/add-player", response_model=AdminRosterResponse)
async def admin_add_tournament_roster_player(
    request: Request,
    slug: str,
    payload: AdminRosterAddPlayerRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AdminRosterResponse:
    return await _admin_roster_mutation(
        request,
        slug=slug,
        payload=payload,
        operation="player_added",
        auth_session=auth_session,
        db_session=db_session,
    )


@router.post("/tournaments/{slug}/roster/remove-player", response_model=AdminRosterResponse)
async def admin_remove_tournament_roster_player(
    request: Request,
    slug: str,
    payload: AdminRosterRemovePlayerRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AdminRosterResponse:
    return await _admin_roster_mutation(
        request,
        slug=slug,
        payload=payload,
        operation="player_removed",
        auth_session=auth_session,
        db_session=db_session,
    )


@router.post("/tournaments/{slug}/roster/move-player", response_model=AdminRosterResponse)
async def admin_move_tournament_roster_player(
    request: Request,
    slug: str,
    payload: AdminRosterMovePlayerRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AdminRosterResponse:
    return await _admin_roster_mutation(
        request,
        slug=slug,
        payload=payload,
        operation="player_moved",
        auth_session=auth_session,
        db_session=db_session,
    )


@router.post("/tournaments/{slug}/roster/replace-player", response_model=AdminRosterResponse)
async def admin_replace_tournament_roster_player(
    request: Request,
    slug: str,
    payload: AdminRosterReplacePlayerRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AdminRosterResponse:
    return await _admin_roster_mutation(
        request,
        slug=slug,
        payload=payload,
        operation="player_replaced",
        auth_session=auth_session,
        db_session=db_session,
    )


@router.post("/tournaments/{slug}/roster/change-captain", response_model=AdminRosterResponse)
async def admin_change_tournament_roster_captain(
    request: Request,
    slug: str,
    payload: AdminRosterChangeCaptainRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AdminRosterResponse:
    return await _admin_roster_mutation(
        request,
        slug=slug,
        payload=payload,
        operation="captain_changed",
        auth_session=auth_session,
        db_session=db_session,
    )


@router.delete("/tournaments/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_tournament(
    slug: str,
    payload: AdminTournamentDeleteRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    ensure_admin_role(auth_session)

    tournament = await db_session.scalar(
        select(Tournament)
        .where(Tournament.slug == slug)
        .with_for_update()
    )
    if tournament is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tournament not found.")
    if payload.confirmation_name.strip() != tournament.name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Введите точное название турнира для подтверждения удаления.",
        )

    tournament_id = tournament.id
    tournament_name = tournament.name
    tournament_slug = tournament.slug
    cover_key = object_key_from_upload_url(tournament.cover_url)
    try:
        media_metadata_deleted = await purge_deleted_media_metadata(
            db_session,
            tournament_ids=(tournament_id,),
        )
    except MediaCleanupRequired as exc:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "tournament_media_cleanup_required",
                "message": (
                    "Сначала удалите баннер турнира и дождитесь завершения "
                    "безопасной очистки медиа."
                ),
                "statuses": exc.status_counts,
            },
        ) from exc
    assignment_result = await db_session.execute(
        delete(TournamentDeadlockAssignmentRun).where(
            TournamentDeadlockAssignmentRun.tournament_id == tournament_id
        )
    )
    captain_result = await db_session.execute(
        delete(TournamentDeadlockCaptainRound).where(
            TournamentDeadlockCaptainRound.tournament_id == tournament_id
        )
    )
    ready_result = await db_session.execute(
        delete(TournamentDeadlockReadyRound).where(
            TournamentDeadlockReadyRound.tournament_id == tournament_id
        )
    )
    tournament_result = await db_session.execute(
        delete(Tournament).where(Tournament.id == tournament_id)
    )
    if int(tournament_result.rowcount or 0) != 1:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Не удалось удалить турнир из-за конкурентного изменения.",
        )

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="admin.tournament.delete",
        subject_type="tournament",
        subject_id=tournament_id,
        payload={
            "name": tournament_name,
            "slug": tournament_slug,
            "visibility": tournament.visibility,
            "status": tournament.status,
            "organizer_user_id": tournament.organizer_user_id,
            "assignment_runs_deleted": int(assignment_result.rowcount or 0),
            "captain_rounds_deleted": int(captain_result.rowcount or 0),
            "ready_rounds_deleted": int(ready_result.rowcount or 0),
            "media_metadata_deleted": media_metadata_deleted,
            "note": payload.note.strip(),
        },
    )
    await db_session.commit()

    await delete_tournament_read_models(
        tournament_id,
        ("teams", "workspace_detail", "bracket_summary", "bracket_full"),
    )
    await delete_tournament_profile_access_state(tournament_slug)

    from apps.platform_api.app.services.tournament_runtime_cache import (
        invalidate_tournament_runtime_caches,
    )

    invalidate_tournament_runtime_caches(tournament_id)
    if cover_key is not None:
        await run_in_threadpool(get_object_storage().delete, cover_key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users", response_model=list[AdminUserResponse])
async def admin_list_users(
    search: str | None = Query(default=None, max_length=120),
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[AdminUserResponse]:
    ensure_admin_role(auth_session)

    stmt = select(User).order_by(User.created_at.desc()).limit(100)
    normalized_search = (search or "").strip()
    if normalized_search:
        search_pattern = f"%{normalized_search}%"
        stmt = stmt.where(
            (User.email.ilike(search_pattern)) | (User.display_name.ilike(search_pattern))
        )

    users = list((await db_session.scalars(stmt)).all())
    roles_by_user = await role_slugs_for_users(db_session, [user.id for user in users])
    return [serialize_user(user, roles_by_user.get(user.id, [])) for user in users]


@router.patch("/users/{user_id}/tournament-credits", response_model=AdminUserResponse)
async def admin_update_tournament_credits(
    user_id: str,
    payload: AdminUserTournamentCreditsUpdateRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AdminUserResponse:
    ensure_admin_role(auth_session)

    user = await db_session.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    previous_public_credits = int(user.public_tournament_credits or 0)
    previous_private_credits = int(user.private_tournament_credits or 0)
    user.public_tournament_credits = payload.public_tournament_credits
    user.private_tournament_credits = payload.private_tournament_credits
    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="admin.user.tournament_credits",
        subject_type="user",
        subject_id=user.id,
        payload={
            "previous_public_tournament_credits": previous_public_credits,
            "public_tournament_credits": user.public_tournament_credits,
            "previous_private_tournament_credits": previous_private_credits,
            "private_tournament_credits": user.private_tournament_credits,
            "note": payload.note.strip(),
        },
    )
    await db_session.commit()
    invalidate_user_session_cache(user.id)
    await db_session.refresh(user)
    roles_by_user = await role_slugs_for_users(db_session, [user.id])
    return serialize_user(user, roles_by_user.get(user.id, []))


@router.patch("/users/{user_id}/admin-role", response_model=AdminUserResponse)
async def admin_update_admin_role(
    user_id: str,
    payload: AdminUserRoleUpdateRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AdminUserResponse:
    ensure_superadmin_role(auth_session)

    user = await db_session.scalar(
        select(User).where(User.id == user_id).with_for_update()
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    admin_role = await db_session.scalar(select(Role).where(Role.slug == "admin"))
    if admin_role is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Admin role is missing.")

    existing = await db_session.scalar(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role_id == admin_role.id,
        )
    )
    was_admin = existing is not None
    if payload.is_admin and existing is None:
        db_session.add(UserRole(user_id=user.id, role_id=admin_role.id))
    elif not payload.is_admin and existing is not None:
        await db_session.execute(
            delete(UserRole).where(
                UserRole.user_id == user.id,
                UserRole.role_id == admin_role.id,
            )
        )
    if was_admin == payload.is_admin:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Admin role did not change.")

    invalidated_sessions = await invalidate_user_sessions(
        db_session,
        user_id=user.id,
        now=auth_session.now,
    )

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="admin.user.admin_role",
        subject_type="user",
        subject_id=user.id,
        payload={
            "previous_is_admin": was_admin,
            "is_admin": payload.is_admin,
            "sessions_invalidated": invalidated_sessions,
            "note": payload.note.strip(),
        },
    )
    await db_session.commit()
    invalidate_user_session_cache(user.id)
    roles_by_user = await role_slugs_for_users(db_session, [user.id])
    return serialize_user(user, roles_by_user.get(user.id, []))


@router.patch("/tournaments/{slug}", response_model=AdminTournamentResponse)
async def admin_override_tournament(
    slug: str,
    payload: AdminTournamentOverrideRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AdminTournamentResponse:
    ensure_admin_role(auth_session)

    tournament = await db_session.scalar(
        select(Tournament).where(Tournament.slug == slug).with_for_update()
    )
    if tournament is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tournament not found.")

    original_status = tournament.status
    original_visibility = tournament.visibility
    original_schedule = (
        tournament.registration_starts_at,
        tournament.registration_closes_at,
        tournament.ready_check_starts_at,
        tournament.ready_check_ends_at,
        tournament.captain_selection_starts_at,
        tournament.starts_at,
    )
    active_ready_round = await db_session.scalar(
        select(TournamentDeadlockReadyRound)
        .where(
            TournamentDeadlockReadyRound.tournament_id == tournament.id,
            TournamentDeadlockReadyRound.status == "active",
        )
        .with_for_update()
    )
    note = (payload.note or "").strip() or None

    if payload.status is None and payload.visibility is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Provide status or visibility to apply an override.",
        )
    if note is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Provide an audit note before applying an admin override.",
        )

    if payload.status is not None:
        if payload.status == "registration_open":
            locked_roster_count = await db_session.scalar(
                select(func.count())
                .select_from(TournamentDeadlockAssignmentRun)
                .where(
                    TournamentDeadlockAssignmentRun.tournament_id == tournament.id,
                    TournamentDeadlockAssignmentRun.status == "locked",
                )
            )
            if int(locked_roster_count or 0) > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Registration cannot be opened while the Deadlock roster is locked.",
                )
            await supersede_published_deadlock_assignment_run_for_tournament(
                db_session,
                tournament_id=tournament.id,
            )
            schedule_values = (
                payload.registration_closes_at,
                payload.ready_check_starts_at,
                payload.ready_check_ends_at,
                payload.captain_selection_starts_at,
                payload.starts_at,
            )
            if any(value is None or value <= auth_session.now for value in schedule_values):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="All new workflow dates must be in the future.",
                )
            tournament.registration_starts_at = auth_session.now
            tournament.registration_closes_at = payload.registration_closes_at
            tournament.ready_check_starts_at = payload.ready_check_starts_at
            tournament.ready_check_ends_at = payload.ready_check_ends_at
            tournament.captain_selection_starts_at = payload.captain_selection_starts_at
            tournament.starts_at = payload.starts_at
            tournament.automation_ready_check_started_at = None
            tournament.automation_ready_check_closed_at = None
            tournament.automation_captain_round_started_at = None
            tournament.automation_captain_round_finalized_at = None
            tournament.automation_assignment_generated_at = None
            tournament.automation_last_error = None
            tournament.automation_failure_count = 0
            tournament.automation_retry_after = None
        elif payload.status == "registration_closed":
            tournament.registration_closes_at = auth_session.now
        tournament.status = payload.status
    if payload.visibility is not None:
        if payload.visibility == "public" and original_visibility != "public":
            normalized_name = await lock_tournament_name(db_session, name=tournament.name)
            if await public_tournament_name_exists(
                db_session,
                normalized_name=normalized_name,
                exclude_tournament_id=tournament.id,
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Турнир с таким публичным названием уже существует.",
                )
        tournament.visibility = payload.visibility

    schedule_changed = original_schedule != (
        tournament.registration_starts_at,
        tournament.registration_closes_at,
        tournament.ready_check_starts_at,
        tournament.ready_check_ends_at,
        tournament.captain_selection_starts_at,
        tournament.starts_at,
    )
    ready_check_schedule_changed = original_schedule[2:4] != (
        tournament.ready_check_starts_at,
        tournament.ready_check_ends_at,
    )
    ready_round_closed_by_override = active_ready_round is not None and (
        tournament.status in {"completed", "cancelled"}
        or ready_check_schedule_changed
    )
    if ready_round_closed_by_override:
        active_ready_round.status = "closed"
        active_ready_round.closed_at = active_ready_round.closed_at or auth_session.now
        if tournament.status in {"completed", "cancelled"}:
            mark_ready_check_closed(tournament, now=auth_session.now)
    if (
        tournament.status == original_status
        and tournament.visibility == original_visibility
        and not schedule_changed
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Override did not change tournament state.",
        )

    released_commitments = 0
    reactivated_commitments = 0
    if tournament.status in {"completed", "cancelled"}:
        released_commitments = await release_active_commitments(
            db_session,
            tournament_id=tournament.id,
            released_at=auth_session.now,
            release_reason=(
                "admin_tournament_completed"
                if tournament.status == "completed"
                else "admin_tournament_cancelled"
            ),
        )
    elif original_status in {"completed", "cancelled"} and tournament.status != original_status:
        try:
            reactivated_commitments = await reactivate_viable_tournament_commitments(
                db_session,
                tournament_id=tournament.id,
                activated_at=auth_session.now,
            )
        except PlayerCommitmentConflict as exc:
            conflicts = ", ".join(
                f"{item.team_name} / {item.tournament_name}"
                for item in exc.commitments
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "The tournament cannot be reopened because one or more roster players "
                    f"are now committed elsewhere: {conflicts}."
                ),
            ) from exc

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="admin.tournament.override",
        subject_type="tournament",
        subject_id=tournament.id,
        payload={
            "tournament_slug": tournament.slug,
            "from_status": original_status,
            "to_status": tournament.status,
            "from_visibility": original_visibility,
            "to_visibility": tournament.visibility,
            "registration_starts_at": tournament.registration_starts_at.isoformat()
            if tournament.registration_starts_at
            else None,
            "registration_closes_at": tournament.registration_closes_at.isoformat()
            if tournament.registration_closes_at
            else None,
            "ready_check_starts_at": tournament.ready_check_starts_at.isoformat()
            if tournament.ready_check_starts_at
            else None,
            "ready_check_ends_at": tournament.ready_check_ends_at.isoformat()
            if tournament.ready_check_ends_at
            else None,
            "captain_selection_starts_at": tournament.captain_selection_starts_at.isoformat()
            if tournament.captain_selection_starts_at
            else None,
            "starts_at": tournament.starts_at.isoformat() if tournament.starts_at else None,
            "released_commitments": released_commitments,
            "reactivated_commitments": reactivated_commitments,
            "note": note,
        },
    )
    try:
        await db_session.commit()
    except IntegrityError as exc:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Турнир с таким публичным названием уже существует.",
        ) from exc
    await refresh_tournament_list_read_model_after_commit(tournament.id)
    await db_session.refresh(tournament)
    if ready_round_closed_by_override and active_ready_round is not None:
        await db_session.refresh(active_ready_round)

    row = (
        await db_session.execute(
            admin_tournament_with_counts_stmt().where(Tournament.id == tournament.id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tournament not found.")
    (
        tournament,
        organizer_display_name,
        participant_count,
        locked_roster_count,
        match_count,
        latest_round_number,
        completed_match_count,
        cancelled_match_count,
        unfinished_match_count,
    ) = row
    return serialize_tournament(
        tournament,
        organizer_display_name or "Unknown",
        int(participant_count),
        has_locked_deadlock_roster=bool(int(locked_roster_count)),
        match_count=int(match_count),
        latest_round_number=int(latest_round_number) if latest_round_number is not None else None,
        completed_match_count=int(completed_match_count),
        cancelled_match_count=int(cancelled_match_count),
        unfinished_match_count=int(unfinished_match_count),
    )
