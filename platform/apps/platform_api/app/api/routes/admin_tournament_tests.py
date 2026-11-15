from __future__ import annotations

import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import (
    AdminTournamentTestCreateRequest,
    AdminTournamentTestDeleteRequest,
    AdminTournamentTestGenerateResponse,
    AdminTournamentTestPlayersRequest,
    AdminTournamentTestResponse,
    TournamentStatusUpdateRequest,
)
from apps.platform_api.app.services.mutation_idempotency import (
    bind_mutation_idempotency_resource,
    mutation_payload_fingerprint,
    request_idempotency_key,
    reserve_mutation_idempotency,
)
from apps.platform_api.app.services.admin_tournament_test import (
    MAX_FAKE_PLAYERS,
    active_human_user_ids,
    fake_users_have_external_references,
    summarize_admin_tournament_test,
)
from apps.platform_api.app.services.tournament_catalog_read_models import refresh_tournament_list_read_model_after_commit
from apps.platform_api.app.services.tournament_read_models import delete_tournament_read_models
from apps.platform_api.app.services.tournament_profile_access import delete_tournament_profile_access_state
from apps.platform_api.app.services.tournament_runtime_cache import invalidate_tournament_runtime_caches
from apps.platform_api.app.services.user_account_read_models import delete_user_account_read_model
from python_packages.platform_infra.media.hard_delete import MediaCleanupRequired, purge_deleted_media_metadata
from python_packages.platform_infra.object_storage import get_object_storage, object_key_from_upload_url
from apps.platform_api.app.services.deadlock_automation import advance_deadlock_tournament_automation
from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.models import (
    AdminTournamentTestFixture,
    AdminTournamentTestPlayer,
    DeadlockProfile,
    PlayerProfile,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockReadyRound,
    TournamentInvite,
    TournamentMatch,
    User,
)
from python_packages.platform_infra.security import get_authenticated_session
from python_packages.platform_infra.tournament_names import lock_tournament_name, public_tournament_name_exists

router = APIRouter()
def _ensure_admin(auth_session) -> None:
    if not ({"admin", "superadmin"} & set(auth_session.role_slugs)):
        raise HTTPException(status_code=403, detail="Admin role is required.")


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return f"test-{normalized[:90]}-{uuid4().hex[:8]}"


async def _fixture_or_404(db: AsyncSession, fixture_id: str, *, lock: bool = False):
    fixture_stmt = select(AdminTournamentTestFixture).where(
        AdminTournamentTestFixture.id == fixture_id,
        AdminTournamentTestFixture.status == "active",
    )
    fixture = await db.scalar(fixture_stmt)
    if fixture is None:
        raise HTTPException(status_code=404, detail="Test tournament not found.")
    tournament_stmt = select(Tournament).where(Tournament.id == fixture.tournament_id)
    if lock:
        tournament_stmt = tournament_stmt.with_for_update()
    tournament = await db.scalar(tournament_stmt)
    if lock:
        locked_fixture = await db.scalar(
            fixture_stmt.with_for_update()
        )
        if locked_fixture is None or locked_fixture.tournament_id != fixture.tournament_id:
            raise HTTPException(status_code=404, detail="Test tournament not found.")
        fixture = locked_fixture
    if tournament is None or not tournament.admin_test_fixture:
        raise HTTPException(status_code=404, detail="Test tournament not found.")
    return fixture, tournament


@router.get("/tournament-tests", response_model=list[AdminTournamentTestResponse])
async def list_admin_tournament_tests(
    auth_session=Depends(get_authenticated_session), db: AsyncSession = Depends(get_db_session)
):
    _ensure_admin(auth_session)
    fixtures = (await db.scalars(
        select(AdminTournamentTestFixture)
        .where(AdminTournamentTestFixture.status == "active")
        .order_by(AdminTournamentTestFixture.created_at.desc())
    )).all()
    result = []
    for fixture in fixtures:
        tournament = await db.scalar(select(Tournament).where(Tournament.id == fixture.tournament_id))
        if tournament is not None and tournament.admin_test_fixture:
            result.append(await summarize_admin_tournament_test(db, fixture, tournament))
    return result


@router.post("/tournament-tests", response_model=AdminTournamentTestResponse, status_code=201)
async def create_admin_tournament_test(
    payload: AdminTournamentTestCreateRequest, request: Request,
    auth_session=Depends(get_authenticated_session), db: AsyncSession = Depends(get_db_session),
):
    _ensure_admin(auth_session)
    idem = await reserve_mutation_idempotency(
        db, actor_user_id=auth_session.user.id, scope="admin.tournament_test.create",
        key=request_idempotency_key(request),
        request_fingerprint=mutation_payload_fingerprint(payload.model_dump(mode="json")),
    )
    if idem is not None and idem.replay and idem.record.resource_id:
        fixture, tournament = await _fixture_or_404(db, idem.record.resource_id)
        return await summarize_admin_tournament_test(db, fixture, tournament)
    fixture_name = f"[TEST] {payload.name.strip()}"
    normalized_name = await lock_tournament_name(db, name=fixture_name)
    if payload.visibility == "public" and await public_tournament_name_exists(db, normalized_name=normalized_name):
        raise HTTPException(status_code=409, detail="A public tournament with this name already exists.")
    tournament = Tournament(
        slug=_slugify(payload.name), name=fixture_name, visibility=payload.visibility,
        status="registration_open", format_slug="solo", allowed_ranks=[], max_participants=None,
        organizer_user_id=auth_session.user.id, admin_test_fixture=True,
        match_format="bo1", final_format="bo3", teams_count=None,
    )
    db.add(tournament)
    await db.flush()
    code = await __import__("apps.platform_api.app.api.routes.tournaments", fromlist=["generate_unique_invite_code"]).generate_unique_invite_code(db)
    db.add(TournamentInvite(
        tournament_id=tournament.id, code=code, note="Admin test fixture",
        max_uses=99999, use_count=0, expires_at=None, created_by_user_id=auth_session.user.id,
    ))
    fixture = AdminTournamentTestFixture(tournament_id=tournament.id, creator_user_id=auth_session.user.id)
    db.add(fixture)
    await db.flush()
    bind_mutation_idempotency_resource(idem, fixture.id)
    await write_audit_log(db, actor_user_id=auth_session.user.id, action="admin.tournament_test.create",
                          subject_type="admin_tournament_test_fixture", subject_id=fixture.id,
                          payload={"slug": tournament.slug, "visibility": tournament.visibility})
    await db.commit()
    await refresh_tournament_list_read_model_after_commit(tournament.id)
    return await summarize_admin_tournament_test(db, fixture, tournament)


@router.post("/tournament-tests/{fixture_id}/players", response_model=AdminTournamentTestResponse)
async def add_admin_tournament_test_players(
    fixture_id: str, payload: AdminTournamentTestPlayersRequest, request: Request,
    auth_session=Depends(get_authenticated_session), db: AsyncSession = Depends(get_db_session),
):
    _ensure_admin(auth_session)
    fixture, tournament = await _fixture_or_404(db, fixture_id, lock=True)
    idem = await reserve_mutation_idempotency(
        db, actor_user_id=auth_session.user.id,
        scope=f"admin.tournament_test.players:{fixture.id}",
        key=request_idempotency_key(request),
        request_fingerprint=mutation_payload_fingerprint(payload.model_dump(mode="json")),
    )
    if idem is not None and idem.replay:
        return await summarize_admin_tournament_test(db, fixture, tournament)
    latest = await db.scalar(select(TournamentDeadlockReadyRound).where(
        TournamentDeadlockReadyRound.tournament_id == tournament.id
    ).order_by(TournamentDeadlockReadyRound.id.desc()).limit(1))
    if latest is None or latest.status != "closed":
        raise HTTPException(status_code=409, detail="Добавляйте тестовых игроков после закрытия Ready Check.")
    existing = int(await db.scalar(select(func.count()).select_from(AdminTournamentTestPlayer).where(
        AdminTournamentTestPlayer.fixture_id == fixture.id
    )) or 0)
    if existing + payload.count > MAX_FAKE_PLAYERS:
        raise HTTPException(status_code=422, detail={"code": "fake_player_limit", "remaining": MAX_FAKE_PLAYERS-existing})
    from apps.platform_api.app.api.routes.tournaments import create_participant
    for index in range(existing + 1, existing + payload.count + 1):
        suffix = uuid4().hex[:8]
        user = User(email=None, display_name=f"Тестовый игрок {index:02d}", status="active")
        db.add(user)
        await db.flush()
        db.add(PlayerProfile(user_id=user.id, display_name=user.display_name, handle=f"test_{suffix}"))
        db.add(DeadlockProfile(
            user_id=user.id, rank="Seeker", subrank=3, playtime="501-1000",
            roles=["Carry", "Support"], pool=["Abrams", "Bebop"], captain_priority="neutral",
        ))
        await db.flush()
        participant = await create_participant(db, tournament=tournament, user=user, entry_type="solo", team_name=None)
        participant.status = "confirmed"
        db.add(AdminTournamentTestPlayer(fixture_id=fixture.id, user_id=user.id, prepared_ready=True))
    await write_audit_log(db, actor_user_id=auth_session.user.id, action="admin.tournament_test.players.add",
                          subject_type="admin_tournament_test_fixture", subject_id=fixture.id,
                          payload={"count": payload.count})
    bind_mutation_idempotency_resource(idem, fixture.id)
    await db.commit()
    return await summarize_admin_tournament_test(db, fixture, tournament)


@router.post("/tournament-tests/{fixture_id}/ready-check/start", response_model=AdminTournamentTestResponse)
async def start_admin_tournament_test_ready_check(
    fixture_id: str, request: Request,
    auth_session=Depends(get_authenticated_session), db: AsyncSession = Depends(get_db_session),
):
    _ensure_admin(auth_session)
    fixture, tournament = await _fixture_or_404(db, fixture_id)
    from apps.platform_api.app.api.routes.tournaments import start_deadlock_ready_check
    if not await active_human_user_ids(db, tournament.id):
        active_round = await db.scalar(select(TournamentDeadlockReadyRound.id).where(
            TournamentDeadlockReadyRound.tournament_id == tournament.id,
            TournamentDeadlockReadyRound.status == "active",
        ))
        if active_round is None:
            raise HTTPException(status_code=409, detail={"code": "no_active_participants"})
    if tournament.status == "registration_open":
        from apps.platform_api.app.api.routes.tournaments import update_tournament_status
        await update_tournament_status(
            tournament.slug,
            TournamentStatusUpdateRequest(status="registration_closed"),
            auth_session=auth_session,
            db_session=db,
        )
    try:
        await start_deadlock_ready_check(tournament.slug, auth_session=auth_session, db_session=db)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        latest = await db.scalar(select(TournamentDeadlockReadyRound).where(
            TournamentDeadlockReadyRound.tournament_id == tournament.id,
            TournamentDeadlockReadyRound.status == "active",
        ))
        if latest is None:
            raise
    return await summarize_admin_tournament_test(db, fixture, tournament)


@router.post("/tournament-tests/{fixture_id}/ready-check/close", response_model=AdminTournamentTestResponse)
async def close_admin_tournament_test_ready_check(
    fixture_id: str, request: Request,
    auth_session=Depends(get_authenticated_session), db: AsyncSession = Depends(get_db_session),
):
    _ensure_admin(auth_session)
    fixture, tournament = await _fixture_or_404(db, fixture_id)
    from apps.platform_api.app.api.routes.tournaments import close_deadlock_ready_check
    try:
        await close_deadlock_ready_check(tournament.slug, auth_session=auth_session, db_session=db)
    except HTTPException as exc:
        if exc.status_code != 409 or "not active" not in str(exc.detail):
            raise
        latest = await db.scalar(select(TournamentDeadlockReadyRound).where(
            TournamentDeadlockReadyRound.tournament_id == tournament.id,
        ).order_by(TournamentDeadlockReadyRound.id.desc()).limit(1))
        if latest is None or latest.status != "closed":
            raise
    return await summarize_admin_tournament_test(db, fixture, tournament)


@router.post("/tournament-tests/{fixture_id}/teams/generate", response_model=AdminTournamentTestGenerateResponse)
async def generate_admin_tournament_test_teams(
    fixture_id: str, request: Request,
    auth_session=Depends(get_authenticated_session), db: AsyncSession = Depends(get_db_session),
):
    _ensure_admin(auth_session)
    fixture, tournament = await _fixture_or_404(db, fixture_id, lock=True)
    summary = await summarize_admin_tournament_test(db, fixture, tournament)
    if summary.stage == "teams_locked":
        run = await db.scalar(select(TournamentDeadlockAssignmentRun).where(
            TournamentDeadlockAssignmentRun.tournament_id == tournament.id,
            TournamentDeadlockAssignmentRun.status == "locked",
        ).order_by(TournamentDeadlockAssignmentRun.created_at.desc()).limit(1))
        return AdminTournamentTestGenerateResponse(**summary.model_dump(), assignment_run_id=run.id, teams=run.result_snapshot.get("teams", []))
    if summary.stage != "ready_check_closed":
        raise HTTPException(status_code=409, detail="Закройте Ready Check перед формированием команд.")
    if any(blocker.code == "human_profiles_missing" for blocker in summary.blockers):
        raise HTTPException(status_code=409, detail={"code": "human_profiles_missing"})
    if summary.counts.filler_needed:
        raise HTTPException(status_code=409, detail={"detail": "Недостаточно тестовых игроков.", "blocker": {"code": "insufficient_fillers", "required": summary.counts.required_slots, "available": summary.counts.human_active + summary.counts.fake_ready_yes, "filler_needed": summary.counts.filler_needed}})
    tournament.teams_count = summary.counts.teams_count
    tournament.captain_selection_starts_at = auth_session.now
    await db.flush()
    result = await advance_deadlock_tournament_automation(
        db, tournament=tournament, now=auth_session.now, allow_manual_test=True,
    )
    if result.errors:
        raise HTTPException(status_code=409, detail={"code": "human_assignment_failed"})
    run = await db.scalar(select(TournamentDeadlockAssignmentRun).where(
        TournamentDeadlockAssignmentRun.tournament_id == tournament.id,
        TournamentDeadlockAssignmentRun.status == "locked",
    ).order_by(TournamentDeadlockAssignmentRun.created_at.desc()).limit(1))
    if run is None:
        raise HTTPException(status_code=409, detail={"code": "human_assignment_failed"})
    summary = await summarize_admin_tournament_test(db, fixture, tournament)
    return AdminTournamentTestGenerateResponse(**summary.model_dump(), assignment_run_id=run.id,
        teams=run.result_snapshot.get("teams", []), bracket_match_count=int(await db.scalar(
            select(func.count()).select_from(TournamentMatch).where(TournamentMatch.tournament_id == tournament.id)
        ) or 0))


@router.delete("/tournament-tests/{fixture_id}", status_code=204)
async def delete_admin_tournament_test(
    fixture_id: str, payload: AdminTournamentTestDeleteRequest, request: Request,
    auth_session=Depends(get_authenticated_session), db: AsyncSession = Depends(get_db_session),
):
    _ensure_admin(auth_session)
    idem = await reserve_mutation_idempotency(
        db, actor_user_id=auth_session.user.id,
        scope=f"admin.tournament_test.delete:{fixture_id}",
        key=request_idempotency_key(request),
        request_fingerprint=mutation_payload_fingerprint(payload.model_dump(mode="json")),
    )
    if idem is not None and idem.replay:
        return Response(status_code=204)
    fixture, tournament = await _fixture_or_404(db, fixture_id, lock=True)
    if payload.confirmation_name.strip() != tournament.name:
        raise HTTPException(status_code=422, detail="Введите точное название тестового турнира.")
    fake_ids = list((await db.scalars(select(AdminTournamentTestPlayer.user_id).where(
        AdminTournamentTestPlayer.fixture_id == fixture.id
    ))).all())
    if fake_ids:
        locked_fake_users = (await db.scalars(
            select(User).where(User.id.in_(fake_ids)).order_by(User.id).with_for_update()
        )).all()
        if len(locked_fake_users) != len(fake_ids):
            raise HTTPException(status_code=409, detail={"code": "fake_user_ownership_incomplete"})
    external_reference = await fake_users_have_external_references(db, fake_ids, tournament.id)
    if external_reference:
        raise HTTPException(status_code=409, detail={"code": "fake_users_have_external_references"})
    tournament_id, slug = tournament.id, tournament.slug
    cover_key = object_key_from_upload_url(tournament.cover_url)
    try:
        await purge_deleted_media_metadata(db, tournament_ids=(tournament_id,))
    except MediaCleanupRequired as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail={
            "code": "tournament_media_cleanup_required",
            "message": "Очистите медиа тестового турнира перед удалением.",
            "statuses": exc.status_counts,
        }) from exc
    await write_audit_log(db, actor_user_id=auth_session.user.id, action="admin.tournament_test.delete",
                          subject_type="admin_tournament_test_fixture", subject_id=fixture.id,
                          payload={"fake_players_deleted": len(fake_ids)})
    bind_mutation_idempotency_resource(idem, fixture.id)
    await db.delete(tournament)
    await db.flush()
    if fake_ids:
        await db.execute(delete(User).where(User.id.in_(fake_ids)))
    await db.commit()
    for user_id in fake_ids:
        await delete_user_account_read_model(user_id)
    await delete_tournament_read_models(tournament_id, ("teams", "workspace_detail", "bracket_summary", "bracket_full"))
    await delete_tournament_profile_access_state(slug)
    invalidate_tournament_runtime_caches(tournament_id)
    if cover_key is not None:
        await run_in_threadpool(get_object_storage().delete, cover_key)
    return Response(status_code=204)
