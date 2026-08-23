from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one exact match, found {count}")
    return text.replace(old, new, 1)


# 1. Authentication session touch must never commit the request transaction.
SECURITY = "platform/python_packages/platform_infra/security.py"
security = read(SECURITY)
security = replace_once(
    security,
    "from sqlalchemy import select, update\n",
    "from sqlalchemy import or_, select, update\n",
    label="security sqlalchemy imports",
)
security = replace_once(
    security,
    "from python_packages.platform_infra.db import get_db_session\n",
    "from python_packages.platform_infra.db import get_db_session, session_factory\n",
    label="security session factory import",
)
security = replace_once(
    security,
    '''async def _touch_authenticated_session(\n    db_session: AsyncSession,\n    auth_session: AuthenticatedSession,\n) -> None:\n    settings = get_settings()\n    touch_before = auth_session.now - timedelta(\n        seconds=settings.platform_session_touch_interval_seconds\n    )\n    last_seen_at = auth_session.session.last_seen_at\n    if last_seen_at is not None and last_seen_at > touch_before:\n        return\n    await db_session.execute(\n        update(UserSession)\n        .where(\n            UserSession.id == auth_session.session.id,\n            UserSession.invalidated_at.is_(None),\n        )\n        .values(last_seen_at=auth_session.now)\n    )\n    await db_session.commit()\n    auth_session.session.last_seen_at = auth_session.now\n''',
    '''async def _touch_authenticated_session(\n    auth_session: AuthenticatedSession,\n) -> None:\n    """Persist last-seen metadata without committing the caller's transaction.\n\n    Authentication is a dependency of mutation serializers.  A session touch\n    therefore must use its own short transaction so a metadata write can never\n    release Tournament/Invite locks already owned by the request session.\n    """\n\n    settings = get_settings()\n    touch_before = auth_session.now - timedelta(\n        seconds=settings.platform_session_touch_interval_seconds\n    )\n    last_seen_at = auth_session.session.last_seen_at\n    if last_seen_at is not None and last_seen_at > touch_before:\n        return\n\n    factory = session_factory()\n    async with factory() as touch_session:\n        result = await touch_session.execute(\n            update(UserSession)\n            .where(\n                UserSession.id == auth_session.session.id,\n                UserSession.invalidated_at.is_(None),\n                or_(\n                    UserSession.last_seen_at.is_(None),\n                    UserSession.last_seen_at <= touch_before,\n                ),\n            )\n            .values(last_seen_at=auth_session.now)\n        )\n        await touch_session.commit()\n    if int(result.rowcount or 0) > 0:\n        auth_session.session.last_seen_at = auth_session.now\n''',
    label="isolated auth session touch",
)
security = security.replace(
    "await _touch_authenticated_session(db_session, auth_session)",
    "await _touch_authenticated_session(auth_session)",
)
if security.count("await _touch_authenticated_session(auth_session)") != 2:
    raise RuntimeError("security touch call sites: expected two updated calls")
write(SECURITY, security)


# 2. Stable profile-owner locking for lost-update and first-create races.
PROFILES = "platform/apps/platform_api/app/api/routes/profiles.py"
profiles = read(PROFILES)
profiles = replace_once(
    profiles,
    '''async def profile_media_descriptors(\n''',
    '''async def lock_profile_owner(db_session: AsyncSession, user_id: str) -> None:\n    locked_user_id = await db_session.scalar(\n        select(User.id).where(User.id == user_id).with_for_update()\n    )\n    if locked_user_id is None:\n        raise HTTPException(\n            status_code=status.HTTP_404_NOT_FOUND,\n            detail="Profile owner not found.",\n        )\n\n\nasync def profile_media_descriptors(\n''',
    label="profile owner lock helper",
)
profiles = replace_once(
    profiles,
    '''    profile = await db_session.scalar(\n        select(PlayerProfile).where(PlayerProfile.user_id == auth_session.user.id)\n    )\n    fields_set = payload.model_fields_set\n''',
    '''    await lock_profile_owner(db_session, auth_session.user.id)\n    profile = await db_session.scalar(\n        select(PlayerProfile)\n        .where(PlayerProfile.user_id == auth_session.user.id)\n        .execution_options(populate_existing=True)\n    )\n    if profile is None:\n        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")\n    fields_set = payload.model_fields_set\n''',
    label="player profile lock",
)
profiles = replace_once(
    profiles,
    '''    profile = await db_session.scalar(\n        select(DeadlockProfile).where(DeadlockProfile.user_id == auth_session.user.id)\n    )\n    if profile is None:\n''',
    '''    await lock_profile_owner(db_session, auth_session.user.id)\n    profile = await db_session.scalar(\n        select(DeadlockProfile)\n        .where(DeadlockProfile.user_id == auth_session.user.id)\n        .execution_options(populate_existing=True)\n    )\n    if profile is None:\n''',
    label="deadlock profile lock",
)
profiles = replace_once(
    profiles,
    '''    await db_session.scalar(\n        select(User)\n        .where(User.id == auth_session.user.id)\n        .with_for_update()\n    )\n    await db_session.execute(\n        delete(DeadlockDreamSlot).where(DeadlockDreamSlot.user_id == auth_session.user.id)\n    )\n''',
    '''    await lock_profile_owner(db_session, auth_session.user.id)\n    await db_session.execute(\n        delete(DeadlockDreamSlot).where(DeadlockDreamSlot.user_id == auth_session.user.id)\n    )\n''',
    label="dream slot owner lock helper reuse",
)
write(PROFILES, profiles)


# 3. Player commitment reconciliation must join the Tournament lock hierarchy.
COMMITMENTS = "platform/apps/platform_api/app/services/player_commitments.py"
commitments = read(COMMITMENTS)
commitments = replace_once(
    commitments,
    '''async def reconcile_player_commitments(\n    db_session: AsyncSession,\n    *,\n    now: datetime,\n) -> CommitmentReconciliationResult:\n    terminal_result = await db_session.execute(\n''',
    '''async def lock_active_commitment_tournaments(\n    db_session: AsyncSession,\n) -> tuple[str, ...]:\n    tournament_ids = tuple(\n        sorted(\n            {\n                str(tournament_id)\n                for tournament_id in (\n                    await db_session.scalars(\n                        select(PlayerTournamentCommitment.tournament_id).where(\n                            PlayerTournamentCommitment.released_at.is_(None)\n                        )\n                    )\n                ).all()\n            }\n        )\n    )\n    if not tournament_ids:\n        return ()\n    locked_ids = (\n        await db_session.scalars(\n            select(Tournament.id)\n            .where(Tournament.id.in_(tournament_ids))\n            .order_by(Tournament.id.asc())\n            .with_for_update()\n        )\n    ).all()\n    return tuple(str(tournament_id) for tournament_id in locked_ids)\n\n\nasync def reconcile_player_commitments(\n    db_session: AsyncSession,\n    *,\n    now: datetime,\n) -> CommitmentReconciliationResult:\n    # Reconciliation is a workflow writer.  Lock every affected stable parent\n    # row first, in deterministic id order, before deriving release decisions.\n    await lock_active_commitment_tournaments(db_session)\n\n    terminal_result = await db_session.execute(\n''',
    label="commitment parent locking",
)
write(COMMITMENTS, commitments)


# 4. Automation failure-state writes must reacquire the parent lock after rollback.
AUTOMATION = "platform/apps/platform_api/app/services/deadlock_automation.py"
automation = read(AUTOMATION)
automation = replace_once(
    automation,
    '''async def run_deadlock_automation_once(now: datetime | None = None) -> dict[str, int]:\n''',
    '''async def _lock_tournament_for_failure_state(\n    db_session: AsyncSession,\n    tournament_id: str,\n) -> Tournament | None:\n    try:\n        return await lock_tournament_for_workflow(db_session, tournament_id)\n    except TournamentWorkflowError:\n        return None\n\n\nasync def run_deadlock_automation_once(now: datetime | None = None) -> dict[str, int]:\n''',
    label="automation failure lock helper",
)
automation = automation.replace(
    "fresh_tournament = await db_session.scalar(select(Tournament).where(Tournament.id == tournament_id))",
    "fresh_tournament = await _lock_tournament_for_failure_state(db_session, tournament_id)",
)
if automation.count("await _lock_tournament_for_failure_state(db_session, tournament_id)") != 2:
    raise RuntimeError("automation failure paths: expected two locked reloads")
write(AUTOMATION, automation)


# 5. ORM constraints and durable API idempotency records.
MODELS = "platform/python_packages/platform_infra/models.py"
models = read(MODELS)
models = replace_once(
    models,
    '''class PasswordResetToken(Base):\n''',
    '''class ApiMutationIdempotencyKey(TimestampMixin, Base):\n    __tablename__ = "api_mutation_idempotency_keys"\n    __table_args__ = (\n        UniqueConstraint(\n            "actor_user_id",\n            "scope",\n            "key",\n            name="uq_api_mutation_idempotency_keys_actor_scope_key",\n        ),\n        CheckConstraint("length(request_fingerprint) = 64", name="request_fingerprint_length"),\n    )\n\n    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)\n    actor_user_id: Mapped[str] = mapped_column(\n        String(36),\n        ForeignKey("platform.users.id", ondelete="CASCADE"),\n        index=True,\n    )\n    scope: Mapped[str] = mapped_column(String(200))\n    key: Mapped[str] = mapped_column(String(128))\n    request_fingerprint: Mapped[str] = mapped_column(String(64))\n    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)\n\n\nclass PasswordResetToken(Base):\n''',
    label="idempotency ORM model",
)
models = replace_once(
    models,
    '''        UniqueConstraint(\n            "tournament_id",\n            "user_id",\n            name="uq_tournament_participants_tournament_user",\n        ),\n        Index(\n''',
    '''        UniqueConstraint(\n            "tournament_id",\n            "user_id",\n            name="uq_tournament_participants_tournament_user",\n        ),\n        CheckConstraint("entry_type = 'solo'", name="entry_type_solo"),\n        CheckConstraint(\n            "status IN ('registered', 'confirmed', 'checked_in', 'withdrawn', 'disqualified')",\n            name="status_allowed",\n        ),\n        Index(\n''',
    label="participant constraints",
)
models = replace_once(
    models,
    '''        UniqueConstraint(\n            "tournament_id",\n            "round_number",\n            "sequence_number",\n            name="uq_tournament_matches_tournament_round_sequence",\n        ),\n    )\n''',
    '''        UniqueConstraint(\n            "tournament_id",\n            "round_number",\n            "sequence_number",\n            name="uq_tournament_matches_tournament_round_sequence",\n        ),\n        CheckConstraint("round_number > 0", name="round_number_positive"),\n        CheckConstraint("sequence_number > 0", name="sequence_number_positive"),\n        CheckConstraint(\n            "status IN ('scheduled', 'live', 'completed', 'cancelled')",\n            name="status_allowed",\n        ),\n        CheckConstraint(\n            "winner_side IS NULL OR winner_side IN ('home', 'away')",\n            name="winner_side_allowed",\n        ),\n        CheckConstraint(\n            "home_score IS NULL OR home_score >= 0",\n            name="home_score_nonnegative",\n        ),\n        CheckConstraint(\n            "away_score IS NULL OR away_score >= 0",\n            name="away_score_nonnegative",\n        ),\n        CheckConstraint(\n            "status <> 'completed' OR "\n            "(home_score IS NOT NULL AND away_score IS NOT NULL "\n            "AND home_score <> away_score AND winner_side IS NOT NULL)",\n            name="completed_result_consistent",\n        ),\n    )\n''',
    label="match constraints",
)
models = replace_once(
    models,
    '''class TournamentDeadlockReadyVoteCountShard(TimestampMixin, Base):\n    __tablename__ = "tournament_deadlock_ready_vote_count_shards"\n\n''',
    '''class TournamentDeadlockReadyVoteCountShard(TimestampMixin, Base):\n    __tablename__ = "tournament_deadlock_ready_vote_count_shards"\n    __table_args__ = (\n        CheckConstraint("choice IN ('yes', 'no')", name="choice_allowed"),\n        CheckConstraint("shard >= 0", name="shard_nonnegative"),\n        CheckConstraint("vote_count >= 0", name="vote_count_nonnegative"),\n    )\n\n''',
    label="ready vote shard constraints",
)
write(MODELS, models)


# 6. Request-level idempotency service.
write(
    "platform/apps/platform_api/app/services/mutation_idempotency.py",
    '''from __future__ import annotations\n\nfrom dataclasses import dataclass\nfrom hashlib import sha256\nimport json\nfrom typing import Any\n\nfrom fastapi import HTTPException, Request, status\nfrom sqlalchemy import select\nfrom sqlalchemy.dialects import postgresql\nfrom sqlalchemy.ext.asyncio import AsyncSession\n\nfrom python_packages.platform_infra.models import (\n    ApiMutationIdempotencyKey,\n    new_uuid,\n)\n\n\n@dataclass(frozen=True, slots=True)\nclass MutationIdempotencyReservation:\n    record: ApiMutationIdempotencyKey\n    replay: bool\n\n\ndef request_idempotency_key(request: Request) -> str | None:\n    raw_key = request.headers.get("Idempotency-Key")\n    if raw_key is None:\n        return None\n    key = raw_key.strip()\n    if (\n        not key\n        or len(key) > 128\n        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in key)\n    ):\n        raise HTTPException(\n            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,\n            detail="Idempotency-Key must contain 1..128 visible ASCII characters.",\n        )\n    return key\n\n\ndef mutation_payload_fingerprint(payload: Any) -> str:\n    canonical = json.dumps(\n        payload,\n        ensure_ascii=False,\n        sort_keys=True,\n        separators=(",", ":"),\n        default=str,\n    )\n    return sha256(canonical.encode("utf-8")).hexdigest()\n\n\nasync def reserve_mutation_idempotency(\n    db_session: AsyncSession,\n    *,\n    actor_user_id: str,\n    scope: str,\n    key: str | None,\n    request_fingerprint: str,\n) -> MutationIdempotencyReservation | None:\n    if key is None:\n        return None\n\n    record_id = new_uuid()\n    inserted_id = await db_session.scalar(\n        postgresql.insert(ApiMutationIdempotencyKey)\n        .values(\n            id=record_id,\n            actor_user_id=actor_user_id,\n            scope=scope,\n            key=key,\n            request_fingerprint=request_fingerprint,\n        )\n        .on_conflict_do_nothing(\n            constraint="uq_api_mutation_idempotency_keys_actor_scope_key"\n        )\n        .returning(ApiMutationIdempotencyKey.id)\n    )\n    record = await db_session.scalar(\n        select(ApiMutationIdempotencyKey)\n        .where(\n            ApiMutationIdempotencyKey.actor_user_id == actor_user_id,\n            ApiMutationIdempotencyKey.scope == scope,\n            ApiMutationIdempotencyKey.key == key,\n        )\n        .with_for_update()\n    )\n    if record is None:\n        raise RuntimeError("Idempotency reservation disappeared during the transaction.")\n    if record.request_fingerprint != request_fingerprint:\n        raise HTTPException(\n            status_code=status.HTTP_409_CONFLICT,\n            detail="Idempotency-Key was already used with a different request payload.",\n        )\n    return MutationIdempotencyReservation(\n        record=record,\n        replay=inserted_id is None,\n    )\n\n\ndef bind_mutation_idempotency_resource(\n    reservation: MutationIdempotencyReservation | None,\n    resource_id: str,\n) -> None:\n    if reservation is None:\n        return\n    existing_resource_id = reservation.record.resource_id\n    if existing_resource_id is not None and existing_resource_id != resource_id:\n        raise RuntimeError("Idempotency reservation is already bound to another resource.")\n    reservation.record.resource_id = resource_id\n''',
)


# 7. Protect resource-creating tournament mutations against client retries and
#    retain disqualification records on self-leave.
TOURNAMENTS = "platform/apps/platform_api/app/api/routes/tournaments.py"
tournaments = read(TOURNAMENTS)
tournaments = replace_once(
    tournaments,
    '''from apps.platform_api.app.services.player_commitments import (\n    PlayerCommitmentConflict,\n    reactivate_team_commitments,\n    release_active_commitments,\n)\n''',
    '''from apps.platform_api.app.services.player_commitments import (\n    PlayerCommitmentConflict,\n    reactivate_team_commitments,\n    release_active_commitments,\n)\nfrom apps.platform_api.app.services.mutation_idempotency import (\n    bind_mutation_idempotency_resource,\n    mutation_payload_fingerprint,\n    request_idempotency_key,\n    reserve_mutation_idempotency,\n)\n''',
    label="tournament idempotency imports",
)
tournaments = replace_once(
    tournaments,
    '''async def create_tournament(\n    payload: TournamentCreateRequest,\n    auth_session=Depends(get_authenticated_session),\n''',
    '''async def create_tournament(\n    payload: TournamentCreateRequest,\n    request: Request,\n    auth_session=Depends(get_authenticated_session),\n''',
    label="create tournament request param",
)
tournaments = replace_once(
    tournaments,
    '''    ensure_tournament_schedule_is_future(payload, now=auth_session.now)\n    normalized_name = await lock_tournament_name(db_session, name=payload.name)\n''',
    '''    ensure_tournament_schedule_is_future(payload, now=auth_session.now)\n    idempotency = await reserve_mutation_idempotency(\n        db_session,\n        actor_user_id=auth_session.user.id,\n        scope="tournament.create",\n        key=request_idempotency_key(request),\n        request_fingerprint=mutation_payload_fingerprint(\n            payload.model_dump(mode="json")\n        ),\n    )\n    if idempotency is not None and idempotency.replay:\n        resource_id = idempotency.record.resource_id\n        if resource_id is None:\n            raise HTTPException(\n                status_code=status.HTTP_409_CONFLICT,\n                detail="The idempotent request completed without a resource reference.",\n            )\n        existing_tournament = await db_session.scalar(\n            select(Tournament).where(Tournament.id == resource_id)\n        )\n        if existing_tournament is None:\n            raise HTTPException(\n                status_code=status.HTTP_409_CONFLICT,\n                detail="The resource for this Idempotency-Key no longer exists.",\n            )\n        return serialize_tournament(\n            existing_tournament,\n            auth_session.user.display_name,\n            await participant_count_for_tournament(\n                db_session, tournament_id=existing_tournament.id\n            ),\n        )\n\n    normalized_name = await lock_tournament_name(db_session, name=payload.name)\n''',
    label="create tournament reservation",
)
tournaments = replace_once(
    tournaments,
    '''    try:\n        await db_session.flush()\n    except IntegrityError as exc:\n        await db_session.rollback()\n        raise HTTPException(\n            status_code=status.HTTP_409_CONFLICT,\n            detail="Турнир с таким публичным названием уже существует.",\n        ) from exc\n    invite_code = normalize_invite_code(payload.invite_code or "")\n''',
    '''    try:\n        await db_session.flush()\n    except IntegrityError as exc:\n        await db_session.rollback()\n        raise HTTPException(\n            status_code=status.HTTP_409_CONFLICT,\n            detail="Турнир с таким публичным названием уже существует.",\n        ) from exc\n    bind_mutation_idempotency_resource(idempotency, tournament.id)\n    invite_code = normalize_invite_code(payload.invite_code or "")\n''',
    label="bind tournament idempotency",
)
tournaments = replace_once(
    tournaments,
    '''    await check_invite_rate_limit(\n        request,\n        user_id=auth_session.user.id,\n        operation="manage",\n    )\n    tournament = await get_tournament_or_404(db_session, slug)\n    ensure_tournament_organizer(auth_session, tournament)\n    try:\n''',
    '''    tournament = await get_tournament_or_404(db_session, slug)\n    ensure_tournament_organizer(auth_session, tournament)\n    idempotency = await reserve_mutation_idempotency(\n        db_session,\n        actor_user_id=auth_session.user.id,\n        scope=f"tournament.invite.create:{tournament.id}",\n        key=request_idempotency_key(request),\n        request_fingerprint=mutation_payload_fingerprint(\n            {\n                "tournament_id": tournament.id,\n                "payload": payload.model_dump(mode="json"),\n            }\n        ),\n    )\n    if idempotency is not None and idempotency.replay:\n        resource_id = idempotency.record.resource_id\n        if resource_id is None:\n            raise HTTPException(\n                status_code=status.HTTP_409_CONFLICT,\n                detail="The idempotent request completed without a resource reference.",\n            )\n        existing_invite = await db_session.scalar(\n            select(TournamentInvite).where(\n                TournamentInvite.id == resource_id,\n                TournamentInvite.tournament_id == tournament.id,\n            )\n        )\n        if existing_invite is None:\n            raise HTTPException(\n                status_code=status.HTTP_409_CONFLICT,\n                detail="The resource for this Idempotency-Key no longer exists.",\n            )\n        return serialize_invite(tournament, existing_invite, now=datetime.now(UTC))\n\n    await check_invite_rate_limit(\n        request,\n        user_id=auth_session.user.id,\n        operation="manage",\n    )\n    try:\n''',
    label="create invite reservation",
)
tournaments = replace_once(
    tournaments,
    '''    db_session.add(invite)\n    await db_session.flush()\n    await write_audit_log(\n        db_session,\n        actor_user_id=auth_session.user.id,\n        action="tournament.invite.create",\n''',
    '''    db_session.add(invite)\n    await db_session.flush()\n    bind_mutation_idempotency_resource(idempotency, invite.id)\n    await write_audit_log(\n        db_session,\n        actor_user_id=auth_session.user.id,\n        action="tournament.invite.create",\n''',
    label="bind invite idempotency",
)
tournaments = replace_once(
    tournaments,
    '''    if participant.status in {"confirmed", "checked_in"}:\n        raise HTTPException(\n            status_code=status.HTTP_409_CONFLICT,\n            detail="Confirmed participants cannot leave the tournament.",\n        )\n''',
    '''    if participant.status == "disqualified":\n        raise HTTPException(\n            status_code=status.HTTP_409_CONFLICT,\n            detail=(\n                "Disqualified participant records are retained until the organizer "\n                "explicitly restores the participant."\n            ),\n        )\n    if participant.status in {"confirmed", "checked_in"}:\n        raise HTTPException(\n            status_code=status.HTTP_409_CONFLICT,\n            detail="Confirmed participants cannot leave the tournament.",\n        )\n''',
    label="retain disqualified self leave",
)
write(TOURNAMENTS, tournaments)


# Allow browser clients to send Idempotency-Key in non-production CORS mode.
MAIN = "platform/apps/platform_api/app/main.py"
main = read(MAIN)
main = replace_once(
    main,
    '''            allow_headers=["Content-Type", "X-CSRF-Token", "X-Platform-QA-Phase"],\n''',
    '''            allow_headers=[\n                "Content-Type",\n                "Idempotency-Key",\n                "X-CSRF-Token",\n                "X-Platform-QA-Phase",\n            ],\n''',
    label="idempotency CORS header",
)
write(MAIN, main)


# 8. Historical migration must fail closed instead of deleting unrelated tournaments.
MIGRATION_0011 = "platform/alembic/versions/20260429_0011_drop_tournament_dream_slots.py"
migration_0011 = read(MIGRATION_0011)
migration_0011 = replace_once(
    migration_0011,
    '''def upgrade() -> None:\n    op.execute(\n        """\n        DELETE FROM platform.tournaments\n        WHERE format_slug <> 'solo_balanced_deadlock'\n        """\n    )\n    op.drop_index(\n''',
    '''def upgrade() -> None:\n    legacy_tournament_count = int(\n        op.get_bind().scalar(\n            sa.text(\n                """\n                SELECT count(*)\n                FROM platform.tournaments\n                WHERE format_slug <> 'solo_balanced_deadlock'\n                """\n            )\n        )\n        or 0\n    )\n    if legacy_tournament_count:\n        raise RuntimeError(\n            "Cannot apply 20260429_0011 safely: found "\n            f"{legacy_tournament_count} non-solo tournament(s). "\n            "Migrate or archive those rows explicitly before retrying; "\n            "this migration will not delete tournament data."\n        )\n    op.drop_index(\n''',
    label="historical migration fail closed",
)
write(MIGRATION_0011, migration_0011)


# 9. New migration for domain/relational guards and idempotency storage.
write(
    "platform/alembic/versions/20260823_0041_persistence_concurrency_hardening.py",
    '''"""Harden persistence domains, relations and mutation idempotency.\n\nRevision ID: 20260823_0041\nRevises: 20260822_0040\nCreate Date: 2026-08-23\n"""\nfrom __future__ import annotations\n\nfrom alembic import op\nimport sqlalchemy as sa\n\n\nrevision = "20260823_0041"\ndown_revision = "20260822_0040"\nbranch_labels = None\ndepends_on = None\n\n\n_DATA_CHECKS: tuple[tuple[str, str], ...] = (\n    (\n        "invalid tournament participant state",\n        """\n        SELECT count(*) FROM platform.tournament_participants\n        WHERE entry_type <> 'solo'\n           OR status NOT IN ('registered', 'confirmed', 'checked_in', 'withdrawn', 'disqualified')\n        """,\n    ),\n    (\n        "invalid tournament match state",\n        """\n        SELECT count(*) FROM platform.tournament_matches\n        WHERE round_number <= 0\n           OR sequence_number <= 0\n           OR status NOT IN ('scheduled', 'live', 'completed', 'cancelled')\n           OR (winner_side IS NOT NULL AND winner_side NOT IN ('home', 'away'))\n           OR (home_score IS NOT NULL AND home_score < 0)\n           OR (away_score IS NOT NULL AND away_score < 0)\n           OR (status = 'completed' AND (\n                home_score IS NULL OR away_score IS NULL OR home_score = away_score OR winner_side IS NULL\n           ))\n        """,\n    ),\n    (\n        "invalid ready-vote count shard",\n        """\n        SELECT count(*) FROM platform.tournament_deadlock_ready_vote_count_shards\n        WHERE choice NOT IN ('yes', 'no') OR shard < 0 OR vote_count < 0\n        """,\n    ),\n    (\n        "invite access references an invite from another tournament",\n        """\n        SELECT count(*)\n        FROM platform.tournament_invite_accesses access\n        JOIN platform.tournament_invites invite ON invite.id = access.invite_id\n        WHERE access.invite_id IS NOT NULL\n          AND invite.tournament_id <> access.tournament_id\n        """,\n    ),\n)\n\n\ndef _assert_data_invariants() -> None:\n    bind = op.get_bind()\n    for label, statement in _DATA_CHECKS:\n        invalid_count = int(bind.scalar(sa.text(statement)) or 0)\n        if invalid_count:\n            raise RuntimeError(\n                "Cannot apply 20260823_0041: found "\n                f"{invalid_count} row(s) with {label}. Repair the data before retrying."\n            )\n\n\ndef upgrade() -> None:\n    _assert_data_invariants()\n\n    op.create_table(\n        "api_mutation_idempotency_keys",\n        sa.Column("id", sa.String(length=36), nullable=False),\n        sa.Column("actor_user_id", sa.String(length=36), nullable=False),\n        sa.Column("scope", sa.String(length=200), nullable=False),\n        sa.Column("key", sa.String(length=128), nullable=False),\n        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),\n        sa.Column("resource_id", sa.String(length=64), nullable=True),\n        sa.Column(\n            "created_at",\n            sa.DateTime(timezone=True),\n            nullable=False,\n            server_default=sa.func.now(),\n        ),\n        sa.Column(\n            "updated_at",\n            sa.DateTime(timezone=True),\n            nullable=False,\n            server_default=sa.func.now(),\n        ),\n        sa.CheckConstraint(\n            "length(request_fingerprint) = 64",\n            name="ck_api_mutation_idempotency_keys_request_fingerprint_length",\n        ),\n        sa.ForeignKeyConstraint(\n            ["actor_user_id"],\n            ["platform.users.id"],\n            name="fk_api_mutation_idempotency_keys_actor_user_id_users",\n            ondelete="CASCADE",\n        ),\n        sa.PrimaryKeyConstraint(\n            "id", name="pk_api_mutation_idempotency_keys"\n        ),\n        sa.UniqueConstraint(\n            "actor_user_id",\n            "scope",\n            "key",\n            name="uq_api_mutation_idempotency_keys_actor_scope_key",\n        ),\n        schema="platform",\n    )\n    op.create_index(\n        "ix_api_mutation_idempotency_keys_actor_user_id",\n        "api_mutation_idempotency_keys",\n        ["actor_user_id"],\n        unique=False,\n        schema="platform",\n    )\n\n    for table_name, constraint_name, condition in (\n        (\n            "tournament_participants",\n            "entry_type_solo",\n            "entry_type = 'solo'",\n        ),\n        (\n            "tournament_participants",\n            "status_allowed",\n            "status IN ('registered', 'confirmed', 'checked_in', 'withdrawn', 'disqualified')",\n        ),\n        ("tournament_matches", "round_number_positive", "round_number > 0"),\n        ("tournament_matches", "sequence_number_positive", "sequence_number > 0"),\n        (\n            "tournament_matches",\n            "status_allowed",\n            "status IN ('scheduled', 'live', 'completed', 'cancelled')",\n        ),\n        (\n            "tournament_matches",\n            "winner_side_allowed",\n            "winner_side IS NULL OR winner_side IN ('home', 'away')",\n        ),\n        (\n            "tournament_matches",\n            "home_score_nonnegative",\n            "home_score IS NULL OR home_score >= 0",\n        ),\n        (\n            "tournament_matches",\n            "away_score_nonnegative",\n            "away_score IS NULL OR away_score >= 0",\n        ),\n        (\n            "tournament_matches",\n            "completed_result_consistent",\n            "status <> 'completed' OR (home_score IS NOT NULL AND away_score IS NOT NULL "\n            "AND home_score <> away_score AND winner_side IS NOT NULL)",\n        ),\n        (\n            "tournament_deadlock_ready_vote_count_shards",\n            "choice_allowed",\n            "choice IN ('yes', 'no')",\n        ),\n        (\n            "tournament_deadlock_ready_vote_count_shards",\n            "shard_nonnegative",\n            "shard >= 0",\n        ),\n        (\n            "tournament_deadlock_ready_vote_count_shards",\n            "vote_count_nonnegative",\n            "vote_count >= 0",\n        ),\n    ):\n        op.create_check_constraint(\n            constraint_name, table_name, condition, schema="platform"\n        )\n\n    op.execute(\n        """\n        CREATE OR REPLACE FUNCTION platform.enforce_invite_access_tournament()\n        RETURNS trigger\n        LANGUAGE plpgsql\n        AS $$\n        BEGIN\n            IF NEW.invite_id IS NOT NULL AND NOT EXISTS (\n                SELECT 1\n                FROM platform.tournament_invites invite\n                WHERE invite.id = NEW.invite_id\n                  AND invite.tournament_id = NEW.tournament_id\n            ) THEN\n                RAISE EXCEPTION 'invite access tournament does not match invite tournament'\n                    USING ERRCODE = '23514',\n                          CONSTRAINT = 'ck_tournament_invite_accesses_invite_tournament';\n            END IF;\n            RETURN NEW;\n        END;\n        $$\n        """\n    )\n    op.execute(\n        """\n        CREATE TRIGGER trg_tournament_invite_accesses_invite_tournament\n        BEFORE INSERT OR UPDATE OF tournament_id, invite_id\n        ON platform.tournament_invite_accesses\n        FOR EACH ROW\n        EXECUTE FUNCTION platform.enforce_invite_access_tournament()\n        """\n    )\n\n    op.execute(\n        """\n        CREATE OR REPLACE FUNCTION platform.enforce_ready_vote_active_participant()\n        RETURNS trigger\n        LANGUAGE plpgsql\n        AS $$\n        DECLARE\n            vote_tournament_id varchar(36);\n        BEGIN\n            SELECT ready_round.tournament_id\n            INTO vote_tournament_id\n            FROM platform.tournament_deadlock_ready_rounds ready_round\n            WHERE ready_round.id = NEW.round_id;\n\n            IF vote_tournament_id IS NULL OR NOT EXISTS (\n                SELECT 1\n                FROM platform.tournament_participants participant\n                WHERE participant.tournament_id = vote_tournament_id\n                  AND participant.user_id = NEW.user_id\n                  AND participant.status NOT IN ('withdrawn', 'disqualified')\n            ) THEN\n                RAISE EXCEPTION 'ready vote requires active tournament participation'\n                    USING ERRCODE = '23514',\n                          CONSTRAINT = 'ck_tournament_deadlock_ready_votes_active_participant';\n            END IF;\n            RETURN NEW;\n        END;\n        $$\n        """\n    )\n    op.execute(\n        """\n        CREATE TRIGGER trg_tournament_deadlock_ready_votes_active_participant\n        BEFORE INSERT OR UPDATE OF round_id, user_id\n        ON platform.tournament_deadlock_ready_votes\n        FOR EACH ROW\n        EXECUTE FUNCTION platform.enforce_ready_vote_active_participant()\n        """\n    )\n\n\ndef downgrade() -> None:\n    op.execute(\n        "DROP TRIGGER IF EXISTS trg_tournament_deadlock_ready_votes_active_participant "\n        "ON platform.tournament_deadlock_ready_votes"\n    )\n    op.execute(\n        "DROP FUNCTION IF EXISTS platform.enforce_ready_vote_active_participant()"\n    )\n    op.execute(\n        "DROP TRIGGER IF EXISTS trg_tournament_invite_accesses_invite_tournament "\n        "ON platform.tournament_invite_accesses"\n    )\n    op.execute(\n        "DROP FUNCTION IF EXISTS platform.enforce_invite_access_tournament()"\n    )\n\n    for table_name, constraint_name in (\n        ("tournament_deadlock_ready_vote_count_shards", "vote_count_nonnegative"),\n        ("tournament_deadlock_ready_vote_count_shards", "shard_nonnegative"),\n        ("tournament_deadlock_ready_vote_count_shards", "choice_allowed"),\n        ("tournament_matches", "completed_result_consistent"),\n        ("tournament_matches", "away_score_nonnegative"),\n        ("tournament_matches", "home_score_nonnegative"),\n        ("tournament_matches", "winner_side_allowed"),\n        ("tournament_matches", "status_allowed"),\n        ("tournament_matches", "sequence_number_positive"),\n        ("tournament_matches", "round_number_positive"),\n        ("tournament_participants", "status_allowed"),\n        ("tournament_participants", "entry_type_solo"),\n    ):\n        op.drop_constraint(\n            f"ck_{table_name}_{constraint_name}",\n            table_name,\n            type_="check",\n            schema="platform",\n        )\n\n    op.drop_index(\n        "ix_api_mutation_idempotency_keys_actor_user_id",\n        table_name="api_mutation_idempotency_keys",\n        schema="platform",\n    )\n    op.drop_table("api_mutation_idempotency_keys", schema="platform")\n''',
)


# 10. Regression tests for the remediation contract.
write(
    "platform/tests/test_platform_persistence_concurrency_remediation.py",
    '''from __future__ import annotations\n\nfrom datetime import UTC, datetime, timedelta\nfrom pathlib import Path\nfrom types import SimpleNamespace\nimport unittest\nfrom unittest.mock import AsyncMock, Mock, patch\n\nfrom fastapi import HTTPException\n\nfrom apps.platform_api.app.api.routes import profiles, tournaments\nfrom apps.platform_api.app.services import deadlock_automation, player_commitments\nfrom apps.platform_api.app.services.mutation_idempotency import (\n    mutation_payload_fingerprint,\n    request_idempotency_key,\n)\nfrom python_packages.platform_infra import security\nfrom python_packages.platform_infra.models import (\n    TournamentDeadlockReadyVoteCountShard,\n    TournamentMatch,\n    TournamentParticipant,\n)\n\n\nclass _AsyncContext:\n    def __init__(self, value):\n        self.value = value\n\n    async def __aenter__(self):\n        return self.value\n\n    async def __aexit__(self, exc_type, exc, tb):\n        return False\n\n\nclass PersistenceConcurrencyRemediationTests(unittest.IsolatedAsyncioTestCase):\n    async def test_auth_touch_uses_isolated_session_transaction(self) -> None:\n        touch_session = Mock()\n        touch_session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=1))\n        touch_session.commit = AsyncMock()\n        factory = Mock(return_value=_AsyncContext(touch_session))\n        auth_session = SimpleNamespace(\n            now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),\n            session=SimpleNamespace(\n                id="session",\n                last_seen_at=datetime(2026, 8, 23, 11, 0, tzinfo=UTC),\n            ),\n        )\n        settings = SimpleNamespace(platform_session_touch_interval_seconds=300)\n        with (\n            patch.object(security, "get_settings", return_value=settings),\n            patch.object(security, "session_factory", return_value=factory),\n        ):\n            await security._touch_authenticated_session(auth_session)\n        touch_session.commit.assert_awaited_once()\n        self.assertEqual(auth_session.session.last_seen_at, auth_session.now)\n\n    async def test_profile_owner_lock_uses_for_update_query(self) -> None:\n        db_session = Mock()\n        db_session.scalar = AsyncMock(return_value="user")\n        await profiles.lock_profile_owner(db_session, "user")\n        statement = db_session.scalar.await_args.args[0]\n        self.assertIsNotNone(statement._for_update_arg)\n\n    async def test_commitment_reconciliation_locks_parent_tournaments_first(self) -> None:\n        db_session = Mock()\n        db_session.scalars = AsyncMock(\n            side_effect=[\n                SimpleNamespace(all=lambda: ["t2", "t1", "t1"]),\n                SimpleNamespace(all=lambda: ["t1", "t2"]),\n            ]\n        )\n        locked = await player_commitments.lock_active_commitment_tournaments(db_session)\n        self.assertEqual(locked, ("t1", "t2"))\n        lock_statement = db_session.scalars.await_args_list[1].args[0]\n        self.assertIsNotNone(lock_statement._for_update_arg)\n\n    async def test_automation_failure_reload_reacquires_workflow_lock(self) -> None:\n        tournament = SimpleNamespace(id="tournament")\n        with patch.object(\n            deadlock_automation,\n            "lock_tournament_for_workflow",\n            AsyncMock(return_value=tournament),\n        ) as locked:\n            result = await deadlock_automation._lock_tournament_for_failure_state(\n                Mock(), "tournament"\n            )\n        self.assertIs(result, tournament)\n        locked.assert_awaited_once()\n\n    def test_persistent_domain_constraints_are_present_in_metadata(self) -> None:\n        participant_constraints = {\n            constraint.name for constraint in TournamentParticipant.__table__.constraints\n        }\n        self.assertIn("ck_tournament_participants_entry_type_solo", participant_constraints)\n        self.assertIn("ck_tournament_participants_status_allowed", participant_constraints)\n\n        match_constraints = {\n            constraint.name for constraint in TournamentMatch.__table__.constraints\n        }\n        self.assertIn("ck_tournament_matches_status_allowed", match_constraints)\n        self.assertIn(\n            "ck_tournament_matches_completed_result_consistent", match_constraints\n        )\n\n        shard_constraints = {\n            constraint.name\n            for constraint in TournamentDeadlockReadyVoteCountShard.__table__.constraints\n        }\n        self.assertIn(\n            "ck_tournament_deadlock_ready_vote_count_shards_vote_count_nonnegative",\n            shard_constraints,\n        )\n\n    def test_idempotency_key_and_payload_fingerprint_are_stable(self) -> None:\n        request = SimpleNamespace(headers={"Idempotency-Key": "retry-123"})\n        self.assertEqual(request_idempotency_key(request), "retry-123")\n        self.assertEqual(\n            mutation_payload_fingerprint({"b": 2, "a": 1}),\n            mutation_payload_fingerprint({"a": 1, "b": 2}),\n        )\n\n    def test_disqualified_self_leave_has_explicit_retention_guard(self) -> None:\n        source = Path(tournaments.__file__).read_text(encoding="utf-8")\n        leave_block = source.split('async def leave_tournament(', 1)[1].split(\n            '@router.get("/{slug}/profiles/{user_id}"', 1\n        )[0]\n        self.assertIn('participant.status == "disqualified"', leave_block)\n        self.assertIn("retained until the organizer", leave_block)\n\n    def test_historical_migration_no_longer_deletes_tournaments(self) -> None:\n        migration = (\n            Path(__file__).resolve().parents[1]\n            / "alembic"\n            / "versions"\n            / "20260429_0011_drop_tournament_dream_slots.py"\n        ).read_text(encoding="utf-8")\n        self.assertNotIn("DELETE FROM platform.tournaments", migration)\n        self.assertIn("will not delete tournament data", migration)\n\n\nif __name__ == "__main__":\n    unittest.main()\n''',
)


# 11. Record the strengthened production invariants in the canonical runbook.
CURRENT = "platform/docs/CURRENT.md"
current = read(CURRENT)
current = replace_once(
    current,
    '''- Invite use and active-participant capacity are transaction-scoped PostgreSQL invariants: invite claim/revoke locks the stable tournament and invite rows in tournament-to-invite order; active-roster mutations serialize on the tournament row and capacity is rechecked before an inactive retained participant becomes active.\n''',
    '''- Invite use and active-participant capacity are transaction-scoped PostgreSQL invariants: invite claim/revoke locks the stable tournament and invite rows in tournament-to-invite order; active-roster mutations serialize on the tournament row and capacity is rechecked before an inactive retained participant becomes active. Authentication last-seen touches use an isolated database transaction and must never commit or release locks owned by a mutation request.\n- Resource-creating API retries use durable actor/scope `Idempotency-Key` records. A repeated key with the same payload resolves to the originally created tournament/invite; reusing a key with a different payload is rejected.\n- Player-commitment reconciliation is a tournament workflow writer: it locks every affected Tournament row in deterministic id order before reading lifecycle state or releasing commitments. Automation failure-state persistence reacquires the same Tournament lock after any rollback.\n''',
    label="CURRENT persistence invariants",
)
write(CURRENT, current)

print("persistence concurrency remediation applied")
