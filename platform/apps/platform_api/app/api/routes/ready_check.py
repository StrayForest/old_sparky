from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import (
    ReadyCheckAgendaItemResponse,
    ReadyCheckAgendaResponse,
    ReadyCheckStateProbeResponse,
)
from apps.platform_api.app.services.ready_check_events import (
    read_ready_check_state,
    stream_ready_check_events,
)
from python_packages.platform_domain.tournaments import SOLO_TOURNAMENT_FORMAT
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.models import (
    Tournament,
    TournamentDeadlockReadyRound,
    TournamentParticipant,
)
from python_packages.platform_infra.ready_check_admission import (
    ReadyCheckAdmissionInvalid,
    issue_ready_check_state_proof,
    issue_ready_check_stream_proof,
    ready_check_proof_expiration,
    verify_ready_check_state_proof,
    verify_ready_check_stream_proof,
)
from python_packages.platform_infra.ready_check_policy import (
    ReadyCheckDemand,
    ready_check_preparation_plan,
    ready_check_user_admission,
    proportional_ready_check_capacity,
)
from python_packages.platform_infra.security import get_authenticated_session
from python_packages.platform_infra.sse_connection_limit import (
    READY_CHECK_SSE_GLOBAL_LIMIT,
    READY_CHECK_SSE_USER_LIMIT,
    SSE_CONNECTION_LEASE_SCOPE,
    SseConnectionLease,
    add_sse_authenticated_user_scope,
    current_ready_check_sse_connection_count,
    qa_sse_capacity_limit,
)


router = APIRouter()
stream_router = APIRouter()


def _stream_cookie(request: Request) -> str:
    from python_packages.platform_infra.config import get_settings

    token = request.cookies.get(get_settings().platform_session_cookie_name)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    return token


@router.get("/agenda", response_model=ReadyCheckAgendaResponse)
async def get_ready_check_agenda(
    request: Request,
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> ReadyCheckAgendaResponse:
    """Return upcoming checks for the current user, including open timing.

    This is an agenda read, not a polling endpoint. It may use PostgreSQL once
    per navigation/refresh; the hot fallback state endpoint below does not.
    """

    response.headers["Cache-Control"] = "private, no-store"
    now = auth_session.now.astimezone(UTC)
    rows = (
        await db_session.execute(
            select(Tournament)
            .join(
                TournamentParticipant,
                TournamentParticipant.tournament_id == Tournament.id,
            )
            .where(
                TournamentParticipant.user_id == auth_session.user.id,
                TournamentParticipant.status.not_in(("withdrawn", "disqualified")),
                Tournament.format_slug == SOLO_TOURNAMENT_FORMAT,
                Tournament.ready_check_starts_at.is_not(None),
                Tournament.ready_check_ends_at > now,
                Tournament.status.not_in(("completed", "cancelled")),
            )
            .order_by(Tournament.ready_check_starts_at, Tournament.id)
        )
    ).scalars().all()
    if not rows:
        return ReadyCheckAgendaResponse()

    # Planning demand is global to the simultaneous Ready Check cohort, not
    # just the tournaments visible to this user. The returned agenda remains
    # user-scoped, while the deterministic quota calculation sees every
    # approaching eligible tournament so independent agenda requests converge
    # on the same proportional plan before Redis applies the final cap.
    planning_rows = (
        await db_session.execute(
            select(Tournament)
            .where(
                Tournament.format_slug == SOLO_TOURNAMENT_FORMAT,
                Tournament.ready_check_starts_at.is_not(None),
                Tournament.ready_check_ends_at > now,
                Tournament.status.not_in(("completed", "cancelled")),
            )
            .order_by(Tournament.ready_check_starts_at, Tournament.id)
        )
    ).scalars().all()
    if not planning_rows:
        return ReadyCheckAgendaResponse()

    already_connected = await current_ready_check_sse_connection_count()
    ready_check_capacity = qa_sse_capacity_limit(request) or READY_CHECK_SSE_GLOBAL_LIMIT

    tournament_ids = [tournament.id for tournament in planning_rows]
    count_rows = await db_session.execute(
        select(
            TournamentParticipant.tournament_id,
            func.count(TournamentParticipant.user_id),
        )
        .where(
            TournamentParticipant.tournament_id.in_(tournament_ids),
            TournamentParticipant.status.not_in(("withdrawn", "disqualified")),
        )
        .group_by(TournamentParticipant.tournament_id)
    )
    eligible_counts = {str(tournament_id): int(count) for tournament_id, count in count_rows.all()}
    active_round_rows = await db_session.scalars(
        select(TournamentDeadlockReadyRound).where(
            TournamentDeadlockReadyRound.tournament_id.in_(tournament_ids),
            TournamentDeadlockReadyRound.status == "active",
        )
    )
    active_rounds = {str(round_row.tournament_id): round_row for round_row in active_round_rows.all()}

    demands = tuple(
        ReadyCheckDemand(
            tournament_id=str(tournament.id),
            starts_at=tournament.ready_check_starts_at,
            eligible_count=len(list(active_rounds[str(tournament.id)].eligible_user_ids or []))
            if str(tournament.id) in active_rounds
            else eligible_counts.get(str(tournament.id), 0),
        )
        for tournament in planning_rows
        if tournament.ready_check_starts_at is not None
    )
    checks: list[ReadyCheckAgendaItemResponse] = []
    session_token = _stream_cookie(request)
    for tournament in rows:
        tournament_id = str(tournament.id)
        demand = next((item for item in demands if item.tournament_id == tournament_id), None)
        if demand is None or demand.eligible_count < 1:
            continue
        # Checks in the same preparation horizon share the same global plan.
        cohort = tuple(
            item
            for item in demands
            if abs((item.starts_at - demand.starts_at).total_seconds()) <= 15 * 60
        )
        plan = ready_check_preparation_plan(
            cohort,
            already_connected=already_connected,
        )
        quotas = proportional_ready_check_capacity(
            cohort,
            capacity=max(0, ready_check_capacity - already_connected),
        )
        open_at, priority, admission_mode = ready_check_user_admission(
            plan,
            demand=demand,
            user_id=str(auth_session.user.id),
            sse_quota=quotas.get(tournament_id, 0),
            now=now,
        )
        checks.append(
            ReadyCheckAgendaItemResponse(
                tournament_id=tournament_id,
                slug=tournament.slug,
                ready_check_starts_at=tournament.ready_check_starts_at,
                ready_check_ends_at=tournament.ready_check_ends_at,
                admission_open_at=open_at,
                admission_priority=priority,
                admission_mode=admission_mode,
                state_ticket=issue_ready_check_state_proof(
                    tournament_id=tournament_id,
                    slug=tournament.slug,
                    user_id=str(auth_session.user.id),
                    session_id=str(auth_session.session.id),
                    session_token=session_token,
                    ready_check_starts_at=tournament.ready_check_starts_at,
                    ready_check_ends_at=tournament.ready_check_ends_at,
                    now=now,
                ),
            )
        )
    if not checks:
        return ReadyCheckAgendaResponse()
    stream_checks = [item for item in checks if item.admission_mode != "polling"]
    stream_open_at = min(item.admission_open_at for item in stream_checks) if stream_checks else None
    stream_ends_at = max(item.ready_check_ends_at for item in stream_checks) if stream_checks else None
    stream_ticket_expires_at = None
    if stream_open_at is not None and stream_ends_at is not None:
        stream_issued_at = max(
            int(now.timestamp()),
            int(stream_open_at.timestamp()),
        )
        stream_ticket_expires_at = datetime.fromtimestamp(
            ready_check_proof_expiration(
                issued_at=stream_issued_at,
                ready_check_ends_at=int(stream_ends_at.timestamp()),
            ),
            UTC,
        )
    return ReadyCheckAgendaResponse(
        checks=checks,
        sse_ticket=(
            issue_ready_check_stream_proof(
                user_id=str(auth_session.user.id),
                session_id=str(auth_session.session.id),
                session_token=session_token,
                tournament_ids=[item.tournament_id for item in stream_checks],
                admission_open_at=stream_open_at,
                ready_check_ends_at=stream_ends_at,
                now=now,
            )
            if stream_checks
            else None
        ),
        sse_ticket_expires_at=stream_ticket_expires_at,
    )

@router.get("/state", response_model=ReadyCheckStateProbeResponse)
async def get_ready_check_state(
    request: Request,
    response: Response,
    ticket: str = Query(min_length=1, max_length=16384),
) -> ReadyCheckStateProbeResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        proof = verify_ready_check_state_proof(
            ticket,
            expected_slug=str(request.query_params.get("slug") or ""),
            session_token=_stream_cookie(request),
        )
    except ReadyCheckAdmissionInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ready Check state proof is invalid or expired.",
            headers={"Cache-Control": "no-store"},
        ) from exc
    projected = await read_ready_check_state(
        tournament_id=proof.tournament_id,
        user_id=proof.user_id,
        ready_check_starts_at=proof.ready_check_starts_at,
    )
    if projected is None:
        return ReadyCheckStateProbeResponse()
    projected_status = projected.get("status")
    if projected_status not in {"waiting", "active", "closed"}:
        return ReadyCheckStateProbeResponse()
    try:
        revision = int(projected.get("revision") or 0)
    except (TypeError, ValueError):
        revision = 0
    return ReadyCheckStateProbeResponse(
        revision=revision,
        status=projected_status,
    )


@stream_router.get("/events")
async def get_ready_check_events(
    request: Request,
    ticket: str = Query(min_length=1, max_length=16384),
) -> StreamingResponse:
    try:
        proof = verify_ready_check_stream_proof(
            ticket,
            session_token=_stream_cookie(request),
        )
    except ReadyCheckAdmissionInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ready Check stream proof is invalid or expired.",
            headers={"Cache-Control": "no-store"},
        ) from exc
    now_epoch = int(datetime.now(UTC).timestamp())
    if now_epoch + 5 < proof.admission_open_at:
        raise HTTPException(
            status_code=status.HTTP_425_TOO_EARLY,
            detail="Ready Check stream admission is not open yet.",
            headers={
                "Cache-Control": "no-store",
                "Retry-After": str(max(1, proof.admission_open_at - now_epoch)),
            },
        )
    lease = request.scope.get(SSE_CONNECTION_LEASE_SCOPE)
    if not isinstance(lease, SseConnectionLease):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Live-update connection protection is temporarily unavailable.",
        )
    await add_sse_authenticated_user_scope(
        request,
        proof.user_id,
        user_limit=READY_CHECK_SSE_USER_LIMIT,
        user_scope="ready_check",
    )
    return StreamingResponse(
        stream_ready_check_events(
            proof.tournament_ids,
            connection_lease=lease,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
