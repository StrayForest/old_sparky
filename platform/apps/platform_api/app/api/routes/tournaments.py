from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import logging
import secrets
import string
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from sqlalchemy import Select, and_, cast, delete, exists, func, or_, select, union
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from apps.platform_api.app.api.pagination import (
    PARTICIPANT_LIST_DEFAULT_LIMIT,
    PARTICIPANT_LIST_MAX_LIMIT,
    TOURNAMENT_LIST_DEFAULT_LIMIT,
    TOURNAMENT_LIST_MAX_LIMIT,
    set_pagination_headers,
)
from apps.platform_api.app.api.schemas import (
    TournamentDeadlockAutoAssignmentRunResponse,
    TournamentDeadlockAutoAssignmentJobResponse,
    TournamentDeadlockAutoAssignmentStateResponse,
    TournamentDeadlockCaptainEntryResponse,
    TournamentDeadlockCaptainPreviewResponse,
    TournamentDeadlockCaptainRoundResponse,
    TournamentDeadlockCaptainRoundStartRequest,
    TournamentDeadlockCaptainRoundStateResponse,
    TournamentDeadlockCaptainRoundRespondRequest,
    TournamentDeadlockReadyCheckStateResponse,
    TournamentDeadlockReadyRoundResponse,
    TournamentDeadlockReadyVoteRequest,
    TournamentDeadlockReadyVoteResponse,
    DeadlockDreamSlotResponse,
    DeadlockProfileResponse,
    MediaAcceptedResponse,
    MediaDeleteAcceptedResponse,
    MediaDescriptorResponse,
    TournamentCreateRequest,
    TournamentInviteClaimRequest,
    TournamentInviteCodeAvailabilityResponse,
    TournamentInviteCreateRequest,
    TournamentInviteRedeemResponse,
    TournamentInviteResponse,
    TournamentBracketMatchResponse,
    TournamentBracketCapabilitiesResponse,
    TournamentBracketResponse,
    TournamentBracketTeamMemberResponse,
    TournamentBracketTeamResponse,
    TournamentMatchCreateRequest,
    TournamentMatchReportRequest,
    TournamentMatchResponse,
    TournamentMatchScheduleUpdateRequest,
    TournamentMatchStatusUpdateRequest,
    TournamentParticipantJoinRequest,
    TournamentParticipantManageRequest,
    TournamentParticipantModerationRequest,
    TournamentParticipantResponse,
    PlayerTournamentCommitmentResponse,
    TournamentProfileStatsResponse,
    TournamentResponse,
    TournamentScopedProfileResponse,
    TournamentProfileResponse,
    TournamentStatusUpdateRequest,
    TournamentWorkspaceResponse,
)
from apps.platform_api.app.services.current_user import serialize_current_user
from apps.platform_api.app.services.media import (
    accepted_media_response,
    api_media_service,
    compatibility_media_url,
    enqueue_media_asset,
    load_media_descriptors,
    raise_media_http_error,
    upload_file_chunks,
    upload_size_hint,
)
from apps.platform_api.app.services.brackets import (
    advance_revision,
    clear_match_result_and_progression,
    create_full_bracket_graph,
    ensure_expected_revision,
    lock_tournament_for_bracket,
    propagate_match_winner,
)
from apps.platform_api.app.services.player_commitments import (
    PlayerCommitmentConflict,
    reactivate_team_commitments,
    release_active_commitments,
)
from apps.platform_api.app.services.mutation_idempotency import (
    bind_mutation_idempotency_resource,
    mutation_payload_fingerprint,
    request_idempotency_key,
    reserve_mutation_idempotency,
)
from apps.platform_api.app.services.tournament_allowances import (
    PRIVATE_TOURNAMENT_MONTHLY_LIMIT,
    private_tournament_monthly_remaining,
)
from apps.platform_api.app.services.tournament_workflow import (
    ReadyRoundStateSnapshot,
    TournamentCompletionError,
    TournamentStatusTransitionError,
    build_deadlock_ready_round_state_snapshot,
    complete_locked_tournament_after_final_match,
    deadlock_assignment_run_by_id_for_tournament,
    deadlock_auto_assignment_run_for_tournament,
    deadlock_auto_assignment_run_freshness,
    deadlock_auto_assignment_state_runs_for_tournament,
    deadlock_auto_assignment_stale_detail,
    deadlock_captain_entries_for_round,
    deadlock_captain_round_for_tournament,
    deadlock_closed_ready_round_for_tournament,
    deadlock_latest_auto_assignment_inputs_for_tournament,
    deadlock_locked_auto_assignment_run_for_tournament,
    deadlock_published_auto_assignment_run_for_tournament,
    supersede_published_deadlock_assignment_run_for_tournament,
    deadlock_ready_candidate_rows_for_round,
    deadlock_ready_check_read_preflight,
    deadlock_ready_round_for_tournament,
    deadlock_ready_state_round_for_tournament,
    finalize_deadlock_assignment_with_commitments,
    lock_tournament_for_workflow,
    mark_ready_check_closed,
    mark_ready_check_started,
    prepare_deadlock_ready_vote,
    prepare_deadlock_captain_candidate_rows,
    prune_participant_from_active_captain_round,
    prune_participant_from_active_ready_round,
    participant_status_is_inactive,
    serialize_deadlock_ready_round,
    tournament_has_locked_deadlock_roster,
    transition_tournament_status,
    upsert_deadlock_ready_vote,
)
from apps.platform_api.app.services.tournament_runtime_cache import (
    register_tournament_runtime_cache_invalidator,
)
from apps.platform_api.app.services.tournament_participant_capacity import (
    PARTICIPANT_SLOT_MATERIALIZATION_LIMIT,
    claim_participant_slot,
    claim_slot_for_existing_participant,
    has_free_participant_slot,
    release_participant_slot,
)
from python_packages.platform_domain.deadlock import (
    AutoAssignmentError,
    AutoAssignmentRunFreshness,
    AutoAssignmentRunWorkflowError,
    CaptainRoundState,
    DEFAULT_TEAM_COUNT_LIMIT,
    normalize_requested_teams_count,
    resolve_effective_teams_count,
    assign_captain_team_numbers,
    build_captain_preview,
    calculate_player_strength,
    captain_priority_bucket,
    transition_auto_assignment_run_status,
    prepare_ready_check_start,
    prepare_captain_round_entries,
)
from python_packages.platform_domain.tournaments import (
    ExistingBracketMatchState,
    SOLO_TOURNAMENT_FORMAT,
    TournamentWorkflowError,
    available_match_statuses,
    available_tournament_statuses,
    build_next_round_matches,
    can_view_tournament_summary,
    can_view_tournament_workspace,
    ensure_solo_entry,
    ensure_supported_tournament_format,
    ensure_tournament_rank_allows_join,
    ensure_organizer_can_moderate_participants,
    ensure_participant_restoration_allowed,
    ensure_match_team_ids_are_locked,
    ensure_invite_claimable,
    can_self_join_tournament,
    can_self_leave_tournament,
    eliminated_team_id_for_single_elimination,
    ensure_deadlock_match_staging_allowed,
    ensure_match_admin_actions_allowed,
    ensure_match_deletion_allowed,
    ensure_match_report_allowed,
    ensure_match_round_staging_allowed,
    ensure_match_schedule_allowed,
    ensure_deadlock_registration_changes_allowed,
    ensure_deadlock_roster_staging_allowed,
    ensure_organizer_can_manage_participants,
    invite_is_active,
    is_solo_tournament_format,
    normalize_tournament_allowed_ranks,
    remaining_invite_uses,
    resolve_match_report,
    strength_seed_teams,
    transition_participant_status,
    transition_match_status,
)
from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.invite_rate_limit import check_invite_rate_limit
from python_packages.platform_infra.media.errors import MediaError
from python_packages.platform_infra.media.source_store import StagedSource
from python_packages.platform_infra.media_rate_limit import check_media_upload_rate_limit
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
    TournamentInvite,
    TournamentInviteAccess,
    TournamentMatch,
    TournamentParticipant,
    TournamentParticipantSlot,
    User,
    new_uuid,
)
from python_packages.platform_infra.security import (
    get_authenticated_session,
    get_optional_authenticated_session,
)
from python_packages.platform_infra.tournament_names import (
    lock_tournament_name,
    public_tournament_name_exists,
)
from python_packages.platform_infra.slugs import unique_slug_from_name

router = APIRouter()

logger = logging.getLogger(__name__)

INVITE_CODE_ALPHABET = string.ascii_uppercase + string.digits
INACTIVE_PARTICIPANT_STATUSES = ("withdrawn", "disqualified")
DEFAULT_TOURNAMENT_COVER_URL = (
    "/assets/tournament-covers/tournament-cover-template-1-v1.webp"
)
PAGINATION_EXPOSE_HEADERS = "X-Total-Count, X-Limit, X-Offset, X-Has-More"
BRACKET_RESPONSE_CACHE_TTL_SECONDS = 30.0
BRACKET_RESPONSE_CACHE_MAX_ENTRIES = 128
READY_CHECK_STATE_CACHE_TTL_SECONDS = 30.0
READY_CHECK_STATE_CACHE_MAX_ENTRIES = 128
PARTICIPANT_PAGE_CACHE_TTL_SECONDS = 30.0
PARTICIPANT_PAGE_CACHE_MAX_ENTRIES = 512
PUBLIC_WORKSPACE_SNAPSHOT_CACHE_TTL_SECONDS = 2.0
PUBLIC_WORKSPACE_SNAPSHOT_CACHE_MAX_ENTRIES = 128
MANUAL_READY_CHECK_DURATION = timedelta(minutes=10)
TERMINAL_TOURNAMENT_STATUSES = frozenset(("completed", "cancelled"))


def bracket_capabilities(
    *,
    tournament_status: str,
    can_manage: bool,
) -> TournamentBracketCapabilitiesResponse:
    """Return action capabilities independently from the structural bracket state."""

    can_manage_matches = bool(can_manage and tournament_status not in TERMINAL_TOURNAMENT_STATUSES)
    return TournamentBracketCapabilitiesResponse(
        can_manage=can_manage_matches,
        can_schedule_matches=can_manage_matches,
        can_report_matches=bool(
            can_manage_matches
            and tournament_status in {"registration_closed", "in_progress"}
        ),
    )


def tournament_state_version(
    tournament: Tournament,
    *,
    participant_count: int = 0,
) -> int:
    updated_at = tournament.updated_at or tournament.created_at
    updated_ms = int(updated_at.timestamp() * 1000) if updated_at is not None else 0
    return (
        updated_ms
        + int(tournament.bracket_revision or 0) * 1_000_000
        + max(0, int(participant_count))
    )


def _representation_etag(*parts: object) -> str:
    fingerprint = "|".join(str(part) for part in parts)
    return f'"{sha256(fingerprint.encode("utf-8")).hexdigest()}"'


def _serialized_model_response(payload: Any, *, etag: str) -> Response:
    response = Response(
        content=payload.model_dump_json(),
        media_type="application/json",
    )
    response.headers["ETag"] = etag
    return response


def _workspace_response_etag(
    workspace_response: TournamentWorkspaceResponse,
    *,
    workspace_view: str,
    participants_limit: int,
    participants_offset: int,
    include_current_user: bool,
    user_id: str,
) -> str:
    ready_check = workspace_response.ready_check
    ready_round_id = None
    if ready_check is not None:
        active_round = ready_check.active_round or ready_check.latest_round
        ready_round_id = active_round.id if active_round is not None else None
    return _representation_etag(
        "workspace",
        workspace_response.tournament.id,
        workspace_response.tournament.state_version,
        workspace_view,
        participants_limit,
        participants_offset,
        include_current_user,
        user_id,
        workspace_response.bracket.revision if workspace_response.bracket is not None else None,
        ready_check.state_version if ready_check is not None else None,
        ready_round_id,
    )


def _etag_weak_value(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("W/"):
        return normalized[2:].strip()
    return normalized


def _conditional_response(
    request: Request,
    response: Response,
    *,
    etag: str,
) -> Response | None:
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["Vary"] = "Cookie, Accept-Encoding"
    raw_header = request.headers.get("if-none-match", "")
    if raw_header.strip() == "*" or _etag_weak_value(etag) in {
        _etag_weak_value(item) for item in raw_header.split(",") if item.strip()
    }:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={
                "ETag": etag,
                "Cache-Control": "private, no-cache",
                "Vary": "Cookie, Accept-Encoding",
            },
        )
    return None


def ready_check_state_version(
    active_round: TournamentDeadlockReadyRoundResponse | None,
    latest_round: TournamentDeadlockReadyRoundResponse | None,
) -> int:
    round_response = active_round or latest_round
    if round_response is None:
        return 0
    return (
        int(round_response.id) * 1_000_000
        + int(round_response.ready_count) * 1_000
        + int(round_response.declined_count)
    )


@dataclass(frozen=True, slots=True)
class BracketResponseCacheEntry:
    expires_at: float
    response: TournamentBracketResponse




@dataclass(frozen=True, slots=True)
class ReadyCheckStateCacheEntry:
    expires_at: float
    active_round: ReadyRoundStateSnapshot | None
    latest_round: ReadyRoundStateSnapshot | None


@dataclass(frozen=True, slots=True)
class ParticipantJoinPreflight:
    tournament: Tournament
    has_existing_participant: bool
    has_free_participant_slot: bool
    player_rank: str | None
    has_locked_deadlock_roster: bool
    has_invite_access: bool


@dataclass(frozen=True, slots=True)
class ParticipantPageCacheEntry:
    expires_at: float
    participants: list[TournamentParticipantResponse]
    total: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class PublicWorkspaceSnapshotCacheEntry:
    expires_at: float
    tournament_id: str
    tournament_updated_at: datetime | None
    response: TournamentWorkspaceResponse








_bracket_response_cache: dict[
    tuple[str, int, str, str, str, bool, bool],
    BracketResponseCacheEntry,
] = {}
_ready_check_state_cache: dict[tuple[str, int], ReadyCheckStateCacheEntry] = {}
_ready_check_state_cache_locks: dict[tuple[str, int], asyncio.Lock] = {}
_participant_page_cache: dict[
    tuple[str, str, int, int],
    ParticipantPageCacheEntry,
] = {}
_public_workspace_snapshot_cache: dict[str, PublicWorkspaceSnapshotCacheEntry] = {}


def _prune_expired_cache_entries(cache: dict[Any, Any], *, now: float) -> None:
    expired_keys = [
        key
        for key, entry in cache.items()
        if getattr(entry, "expires_at", 0.0) <= now
    ]
    for key in expired_keys:
        cache.pop(key, None)


def _trim_cache(cache: dict[Any, Any], *, max_entries: int, now: float) -> None:
    _prune_expired_cache_entries(cache, now=now)
    while len(cache) >= max_entries:
        oldest_key = next(iter(cache), None)
        if oldest_key is None:
            break
        cache.pop(oldest_key, None)


def _get_bracket_response_cache(
    key: tuple[str, int, str, str, str, bool, bool],
) -> TournamentBracketResponse | None:
    now = time.monotonic()
    entry = _bracket_response_cache.get(key)
    if entry is None:
        return None
    if entry.expires_at <= now:
        _bracket_response_cache.pop(key, None)
        return None
    return entry.response


def _set_bracket_response_cache(
    key: tuple[str, int, str, str, str, bool, bool],
    response: TournamentBracketResponse,
) -> None:
    now = time.monotonic()
    _trim_cache(_bracket_response_cache, max_entries=BRACKET_RESPONSE_CACHE_MAX_ENTRIES, now=now)
    _bracket_response_cache[key] = BracketResponseCacheEntry(
        expires_at=now + BRACKET_RESPONSE_CACHE_TTL_SECONDS,
        response=response,
    )


def _get_ready_check_state_cache(
    key: tuple[str, int],
) -> ReadyCheckStateCacheEntry | None:
    now = time.monotonic()
    entry = _ready_check_state_cache.get(key)
    if entry is None:
        return None
    if entry.expires_at <= now:
        _ready_check_state_cache.pop(key, None)
        _ready_check_state_cache_locks.pop(key, None)
        return None
    return entry


def _set_ready_check_state_cache(
    key: tuple[str, int],
    entry: ReadyCheckStateCacheEntry,
) -> None:
    now = time.monotonic()
    _trim_cache(
        _ready_check_state_cache,
        max_entries=READY_CHECK_STATE_CACHE_MAX_ENTRIES,
        now=now,
    )
    _ready_check_state_cache[key] = entry


def _ready_check_state_cache_lock(key: tuple[str, int]) -> asyncio.Lock:
    lock = _ready_check_state_cache_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ready_check_state_cache_locks[key] = lock
    return lock


def _get_participant_page_cache(
    key: tuple[str, str, int, int],
) -> tuple[list[TournamentParticipantResponse], int, bool] | None:
    now = time.monotonic()
    entry = _participant_page_cache.get(key)
    if entry is None:
        return None
    if entry.expires_at <= now:
        _participant_page_cache.pop(key, None)
        return None
    return entry.participants, entry.total, entry.has_more


def _set_participant_page_cache(
    key: tuple[str, str, int, int],
    participants: list[TournamentParticipantResponse],
    total: int,
    has_more: bool,
) -> None:
    now = time.monotonic()
    _trim_cache(
        _participant_page_cache,
        max_entries=PARTICIPANT_PAGE_CACHE_MAX_ENTRIES,
        now=now,
    )
    _participant_page_cache[key] = ParticipantPageCacheEntry(
        expires_at=now + PARTICIPANT_PAGE_CACHE_TTL_SECONDS,
        participants=participants,
        total=total,
        has_more=has_more,
    )


def _get_public_workspace_snapshot_cache(
    slug: str,
) -> PublicWorkspaceSnapshotCacheEntry | None:
    now = time.monotonic()
    entry = _public_workspace_snapshot_cache.get(slug)
    if entry is None:
        return None
    if entry.expires_at <= now:
        _public_workspace_snapshot_cache.pop(slug, None)
        return None
    return entry


def _set_public_workspace_snapshot_cache(
    slug: str,
    *,
    tournament_id: str,
    tournament_updated_at: datetime | None,
    response: TournamentWorkspaceResponse,
) -> None:
    now = time.monotonic()
    _trim_cache(
        _public_workspace_snapshot_cache,
        max_entries=PUBLIC_WORKSPACE_SNAPSHOT_CACHE_MAX_ENTRIES,
        now=now,
    )
    _public_workspace_snapshot_cache[slug] = PublicWorkspaceSnapshotCacheEntry(
        expires_at=now + PUBLIC_WORKSPACE_SNAPSHOT_CACHE_TTL_SECONDS,
        tournament_id=tournament_id,
        tournament_updated_at=tournament_updated_at,
        response=response,
    )


def _invalidate_participant_page_cache(tournament_id: str) -> None:
    keys = [
        key
        for key in _participant_page_cache
        if key[0] == tournament_id
    ]
    for key in keys:
        _participant_page_cache.pop(key, None)


def _invalidate_ready_check_state_cache(tournament_id: str) -> None:
    keys = [
        key
        for key in _ready_check_state_cache
        if key[0] == tournament_id
    ]
    for key in keys:
        _ready_check_state_cache.pop(key, None)
        _ready_check_state_cache_locks.pop(key, None)


def invalidate_tournament_runtime_caches(tournament_id: str) -> None:
    normalized_tournament_id = str(tournament_id)
    bracket_keys = [
        key for key in _bracket_response_cache if key[0] == normalized_tournament_id
    ]
    for key in bracket_keys:
        _bracket_response_cache.pop(key, None)
    _invalidate_participant_page_cache(tournament_id)
    _invalidate_ready_check_state_cache(tournament_id)
    snapshot_keys = [
        slug
        for slug, entry in _public_workspace_snapshot_cache.items()
        if entry.tournament_id == normalized_tournament_id
    ]
    for slug in snapshot_keys:
        _public_workspace_snapshot_cache.pop(slug, None)


register_tournament_runtime_cache_invalidator(invalidate_tournament_runtime_caches)


def _ready_round_snapshot_for_user(
    snapshot: ReadyRoundStateSnapshot | None,
    *,
    current_user_id: str,
) -> TournamentDeadlockReadyRoundResponse | None:
    if snapshot is None:
        return None
    return snapshot.response.model_copy(
        update={"current_user_choice": snapshot.choices_by_user_id.get(current_user_id)}
    )


def _ready_check_state_response_from_cache(
    entry: ReadyCheckStateCacheEntry,
    *,
    current_user_id: str,
) -> TournamentDeadlockReadyCheckStateResponse:
    active_round = _ready_round_snapshot_for_user(
        entry.active_round,
        current_user_id=current_user_id,
    )
    latest_round = _ready_round_snapshot_for_user(
        entry.latest_round,
        current_user_id=current_user_id,
    )
    return TournamentDeadlockReadyCheckStateResponse(
        active_round=active_round,
        latest_round=latest_round,
        state_version=ready_check_state_version(active_round, latest_round),
    )


def tournament_with_counts_stmt(
    tournament_page=None,
) -> Select[tuple[Tournament, str, str | None, str | None, int, int]]:
    # List pages need one grouped aggregate for the bounded page.  Single
    # tournament routes, however, already constrain the outer query to one
    # row (or one invite target).  Correlated counts keep those hot reads
    # indexed to the current tournament instead of repeatedly aggregating
    # every participant/assignment row in the database.
    if tournament_page is None:
        participant_count = (
            select(func.count(TournamentParticipant.id))
            .where(
                TournamentParticipant.tournament_id == Tournament.id,
                TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
            )
            .correlate(Tournament)
            .scalar_subquery()
        )
        locked_roster_count = (
            select(func.count(TournamentDeadlockAssignmentRun.id))
            .where(
                TournamentDeadlockAssignmentRun.tournament_id == Tournament.id,
                TournamentDeadlockAssignmentRun.status == "locked",
            )
            .correlate(Tournament)
            .scalar_subquery()
        )
        return (
            select(
                Tournament,
                User.display_name.label("organizer_display_name"),
                PlayerProfile.avatar_url.label("organizer_avatar_url"),
                PlayerProfile.avatar_asset_id.label("organizer_avatar_asset_id"),
                participant_count.label("participant_count"),
                locked_roster_count.label("locked_roster_count"),
            )
            .join(User, User.id == Tournament.organizer_user_id)
            .outerjoin(PlayerProfile, PlayerProfile.user_id == User.id)
        )

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
    if tournament_page is not None:
        page_ids = select(tournament_page.c.id)
        participant_counts_stmt = participant_counts_stmt.where(
            TournamentParticipant.tournament_id.in_(page_ids)
        )
        locked_roster_counts_stmt = locked_roster_counts_stmt.where(
            TournamentDeadlockAssignmentRun.tournament_id.in_(page_ids)
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

    stmt = (
        select(
            Tournament,
            User.display_name.label("organizer_display_name"),
            PlayerProfile.avatar_url.label("organizer_avatar_url"),
            PlayerProfile.avatar_asset_id.label("organizer_avatar_asset_id"),
            func.coalesce(participant_counts.c.participant_count, 0).label("participant_count"),
            func.coalesce(locked_roster_counts.c.locked_roster_count, 0).label("locked_roster_count"),
        )
        .join(User, User.id == Tournament.organizer_user_id)
        .outerjoin(PlayerProfile, PlayerProfile.user_id == User.id)
        .outerjoin(participant_counts, participant_counts.c.tournament_id == Tournament.id)
        .outerjoin(locked_roster_counts, locked_roster_counts.c.tournament_id == Tournament.id)
    )
    if tournament_page is not None:
        stmt = stmt.join(tournament_page, tournament_page.c.id == Tournament.id)
    return stmt


def serialize_tournament(
    tournament: Tournament,
    organizer_display_name: str,
    participant_count: int,
    *,
    organizer_avatar_url: str | None = None,
    cover_media: MediaDescriptorResponse | None = None,
    organizer_avatar_media: MediaDescriptorResponse | None = None,
    has_locked_deadlock_roster: bool = False,
    current_user_participant_status: str | None = None,
    current_user_has_invite_access: bool = False,
) -> TournamentResponse:
    return TournamentResponse(
        id=tournament.id,
        slug=tournament.slug,
        name=tournament.name,
        description=tournament.description,
        cover_url=compatibility_media_url(
            cover_media,
            preferred_variant="banner-1120",
            legacy_url=tournament.cover_url,
        ),
        cover_media=cover_media,
        visibility=tournament.visibility,
        status=tournament.status,
        format_slug=tournament.format_slug,
        organizer_user_id=tournament.organizer_user_id,
        organizer_display_name=organizer_display_name,
        organizer_avatar_url=compatibility_media_url(
            organizer_avatar_media,
            preferred_variant="avatar-256",
            legacy_url=organizer_avatar_url,
        ),
        organizer_avatar_media=organizer_avatar_media,
        participant_count=participant_count,
        allowed_ranks=list(tournament.allowed_ranks or []),
        max_participants=tournament.max_participants,
        has_locked_deadlock_roster=has_locked_deadlock_roster,
        current_user_participant_status=current_user_participant_status,
        current_user_has_invite_access=current_user_has_invite_access,
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
        state_version=tournament_state_version(
            tournament,
            participant_count=participant_count,
        ),
    )


async def tournament_media_descriptors(
    db_session: AsyncSession,
    tournament: Tournament,
    *,
    organizer_avatar_asset_id: str | None = None,
) -> tuple[MediaDescriptorResponse | None, MediaDescriptorResponse | None]:
    if organizer_avatar_asset_id is None:
        organizer_avatar_asset_id = await db_session.scalar(
            select(PlayerProfile.avatar_asset_id).where(
                PlayerProfile.user_id == tournament.organizer_user_id
            )
        )
    descriptors = await load_media_descriptors(
        db_session,
        (tournament.banner_asset_id, organizer_avatar_asset_id),
    )
    return (
        descriptors.get(tournament.banner_asset_id) if tournament.banner_asset_id else None,
        descriptors.get(organizer_avatar_asset_id) if organizer_avatar_asset_id else None,
    )


def serialize_participant(
    participant: TournamentParticipant,
    display_name: str,
) -> TournamentParticipantResponse:
    return TournamentParticipantResponse(
        id=participant.id,
        tournament_id=participant.tournament_id,
        user_id=participant.user_id,
        display_name=display_name,
        status=participant.status,
        entry_type=participant.entry_type,
        team_name=participant.team_name,
        moderation_note=participant.moderation_note,
        moderated_at=participant.moderated_at,
        moderated_by_user_id=participant.moderated_by_user_id,
        created_at=participant.created_at,
    )


def serialize_match(
    match: TournamentMatch,
    *,
    tournament_status: str,
    latest_round_number: int,
) -> TournamentMatchResponse:
    next_statuses = (
        ()
        if tournament_status in {"completed", "cancelled"}
        else available_match_statuses(
            match.status,
            tournament_status=tournament_status,
            current_round_number=match.round_number,
            latest_round_number=latest_round_number,
        )
    )
    return TournamentMatchResponse(
        id=match.id,
        tournament_id=match.tournament_id,
        title=match.title,
        round_number=match.round_number,
        sequence_number=match.sequence_number,
        home_label=match.home_label,
        away_label=match.away_label,
        home_team_id=match.home_team_id,
        away_team_id=match.away_team_id,
        winner_team_id=match.winner_team_id,
        home_source_match_id=match.home_source_match_id,
        away_source_match_id=match.away_source_match_id,
        scheduled_at=match.scheduled_at,
        status=match.status,
        home_score=match.home_score,
        away_score=match.away_score,
        winner_side=match.winner_side,
        report_note=match.report_note,
        reported_by_user_id=match.reported_by_user_id,
        reported_at=match.reported_at,
        created_at=match.created_at,
        available_next_statuses=list(next_statuses),
    )


def serialize_invite(
    tournament: Tournament,
    invite: TournamentInvite,
    *,
    now: datetime,
) -> TournamentInviteResponse:
    return TournamentInviteResponse(
        id=invite.id,
        tournament_id=invite.tournament_id,
        code=invite.code,
        note=invite.note,
        max_uses=invite.max_uses,
        use_count=invite.use_count,
        remaining_uses=remaining_invite_uses(invite.max_uses, invite.use_count),
        expires_at=invite.expires_at,
        revoked_at=invite.revoked_at,
        last_claimed_by_user_id=invite.last_claimed_by_user_id,
        last_claimed_at=invite.last_claimed_at,
        created_at=invite.created_at,
        is_active=invite_is_active(
            max_uses=invite.max_uses,
            use_count=invite.use_count,
            revoked_at=invite.revoked_at,
            expires_at=invite.expires_at,
            now=now,
        ),
    )


def serialize_deadlock_profile(profile: DeadlockProfile) -> DeadlockProfileResponse:
    return DeadlockProfileResponse.model_validate(profile)


async def get_tournament_or_404(db_session: AsyncSession, slug: str) -> Tournament:
    tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
    if tournament is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tournament not found.")
    return tournament


async def get_match_or_404(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    match_id: str,
) -> TournamentMatch:
    match = await db_session.scalar(
        select(TournamentMatch).where(
            TournamentMatch.id == match_id,
            TournamentMatch.tournament_id == tournament_id,
        )
    )
    if match is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
    return match


async def tournament_matches_in_order(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> list[TournamentMatch]:
    return (
        await db_session.scalars(
            select(TournamentMatch)
            .where(TournamentMatch.tournament_id == tournament_id)
            .order_by(
                TournamentMatch.round_number.asc(),
                TournamentMatch.sequence_number.asc(),
                TournamentMatch.created_at.asc(),
            )
        )
    ).all()


async def tournament_latest_round_number(
    db_session: AsyncSession,
    *,
    tournament_id: str,
) -> int | None:
    latest_round_number = await db_session.scalar(
        select(func.max(TournamentMatch.round_number)).where(
            TournamentMatch.tournament_id == tournament_id
        )
    )
    if latest_round_number is None:
        return None
    return int(latest_round_number)


async def serialize_single_match_for_tournament(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    match: TournamentMatch,
) -> TournamentMatchResponse:
    latest_round_number = await tournament_latest_round_number(
        db_session,
        tournament_id=tournament.id,
    )
    return serialize_match(
        match,
        tournament_status=tournament.status,
        latest_round_number=latest_round_number or match.round_number,
    )


async def get_participant_or_404(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    participant_id: str,
) -> TournamentParticipant:
    participant = await db_session.scalar(
        select(TournamentParticipant).where(
            TournamentParticipant.id == participant_id,
            TournamentParticipant.tournament_id == tournament_id,
        )
    )
    if participant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Participant not found.")
    return participant


async def get_invite_or_404(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    invite_id: str,
) -> TournamentInvite:
    invite = await db_session.scalar(
        select(TournamentInvite).where(
            TournamentInvite.id == invite_id,
            TournamentInvite.tournament_id == tournament_id,
        )
    )
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found.")
    return invite


def ensure_tournament_organizer(auth_session, tournament: Tournament) -> None:
    if tournament.organizer_user_id != auth_session.user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organizer can manage this tournament.",
        )


def ensure_tournament_manager(auth_session, tournament: Tournament) -> None:
    if (
        tournament.organizer_user_id != auth_session.user.id
        and not auth_session_has_admin_role(auth_session)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organizer or a platform admin can manage this tournament.",
        )


def auth_session_has_admin_role(auth_session) -> bool:
    if auth_session is None:
        return False
    return "admin" in auth_session.role_slugs or "superadmin" in auth_session.role_slugs


def ensure_tournament_summary_visible(
    tournament: Tournament,
    auth_session,
) -> None:
    try:
        is_visible = can_view_tournament_summary(
            tournament_visibility=tournament.visibility,
            has_authenticated_user=auth_session is not None,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if is_visible:
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required to view invite-only tournaments.",
    )


def ensure_tournament_workspace_visible(
    tournament: Tournament,
    *,
    auth_session,
    has_participant_record: bool,
) -> None:
    if tournament.visibility == "invite_only" and auth_session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to view invite-only tournament roster and bracket data.",
        )
    try:
        is_visible = can_view_tournament_workspace(
            tournament_visibility=tournament.visibility,
            is_participant=has_participant_record,
            is_organizer=auth_session is not None and tournament.organizer_user_id == auth_session.user.id,
            is_admin=auth_session_has_admin_role(auth_session),
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if is_visible:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Tournament roster and bracket data are visible only to joined participants, the organizer, or platform admins.",
    )


def ensure_deadlock_tournament_format(tournament: Tournament) -> None:
    if not is_solo_tournament_format(tournament.format_slug):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deadlock workflow is available only for solo tournaments.",
        )


async def participant_count_for_tournament(db_session: AsyncSession, tournament_id: str) -> int:
    return int(
        await db_session.scalar(
            select(func.count())
            .select_from(TournamentParticipant)
            .where(
                TournamentParticipant.tournament_id == tournament_id,
                TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
            )
        )
        or 0
    )

async def deadlock_rank_for_user(db_session: AsyncSession, user_id: str) -> str | None:
    return await db_session.scalar(
        select(DeadlockProfile.rank).where(DeadlockProfile.user_id == user_id)
    )


def auth_session_can_create_public_tournaments(auth_session) -> bool:
    return auth_session_has_admin_role(auth_session) or (
        int(auth_session.user.public_tournament_credits or 0) > 0
    )


async def consume_tournament_creation_allowance(
    db_session: AsyncSession,
    *,
    organizer_user_id: str,
    visibility: str,
    now: datetime,
) -> tuple[str, int]:
    organizer = await db_session.scalar(
        select(User)
        .where(User.id == organizer_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if organizer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organizer account not found.",
        )

    if visibility == "public":
        credits = int(organizer.public_tournament_credits or 0)
        if credits <= 0:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account has no public tournament credits.",
            )
        organizer.public_tournament_credits = credits - 1
        return "public_credit", organizer.public_tournament_credits

    monthly_remaining = await private_tournament_monthly_remaining(
        db_session,
        organizer_user_id=organizer.id,
        now=now,
    )
    if monthly_remaining > 0:
        return "private_monthly_base", int(organizer.private_tournament_credits or 0)

    credits = int(organizer.private_tournament_credits or 0)
    if credits <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This account has used its monthly private tournament and has no "
                "additional private tournament credits."
            ),
        )
    organizer.private_tournament_credits = credits - 1
    return "private_credit", organizer.private_tournament_credits


def ensure_tournament_schedule_is_future(
    payload: TournamentCreateRequest,
    *,
    now: datetime,
) -> None:
    schedule = (
        ("Registration start", payload.registration_starts_at),
        ("Registration close", payload.registration_closes_at),
        ("Ready-check start", payload.ready_check_starts_at),
        ("Ready-check end", payload.ready_check_ends_at),
        ("Team formation start", payload.captain_selection_starts_at),
        ("Tournament start", payload.starts_at),
    )
    for label, value in schedule:
        if value is not None and value <= now:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{label} must be in the future.",
            )


async def ensure_participant_join_limits(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    user_id: str,
) -> None:
    player_rank = await db_session.scalar(
        select(DeadlockProfile.rank).where(DeadlockProfile.user_id == user_id)
    )
    if tournament.max_participants is not None and not await has_free_participant_slot(
        db_session,
        tournament_id=tournament.id,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tournament participant limit has been reached.",
        )
    try:
        ensure_tournament_rank_allows_join(
            allowed_ranks=tournament.allowed_ranks,
            player_rank=player_rank,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def ensure_participant_join_limits_from_values(
    *,
    tournament: Tournament,
    player_rank: str | None,
    has_free_participant_slot: bool,
) -> None:
    """Validate rank and capacity using a durable slot snapshot."""
    try:
        if tournament.max_participants is not None and not has_free_participant_slot:
            raise TournamentWorkflowError(
                "Tournament participant limit has been reached."
            )
        ensure_tournament_rank_allows_join(
            allowed_ranks=tournament.allowed_ranks,
            player_rank=player_rank,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


async def participant_join_preflight(
    db_session: AsyncSession,
    *,
    slug: str,
    user_id: str,
) -> ParticipantJoinPreflight | None:
    existing_participant = (
        select(TournamentParticipant.id)
        .where(
            TournamentParticipant.tournament_id == Tournament.id,
            TournamentParticipant.user_id == user_id,
        )
        .exists()
    )
    free_slot = (
        select(TournamentParticipantSlot.id)
        .where(
            TournamentParticipantSlot.tournament_id == Tournament.id,
            TournamentParticipantSlot.participant_id.is_(None),
        )
        .limit(1)
        .exists()
    )
    active_participant_count = (
        select(func.count(TournamentParticipant.id))
        .where(
            TournamentParticipant.tournament_id == Tournament.id,
            TournamentParticipant.status.not_in(("withdrawn", "disqualified")),
        )
        .scalar_subquery()
    )
    free_slot = or_(
        free_slot,
        and_(
            Tournament.max_participants > PARTICIPANT_SLOT_MATERIALIZATION_LIMIT,
            active_participant_count < Tournament.max_participants,
        ),
    )
    player_rank = (
        select(DeadlockProfile.rank)
        .where(DeadlockProfile.user_id == user_id)
        .scalar_subquery()
    )
    locked_roster = (
        select(TournamentDeadlockAssignmentRun.id)
        .where(
            TournamentDeadlockAssignmentRun.tournament_id == Tournament.id,
            TournamentDeadlockAssignmentRun.status == "locked",
        )
        .exists()
    )
    invite_access = (
        select(TournamentInviteAccess.id)
        .where(
            TournamentInviteAccess.tournament_id == Tournament.id,
            TournamentInviteAccess.user_id == user_id,
        )
        .exists()
    )
    row = (
        await db_session.execute(
            select(
                Tournament,
                existing_participant.label("has_existing_participant"),
                free_slot.label("has_free_participant_slot"),
                player_rank.label("player_rank"),
                locked_roster.label("has_locked_deadlock_roster"),
                invite_access.label("has_invite_access"),
            )
            .where(Tournament.slug == slug)
        )
    ).first()
    if row is None:
        return None
    return ParticipantJoinPreflight(
        tournament=row[0],
        has_existing_participant=bool(row.has_existing_participant),
        has_free_participant_slot=bool(row.has_free_participant_slot),
        player_rank=row.player_rank,
        has_locked_deadlock_roster=bool(row.has_locked_deadlock_roster),
        has_invite_access=bool(row.has_invite_access),
    )


def ensure_distinct_match_sides(home_label: str, away_label: str) -> None:
    if home_label.casefold() == away_label.casefold():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Home and away sides must be different.",
        )


def deadlock_match_label_for_team(team_id: str) -> str:
    return f"Team {team_id}"


def sort_deadlock_team_id(team_id: str) -> tuple[int, str] | tuple[float, str]:
    normalized = str(team_id).strip()
    if normalized.isdigit():
        return (int(normalized), normalized)
    return (float("inf"), normalized)


def locked_deadlock_team_labels_from_run(
    run_row: TournamentDeadlockAssignmentRun,
) -> list[str]:
    snapshot = dict(run_row.result_snapshot or {})
    raw_teams = list(snapshot.get("teams") or [])
    team_ids = [
        str(team.get("team_id")).strip()
        for team in raw_teams
        if isinstance(team, dict) and team.get("team_id") is not None
    ]
    ordered_team_ids = sorted({team_id for team_id in team_ids if team_id}, key=sort_deadlock_team_id)
    return [deadlock_match_label_for_team(team_id) for team_id in ordered_team_ids]


def deadlock_team_id_from_match_label(label: str | None) -> str | None:
    if label is None:
        return None
    normalized = str(label).strip()
    prefix = "Team "
    if not normalized.startswith(prefix):
        return None
    team_id = normalized[len(prefix):].strip()
    return team_id or None


@dataclass(frozen=True, slots=True)
class TournamentTeamMemberProfile:
    handle: str
    avatar_url: str | None


def deadlock_assignment_member_user_ids(
    run_row: TournamentDeadlockAssignmentRun | None,
) -> set[str]:
    if run_row is None:
        return set()
    user_ids: set[str] = set()
    for team in list(dict(run_row.result_snapshot or {}).get("teams") or []):
        if not isinstance(team, dict):
            continue
        captain = team.get("captain")
        if isinstance(captain, dict) and captain.get("user_id") is not None:
            user_ids.add(str(captain["user_id"]))
        for slot in list(team.get("starter_slots") or []):
            if not isinstance(slot, dict):
                continue
            player = slot.get("assigned_player")
            if isinstance(player, dict) and player.get("user_id") is not None:
                user_ids.add(str(player["user_id"]))
        reserve_slot = team.get("reserve_slot")
        if isinstance(reserve_slot, dict):
            player = reserve_slot.get("assigned_player")
            if isinstance(player, dict) and player.get("user_id") is not None:
                user_ids.add(str(player["user_id"]))
    return user_ids


async def deadlock_assignment_member_profiles(
    db_session: AsyncSession,
    run_row: TournamentDeadlockAssignmentRun | None,
) -> dict[str, TournamentTeamMemberProfile]:
    user_ids = deadlock_assignment_member_user_ids(run_row)
    if not user_ids:
        return {}
    rows = (
        await db_session.execute(
            select(
                PlayerProfile.user_id,
                PlayerProfile.handle,
                PlayerProfile.display_name,
                User.display_name,
                PlayerProfile.avatar_url,
                PlayerProfile.avatar_asset_id,
            )
            .join(User, User.id == PlayerProfile.user_id)
            .where(PlayerProfile.user_id.in_(user_ids))
        )
    ).all()
    media_descriptors = await load_media_descriptors(
        db_session,
        tuple(row[5] for row in rows),
    )
    return {
        str(user_id): TournamentTeamMemberProfile(
            handle=str(handle or profile_display_name or user_display_name or "Игрок"),
            avatar_url=compatibility_media_url(
                media_descriptors.get(avatar_asset_id) if avatar_asset_id else None,
                preferred_variant="avatar-256",
                legacy_url=str(avatar_url) if avatar_url else None,
            ),
        )
        for (
            user_id,
            handle,
            profile_display_name,
            user_display_name,
            avatar_url,
            avatar_asset_id,
        ) in rows
    }


def serialize_deadlock_assignment_member(
    player: dict[str, Any] | None,
    *,
    member_profiles: dict[str, TournamentTeamMemberProfile],
    is_captain: bool = False,
    is_substitute: bool = False,
) -> TournamentBracketTeamMemberResponse | None:
    if not isinstance(player, dict) or player.get("user_id") is None:
        return None
    user_id = str(player["user_id"])
    member_profile = member_profiles.get(user_id)
    rank = str(player.get("rank") or "").strip() or None
    raw_subrank = player.get("subrank")
    subrank = int(raw_subrank) if raw_subrank is not None else None
    return TournamentBracketTeamMemberResponse(
        user_id=user_id,
        handle=(
            member_profile.handle
            if member_profile is not None
            else str(player.get("username") or "Unknown")
        ),
        avatar_url=member_profile.avatar_url if member_profile is not None else None,
        rank=rank,
        subrank=subrank,
        is_captain=is_captain,
        is_substitute=is_substitute,
    )


def serialize_deadlock_bracket_teams(
    run_row: TournamentDeadlockAssignmentRun | None,
    *,
    include_members: bool = True,
    member_profiles: dict[str, TournamentTeamMemberProfile] | None = None,
) -> list[TournamentBracketTeamResponse]:
    if run_row is None:
        return []
    snapshot = dict(run_row.result_snapshot or {})
    raw_teams = [
        team
        for team in list(snapshot.get("teams") or [])
        if isinstance(team, dict) and team.get("team_id") is not None
    ]
    strength_order = strength_seed_teams(raw_teams)
    team_by_id = {str(team["team_id"]).strip(): team for team in raw_teams}
    ordered_teams = [team_by_id[seeded.team_id] for seeded in strength_order]
    resolved_member_profiles = member_profiles or {}
    serialized_teams: list[TournamentBracketTeamResponse] = []
    for seed, team in enumerate(ordered_teams, start=1):
        team_id = str(team["team_id"]).strip()
        captain = team.get("captain")
        captain_id = (
            str(captain.get("user_id"))
            if isinstance(captain, dict) and captain.get("user_id") is not None
            else None
        )
        members: list[TournamentBracketTeamMemberResponse] = []
        if include_members:
            captain_member = serialize_deadlock_assignment_member(
                captain if isinstance(captain, dict) else None,
                member_profiles=resolved_member_profiles,
                is_captain=True,
            )
            if captain_member is not None:
                members.append(captain_member)
                captain_id = captain_member.user_id
            for slot in list(team.get("starter_slots") or []):
                if not isinstance(slot, dict):
                    continue
                member = serialize_deadlock_assignment_member(
                    slot.get("assigned_player") if isinstance(slot.get("assigned_player"), dict) else None,
                    member_profiles=resolved_member_profiles,
                )
                if member is not None:
                    members.append(member)
            reserve_slot = team.get("reserve_slot")
            if isinstance(reserve_slot, dict):
                reserve_member = serialize_deadlock_assignment_member(
                    reserve_slot.get("assigned_player")
                    if isinstance(reserve_slot.get("assigned_player"), dict)
                    else None,
                    member_profiles=resolved_member_profiles,
                    is_substitute=True,
                )
                if reserve_member is not None:
                    members.append(reserve_member)
        serialized_teams.append(
            TournamentBracketTeamResponse(
                id=team_id,
                name=str(team.get("team_name") or "").strip() or deadlock_match_label_for_team(team_id),
                seed=seed,
                starter_strength=round(float(team.get("starter_strength") or 0.0), 4),
                starter_average_strength=round(
                    float(team.get("starter_average_strength") or 0.0),
                    4,
                ),
                captain_id=captain_id,
                members=members,
            )
        )
    return serialized_teams


def serialize_bracket_match_projection(
    match: TournamentMatch,
    *,
    tournament: Tournament,
    total_rounds: int,
) -> TournamentBracketMatchResponse:
    team_a_id = match.home_team_id or deadlock_team_id_from_match_label(match.home_label)
    team_b_id = match.away_team_id or deadlock_team_id_from_match_label(match.away_label)
    winner_team_id = match.winner_team_id
    if winner_team_id is None and match.winner_side == "home":
        winner_team_id = team_a_id
    elif winner_team_id is None and match.winner_side == "away":
        winner_team_id = team_b_id
    return TournamentBracketMatchResponse(
        id=match.id,
        round_number=match.round_number,
        match_order=match.sequence_number,
        sequence_number=match.sequence_number,
        team_a_id=team_a_id,
        team_b_id=team_b_id,
        home_label=match.home_label,
        away_label=match.away_label,
        score_a=match.home_score,
        score_b=match.away_score,
        home_score=match.home_score,
        away_score=match.away_score,
        winner_team_id=winner_team_id,
        winner_side=match.winner_side,
        home_source_match_id=match.home_source_match_id,
        away_source_match_id=match.away_source_match_id,
        status=match.status,
        match_format=(
            tournament.final_format
            if match.round_number == total_rounds
            else tournament.match_format
        ),
        ready=bool(team_a_id and team_b_id),
        scheduled_at=match.scheduled_at,
    )


def deadlock_assignment_run_user_ids(run_row: TournamentDeadlockAssignmentRun) -> set[str]:
    snapshot = dict(run_row.result_snapshot or {})
    user_ids: set[str] = set()
    for team in list(snapshot.get("teams") or []):
        if not isinstance(team, dict):
            continue
        captain = team.get("captain")
        if isinstance(captain, dict) and captain.get("user_id") is not None:
            user_ids.add(str(captain["user_id"]))
        for slot in list(team.get("starter_slots") or []):
            if not isinstance(slot, dict):
                continue
            player = slot.get("assigned_player")
            if isinstance(player, dict) and player.get("user_id") is not None:
                user_ids.add(str(player["user_id"]))
        reserve_slot = team.get("reserve_slot")
        if isinstance(reserve_slot, dict):
            player = reserve_slot.get("assigned_player")
            if isinstance(player, dict) and player.get("user_id") is not None:
                user_ids.add(str(player["user_id"]))
    return user_ids


def winner_label_for_match(match: TournamentMatch) -> str | None:
    if match.winner_side == "home":
        return match.home_label
    if match.winner_side == "away":
        return match.away_label
    return None


def normalize_invite_code(code: str) -> str:
    return "".join(char for char in code.upper() if char.isalnum())


def normalized_team_name(entry_type: str, team_name: str | None) -> str | None:
    normalized = (team_name or "").strip() or None
    try:
        ensure_solo_entry(entry_type, normalized)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return None


async def ensure_no_existing_participant(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    user_id: str,
) -> None:
    existing_participant = await db_session.scalar(
        select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.user_id == user_id,
        )
    )
    if existing_participant is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This user is already registered in the tournament.",
        )


async def participant_display_name_for_user(db_session: AsyncSession, user_id: str) -> str:
    display_name = await db_session.scalar(
        select(func.coalesce(PlayerProfile.display_name, User.display_name))
        .select_from(User)
        .outerjoin(PlayerProfile, PlayerProfile.user_id == User.id)
        .where(User.id == user_id)
    )
    return display_name or "Unknown"


async def joined_participant_for_user(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    user_id: str,
) -> TournamentParticipant | None:
    return await db_session.scalar(
        select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.user_id == user_id,
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
        )
    )


async def participant_for_user(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    user_id: str,
) -> TournamentParticipant | None:
    return await db_session.scalar(
        select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.user_id == user_id,
        )
    )


async def invite_access_for_user(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    user_id: str,
) -> TournamentInviteAccess | None:
    return await db_session.scalar(
        select(TournamentInviteAccess).where(
            TournamentInviteAccess.tournament_id == tournament_id,
            TournamentInviteAccess.user_id == user_id,
        )
    )


async def workspace_access_for_user(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    user_id: str,
) -> tuple[
    TournamentParticipant | None,
    TournamentInviteAccess | None,
    PlayerTournamentCommitmentResponse | None,
]:
    commitment_tournament = aliased(Tournament)
    row = (
        await db_session.execute(
            select(
                TournamentParticipant,
                TournamentInviteAccess,
                PlayerTournamentCommitment,
                commitment_tournament.slug,
                commitment_tournament.name,
            )
            .select_from(User)
            .outerjoin(
                TournamentParticipant,
                (TournamentParticipant.tournament_id == tournament_id)
                & (TournamentParticipant.user_id == User.id),
            )
            .outerjoin(
                TournamentInviteAccess,
                (TournamentInviteAccess.tournament_id == tournament_id)
                & (TournamentInviteAccess.user_id == User.id),
            )
            .outerjoin(
                PlayerTournamentCommitment,
                (PlayerTournamentCommitment.user_id == User.id)
                & (PlayerTournamentCommitment.released_at.is_(None)),
            )
            .outerjoin(
                commitment_tournament,
                commitment_tournament.id == PlayerTournamentCommitment.tournament_id,
            )
            .where(User.id == user_id)
        )
    ).first()
    if row is None:
        return None, None, None
    commitment = row[2]
    return (
        row[0],
        row[1],
        (
            PlayerTournamentCommitmentResponse(
                id=commitment.id,
                tournament_id=commitment.tournament_id,
                tournament_slug=str(row[3]),
                tournament_name=str(row[4]),
                assignment_run_id=commitment.assignment_run_id,
                team_id=commitment.team_id,
                team_name=commitment.team_name,
                activated_at=commitment.activated_at,
            )
            if commitment is not None
            else None
        ),
    )


def can_view_tournament_workspace_data(
    tournament: Tournament,
    *,
    auth_session,
    has_participant_record: bool,
) -> bool:
    if tournament.visibility == "invite_only" and auth_session is None:
        return False
    try:
        return can_view_tournament_workspace(
            tournament_visibility=tournament.visibility,
            is_participant=has_participant_record,
            is_organizer=auth_session is not None and tournament.organizer_user_id == auth_session.user.id,
            is_admin=auth_session_has_admin_role(auth_session),
        )
    except TournamentWorkflowError:
        return False


async def tournament_participant_page(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    search: str | None = None,
    limit: int = PARTICIPANT_LIST_DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[TournamentParticipantResponse], int, bool]:
    normalized_search = search.strip() if isinstance(search, str) else ""
    cache_key = (tournament_id, normalized_search.casefold(), int(limit), int(offset))
    cached_page = _get_participant_page_cache(cache_key)
    if cached_page is not None:
        return cached_page

    participant_filters = [
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
    ]
    search_candidate_users = None
    if normalized_search:
        search_pattern = f"%{normalized_search.casefold()}%"
        search_candidate_users = union(
            select(PlayerProfile.user_id.label("user_id")).where(
                func.lower(PlayerProfile.display_name).like(search_pattern),
            ),
            select(PlayerProfile.user_id.label("user_id")).where(
                func.lower(PlayerProfile.handle).like(search_pattern),
            ),
            select(User.id.label("user_id")).where(
                func.lower(User.display_name).like(search_pattern),
            ),
        ).cte("participant_search_candidate_users")

    total_count_stmt = select(func.count()).select_from(TournamentParticipant)
    if search_candidate_users is not None:
        total_count_stmt = total_count_stmt.join(
            search_candidate_users,
            search_candidate_users.c.user_id == TournamentParticipant.user_id,
        )
    total_count_stmt = total_count_stmt.where(*participant_filters).scalar_subquery()

    rows_stmt = (
        select(
            TournamentParticipant,
            func.coalesce(PlayerProfile.display_name, User.display_name).label("display_name"),
            total_count_stmt.label("total_count"),
        )
        .join(User, User.id == TournamentParticipant.user_id)
        .outerjoin(PlayerProfile, PlayerProfile.user_id == TournamentParticipant.user_id)
        .where(*participant_filters)
        .order_by(TournamentParticipant.created_at.asc(), TournamentParticipant.id.asc())
        .limit(limit)
        .offset(offset)
    )
    if search_candidate_users is not None:
        rows_stmt = rows_stmt.join(
            search_candidate_users,
            search_candidate_users.c.user_id == TournamentParticipant.user_id,
        )

    rows = (await db_session.execute(rows_stmt)).all()
    if rows:
        total = int(rows[0].total_count or 0)
    elif offset > 0:
        fallback_count_stmt = select(func.count()).select_from(TournamentParticipant)
        if search_candidate_users is not None:
            fallback_count_stmt = fallback_count_stmt.join(
                search_candidate_users,
                search_candidate_users.c.user_id == TournamentParticipant.user_id,
            )
        fallback_count_stmt = fallback_count_stmt.where(*participant_filters)
        total = int(
            await db_session.scalar(
                fallback_count_stmt
            )
            or 0
        )
    else:
        total = 0
    participants = [
        serialize_participant(participant, display_name)
        for participant, display_name, _total_count in rows
    ]
    has_more = offset + len(rows) < total
    _set_participant_page_cache(cache_key, participants, total, has_more)
    return participants, total, has_more






































































async def serialize_deadlock_captain_round(
    db_session: AsyncSession,
    round_row: TournamentDeadlockCaptainRound,
    *,
    current_user_id: str | None = None,
    include_entries: bool = False,
) -> TournamentDeadlockCaptainRoundResponse:
    entry_rows = await deadlock_captain_entries_for_round(db_session, round_id=round_row.id)
    round_state = CaptainRoundState.from_entries(
        round_id=round_row.id,
        teams_count=round_row.teams_count,
        status=round_row.status,
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

    profile_rows_by_user_id: dict[str, dict[str, object]] = {}
    user_ids = [row.user_id for row in entry_rows]
    if user_ids:
        details = await db_session.execute(
            select(
                User.id.label("user_id"),
                func.coalesce(PlayerProfile.display_name, User.display_name).label("display_name"),
                DeadlockProfile.rank,
                DeadlockProfile.subrank,
                DeadlockProfile.playtime,
                DeadlockProfile.captain_priority,
            )
            .select_from(User)
            .outerjoin(PlayerProfile, PlayerProfile.user_id == User.id)
            .outerjoin(DeadlockProfile, DeadlockProfile.user_id == User.id)
            .where(User.id.in_(user_ids))
        )
        profile_rows_by_user_id = {
            str(row.user_id): dict(row._mapping)
            for row in details
        }

    serialized_entries: list[TournamentDeadlockCaptainEntryResponse] = []
    current_user_entry: TournamentDeadlockCaptainEntryResponse | None = None
    for row in entry_rows:
        profile_row = profile_rows_by_user_id.get(row.user_id, {})
        rank = profile_row.get("rank")
        subrank = int(profile_row["subrank"]) if profile_row.get("subrank") is not None else None
        playtime = str(profile_row["playtime"]) if profile_row.get("playtime") is not None else None
        strength = None
        if rank is not None and subrank is not None and playtime is not None:
            strength = round(float(calculate_player_strength(str(rank), subrank, playtime)), 4)

        serialized_entry = TournamentDeadlockCaptainEntryResponse(
            user_id=row.user_id,
            display_name=str(profile_row.get("display_name") or "Unknown"),
            rank=str(rank) if rank is not None else None,
            subrank=subrank,
            playtime=playtime,
            captain_priority=(
                str(profile_row["captain_priority"])
                if profile_row.get("captain_priority") is not None
                else None
            ),
            captain_priority_bucket=(
                captain_priority_bucket(str(rank), profile_row.get("captain_priority"))
                if rank is not None
                else None
            ),
            strength=strength,
            offer_order=row.offer_order,
            state=row.state,
            assigned_team_id=row.assigned_team_id,
            responded_at=row.responded_at,
            updated_at=row.updated_at,
        )
        if include_entries:
            serialized_entries.append(serialized_entry)
        if current_user_id is not None and row.user_id == current_user_id:
            current_user_entry = serialized_entry

    return TournamentDeadlockCaptainRoundResponse(
        id=round_row.id,
        tournament_id=round_row.tournament_id,
        source_ready_round_id=round_row.source_ready_round_id,
        teams_count=round_row.teams_count,
        status=round_row.status,
        candidate_count=round_state.candidate_count,
        accepted_count=round_state.accepted_count,
        offered_count=round_state.offered_count,
        declined_count=round_state.declined_count,
        queued_count=round_state.queued_count,
        assigned_count=round_state.assigned_count,
        initiated_by_user_id=round_row.initiated_by_user_id,
        created_at=round_row.created_at,
        closed_at=round_row.closed_at,
        finalized_at=round_row.finalized_at,
        can_finalize=round_state.can_finalize,
        current_user_entry=current_user_entry,
        entries=serialized_entries,
    )


def serialize_deadlock_auto_assignment_run(
    run_row: TournamentDeadlockAssignmentRun,
    *,
    freshness: AutoAssignmentRunFreshness | None = None,
) -> TournamentDeadlockAutoAssignmentRunResponse:
    snapshot = dict(run_row.result_snapshot or {})
    return TournamentDeadlockAutoAssignmentRunResponse(
        id=run_row.id,
        tournament_id=run_row.tournament_id,
        source_captain_round_id=run_row.source_captain_round_id,
        source_ready_round_id=run_row.source_ready_round_id,
        created_by_user_id=run_row.created_by_user_id,
        status=run_row.status,
        published_at=run_row.published_at,
        published_by_user_id=run_row.published_by_user_id,
        locked_at=run_row.locked_at,
        locked_by_user_id=run_row.locked_by_user_id,
        summary_text=run_row.summary_text,
        teams=list(snapshot.get("teams") or []),
        optimization_summary=dict(snapshot.get("optimization_summary") or {}),
        preference_metrics=dict(snapshot.get("preference_metrics") or {}),
        candidate_pool_user_ids=[str(user_id) for user_id in list(run_row.candidate_pool_user_ids or [])],
        leftover_user_ids=[str(user_id) for user_id in list(run_row.leftover_user_ids or [])],
        is_stale=freshness.is_stale if freshness is not None else False,
        stale_reasons=list(freshness.stale_reasons if freshness is not None else ()),
        created_at=run_row.created_at,
    )


async def build_tournament_bracket_response(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    auth_session,
    has_participant_record: bool | None = None,
    visible_assignment_run: TournamentDeadlockAssignmentRun | None = None,
    assignment_run_loaded: bool = False,
    include_team_members: bool = True,
) -> TournamentBracketResponse:
    if has_participant_record is None:
        has_participant_record = False
        if auth_session is not None:
            has_participant_record = (
                await participant_for_user(
                    db_session,
                    tournament_id=tournament.id,
                    user_id=auth_session.user.id,
                )
            ) is not None
    ensure_tournament_workspace_visible(
        tournament,
        auth_session=auth_session,
        has_participant_record=has_participant_record,
    )
    can_manage = bool(
        auth_session is not None
        and (
            tournament.organizer_user_id == auth_session.user.id
            or auth_session_has_admin_role(auth_session)
        )
    )
    cache_key = (
        tournament.id,
        int(tournament.bracket_revision or 0),
        tournament.status,
        tournament.match_format,
        tournament.final_format,
        can_manage,
        include_team_members,
    )
    cached_response = _get_bracket_response_cache(cache_key)
    if cached_response is not None:
        return cached_response

    visible_run = visible_assignment_run
    if not assignment_run_loaded and is_solo_tournament_format(tournament.format_slug):
        visible_run = await deadlock_published_auto_assignment_run_for_tournament(
            db_session,
            tournament_id=tournament.id,
        )
        if visible_run is None and can_manage:
            visible_run = await deadlock_auto_assignment_run_for_tournament(
                db_session,
                tournament_id=tournament.id,
            )
    match_rows = await tournament_matches_in_order(db_session, tournament_id=tournament.id)
    member_profiles = (
        await deadlock_assignment_member_profiles(db_session, visible_run)
        if include_team_members
        else {}
    )
    teams = serialize_deadlock_bracket_teams(
        visible_run,
        include_members=include_team_members,
        member_profiles=member_profiles,
    )
    total_rounds = max((match.round_number for match in match_rows), default=1)
    bracket_status = "pending"
    if match_rows:
        bracket_status = "ready"
    elif teams:
        bracket_status = "teams_ready"
    response = TournamentBracketResponse(
        tournament_id=tournament.id,
        tournament_status=tournament.status,
        status=bracket_status,
        revision=int(tournament.bracket_revision or 0),
        can_manage=can_manage,
        capabilities=bracket_capabilities(
            tournament_status=tournament.status,
            can_manage=can_manage,
        ),
        teams=teams,
        matches=[
            serialize_bracket_match_projection(
                match,
                tournament=tournament,
                total_rounds=total_rounds,
            )
            for match in match_rows
        ],
    )
    if bracket_status == "ready":
        _set_bracket_response_cache(cache_key, response)
    return response


async def build_tournament_workspace_detail_bracket_response(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    can_manage: bool,
    visible_assignment_run: TournamentDeadlockAssignmentRun | None = None,
    assignment_run_loaded: bool = False,
) -> TournamentBracketResponse:
    visible_run = visible_assignment_run
    if (
        not assignment_run_loaded
        and is_solo_tournament_format(tournament.format_slug)
        and tournament.status != "registration_open"
    ):
        visible_run = await deadlock_published_auto_assignment_run_for_tournament(
            db_session,
            tournament_id=tournament.id,
        )
        if visible_run is None and can_manage:
            visible_run = await deadlock_auto_assignment_run_for_tournament(
                db_session,
                tournament_id=tournament.id,
            )

    member_profiles = await deadlock_assignment_member_profiles(db_session, visible_run)
    teams = serialize_deadlock_bracket_teams(
        visible_run,
        member_profiles=member_profiles,
    )
    bracket_status = "pending"
    if int(tournament.bracket_revision or 0) > 0:
        bracket_status = "ready"
    elif teams:
        bracket_status = "teams_ready"

    return TournamentBracketResponse(
        tournament_id=tournament.id,
        tournament_status=tournament.status,
        status=bracket_status,
        revision=int(tournament.bracket_revision or 0),
        can_manage=can_manage,
        capabilities=bracket_capabilities(
            tournament_status=tournament.status,
            can_manage=can_manage,
        ),
        teams=teams,
        matches=[],
    )


def build_tournament_workspace_bracket_summary_response(
    *,
    tournament: Tournament,
    can_manage: bool,
) -> TournamentBracketResponse:
    bracket_status = "ready" if int(tournament.bracket_revision or 0) > 0 else "pending"
    return TournamentBracketResponse(
        tournament_id=tournament.id,
        tournament_status=tournament.status,
        status=bracket_status,
        revision=int(tournament.bracket_revision or 0),
        can_manage=can_manage,
        capabilities=bracket_capabilities(
            tournament_status=tournament.status,
            can_manage=can_manage,
        ),
        teams=[],
        matches=[],
    )


async def build_deadlock_ready_check_state_response(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    current_user_id: str,
    tournament_bracket_revision: int | None = None,
    active_round: TournamentDeadlockReadyRound | None = None,
    latest_round: TournamentDeadlockReadyRound | None = None,
    rounds_loaded: bool = False,
) -> TournamentDeadlockReadyCheckStateResponse:
    loaded_rounds = rounds_loaded
    loaded_active_round = active_round
    loaded_latest_round = latest_round

    async def load_ready_rounds() -> tuple[TournamentDeadlockReadyRound | None, TournamentDeadlockReadyRound | None]:
        nonlocal loaded_active_round, loaded_latest_round, loaded_rounds
        if not loaded_rounds:
            loaded_active_round, loaded_latest_round = await deadlock_ready_state_round_for_tournament(
                db_session,
                tournament_id=tournament_id,
            )
            loaded_rounds = True
        return loaded_active_round, loaded_latest_round

    cache_revision = int(tournament_bracket_revision or 0)
    cache_key = (tournament_id, cache_revision)
    if cache_revision > 0:
        cached_state = _get_ready_check_state_cache(cache_key)
        if cached_state is not None:
            return _ready_check_state_response_from_cache(
                cached_state,
                current_user_id=current_user_id,
            )

    if cache_revision > 0:
        async with _ready_check_state_cache_lock(cache_key):
            cached_state = _get_ready_check_state_cache(cache_key)
            if cached_state is not None:
                return _ready_check_state_response_from_cache(
                    cached_state,
                    current_user_id=current_user_id,
                )
            active_round, latest_round = await load_ready_rounds()
            if (
                active_round is None
                and latest_round is not None
                and latest_round.status == "closed"
            ):
                cached_state = ReadyCheckStateCacheEntry(
                    expires_at=time.monotonic() + READY_CHECK_STATE_CACHE_TTL_SECONDS,
                    active_round=None,
                    latest_round=await build_deadlock_ready_round_state_snapshot(
                        db_session,
                        latest_round,
                    ),
                )
                _set_ready_check_state_cache(cache_key, cached_state)
                return _ready_check_state_response_from_cache(
                    cached_state,
                    current_user_id=current_user_id,
                )
    else:
        active_round, latest_round = await load_ready_rounds()

    active_response = (
        await serialize_deadlock_ready_round(
            db_session,
            active_round,
            current_user_id=current_user_id,
        )
        if active_round is not None
        else None
    )
    latest_response: TournamentDeadlockReadyRoundResponse | None = None
    if latest_round is not None:
        if active_round is not None and latest_round.id == active_round.id:
            latest_response = active_response
        else:
            latest_response = await serialize_deadlock_ready_round(
                db_session,
                latest_round,
                current_user_id=current_user_id,
            )
    return TournamentDeadlockReadyCheckStateResponse(
        active_round=active_response,
        latest_round=latest_response,
        state_version=ready_check_state_version(active_response, latest_response),
    )


async def build_deadlock_auto_assignment_state_response(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    auth_session,
    include_freshness: bool,
    latest_run: TournamentDeadlockAssignmentRun | None = None,
    published_run: TournamentDeadlockAssignmentRun | None = None,
    assignment_runs_loaded: bool = False,
) -> TournamentDeadlockAutoAssignmentStateResponse:
    current_user_id = auth_session.user.id
    is_organizer = tournament.organizer_user_id == current_user_id
    if not assignment_runs_loaded:
        latest_run, published_run = await deadlock_auto_assignment_state_runs_for_tournament(
            db_session,
            tournament_id=tournament.id,
        )
    current_inputs = (
        await deadlock_latest_auto_assignment_inputs_for_tournament(
            db_session,
            tournament_id=tournament.id,
        )
        if include_freshness and (latest_run is not None or published_run is not None)
        else None
    )
    latest_run_freshness = (
        await deadlock_auto_assignment_run_freshness(
            db_session,
            tournament_id=tournament.id,
            run_row=latest_run,
            current_inputs=current_inputs,
        )
        if include_freshness and latest_run is not None
        else None
    )
    published_run_freshness = (
        await deadlock_auto_assignment_run_freshness(
            db_session,
            tournament_id=tournament.id,
            run_row=published_run,
            current_inputs=current_inputs,
        )
        if include_freshness and published_run is not None
        else None
    )
    return TournamentDeadlockAutoAssignmentStateResponse(
        latest_run=(
            serialize_deadlock_auto_assignment_run(
                latest_run,
                freshness=latest_run_freshness,
            )
            if is_organizer and latest_run is not None
            else None
        ),
        published_run=(
            serialize_deadlock_auto_assignment_run(
                published_run,
                freshness=published_run_freshness,
            )
            if published_run is not None
            else None
        ),
    )






async def create_participant(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    user: User,
    entry_type: str,
    team_name: str | None,
) -> TournamentParticipant:
    try:
        ensure_solo_entry(entry_type, team_name)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    participant = TournamentParticipant(
        id=new_uuid(),
        tournament_id=tournament.id,
        user_id=user.id,
        entry_type=entry_type,
        status="registered",
        team_name=team_name,
    )
    inserted_id = await db_session.scalar(
        postgresql.insert(TournamentParticipant)
        .values(
            id=participant.id,
            tournament_id=participant.tournament_id,
            user_id=participant.user_id,
            entry_type=participant.entry_type,
            status=participant.status,
            team_name=participant.team_name,
        )
        .on_conflict_do_nothing(
            constraint="uq_tournament_participants_tournament_user"
        )
        .returning(TournamentParticipant.id)
    )
    if inserted_id is None:
        raise TournamentWorkflowError(
            "This user is already registered in the tournament."
        )
    participant = await db_session.scalar(
        select(TournamentParticipant).where(TournamentParticipant.id == inserted_id)
    )
    if participant is None:
        raise RuntimeError("Participant disappeared after an idempotent insert.")
    await claim_participant_slot(
        db_session,
        tournament_id=tournament.id,
        max_participants=tournament.max_participants,
        participant_id=participant.id,
    )
    await db_session.flush()
    return participant


async def generate_unique_invite_code(db_session: AsyncSession) -> str:
    for _ in range(8):
        code = "".join(secrets.choice(INVITE_CODE_ALPHABET) for _ in range(10))
        existing = await db_session.scalar(
            select(TournamentInvite.id).where(TournamentInvite.code == code)
        )
        if existing is None:
            return code
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to generate a unique invite code.",
    )


async def invite_code_is_available(db_session: AsyncSession, code: str) -> bool:
    normalized_code = normalize_invite_code(code)
    if len(normalized_code) < 10 or len(normalized_code) > 24:
        return False
    existing = await db_session.scalar(
        select(TournamentInvite.id).where(TournamentInvite.code == normalized_code)
    )
    return existing is None


@router.get("", response_model=list[TournamentResponse])
async def list_tournaments(
    response: Response,
    search: str | None = Query(default=None, max_length=120),
    rank: list[str] = Query(default_factory=list),
    open_registration: bool = Query(default=False),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        pattern="^(registration_open|registration_closed|in_progress|completed|cancelled)$",
    ),
    participants_sort: str | None = Query(default=None, pattern="^(asc|desc)$"),
    date_sort: str | None = Query(default=None, pattern="^(nearest|farthest)$"),
    limit: int = Query(
        default=TOURNAMENT_LIST_DEFAULT_LIMIT,
        ge=1,
        le=TOURNAMENT_LIST_MAX_LIMIT,
    ),
    offset: int = Query(default=0, ge=0),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[TournamentResponse]:
    filters = [
        Tournament.visibility == "public",
        Tournament.format_slug == SOLO_TOURNAMENT_FORMAT,
    ]
    normalized_search = search.strip() if isinstance(search, str) else ""
    if normalized_search:
        search_pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                Tournament.name.ilike(search_pattern),
                User.display_name.ilike(search_pattern),
            )
        )
    if open_registration:
        filters.append(Tournament.status == "registration_open")
    if status_filter:
        filters.append(Tournament.status == status_filter)

    allowed_rank_filter = normalize_tournament_allowed_ranks(rank)
    if allowed_rank_filter:
        filters.append(
            or_(
                Tournament.allowed_ranks.is_(None),
                func.json_array_length(Tournament.allowed_ranks) == 0,
                cast(Tournament.allowed_ranks, postgresql.JSONB).has_any(
                    postgresql.array(list(allowed_rank_filter))
                ),
            )
        )

    total = int(
        await db_session.scalar(
            select(func.count())
            .select_from(Tournament)
            .join(User, User.id == Tournament.organizer_user_id)
            .where(*filters)
        )
        or 0
    )

    participant_count_for_sort = (
        select(func.count(TournamentParticipant.id))
        .where(
            TournamentParticipant.tournament_id == Tournament.id,
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
        )
        .correlate(Tournament)
        .scalar_subquery()
    )
    page_stmt = (
        select(
            Tournament.id.label("id"),
            Tournament.created_at.label("created_at"),
            Tournament.starts_at.label("starts_at"),
        )
        .join(User, User.id == Tournament.organizer_user_id)
        .where(*filters)
    )
    if participants_sort == "asc":
        page_stmt = page_stmt.add_columns(
            participant_count_for_sort.label("participant_sort_count")
        ).order_by(
            participant_count_for_sort.asc(),
            Tournament.created_at.desc(),
            Tournament.id.desc(),
        )
    elif participants_sort == "desc":
        page_stmt = page_stmt.add_columns(
            participant_count_for_sort.label("participant_sort_count")
        ).order_by(
            participant_count_for_sort.desc(),
            Tournament.created_at.desc(),
            Tournament.id.desc(),
        )
    elif date_sort == "nearest":
        page_stmt = page_stmt.order_by(
            Tournament.starts_at.asc().nulls_last(),
            Tournament.created_at.desc(),
            Tournament.id.desc(),
        )
    elif date_sort == "farthest":
        page_stmt = page_stmt.order_by(
            Tournament.starts_at.desc().nulls_last(),
            Tournament.created_at.desc(),
            Tournament.id.desc(),
        )
    else:
        page_stmt = page_stmt.order_by(Tournament.created_at.desc(), Tournament.id.desc())
    tournament_page = page_stmt.limit(limit).offset(offset).cte("tournament_page")

    stmt = tournament_with_counts_stmt(tournament_page)
    if participants_sort == "asc":
        stmt = stmt.order_by(
            tournament_page.c.participant_sort_count.asc(),
            tournament_page.c.created_at.desc(),
            tournament_page.c.id.desc(),
        )
    elif participants_sort == "desc":
        stmt = stmt.order_by(
            tournament_page.c.participant_sort_count.desc(),
            tournament_page.c.created_at.desc(),
            tournament_page.c.id.desc(),
        )
    elif date_sort == "nearest":
        stmt = stmt.order_by(
            tournament_page.c.starts_at.asc().nulls_last(),
            tournament_page.c.created_at.desc(),
            tournament_page.c.id.desc(),
        )
    elif date_sort == "farthest":
        stmt = stmt.order_by(
            tournament_page.c.starts_at.desc().nulls_last(),
            tournament_page.c.created_at.desc(),
            tournament_page.c.id.desc(),
        )
    else:
        stmt = stmt.order_by(tournament_page.c.created_at.desc(), tournament_page.c.id.desc())

    rows = (await db_session.execute(stmt)).all()
    media_descriptors = await load_media_descriptors(
        db_session,
        tuple(
            asset_id
            for row in rows
            for asset_id in (row[0].banner_asset_id, row[3])
        ),
    )
    serialized = [
        serialize_tournament(
            tournament,
            organizer_display_name,
            int(participant_count),
            organizer_avatar_url=organizer_avatar_url,
            cover_media=media_descriptors.get(tournament.banner_asset_id)
            if tournament.banner_asset_id
            else None,
            organizer_avatar_media=media_descriptors.get(organizer_avatar_asset_id)
            if organizer_avatar_asset_id
            else None,
            has_locked_deadlock_roster=bool(int(locked_roster_count)),
        )
        for (
            tournament,
            organizer_display_name,
            organizer_avatar_url,
            organizer_avatar_asset_id,
            participant_count,
            locked_roster_count,
        ) in rows
    ]
    set_pagination_headers(
        response,
        total=total,
        limit=limit,
        offset=offset,
        returned=len(serialized),
    )
    response.headers["Access-Control-Expose-Headers"] = PAGINATION_EXPOSE_HEADERS
    return serialized


@router.get("/mine", response_model=list[TournamentResponse])
async def list_my_tournaments(
    response: Response,
    scope_filter: str | None = Query(
        default=None,
        alias="scope",
        pattern="^(mine|registered)$",
    ),
    search: str | None = Query(default=None, max_length=120),
    rank: list[str] = Query(default_factory=list),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        pattern="^(registration_open|registration_closed|in_progress|completed|cancelled)$",
    ),
    date_sort: str | None = Query(default=None, pattern="^(nearest|farthest)$"),
    limit: int = Query(
        default=TOURNAMENT_LIST_DEFAULT_LIMIT,
        ge=1,
        le=TOURNAMENT_LIST_MAX_LIMIT,
    ),
    offset: int = Query(default=0, ge=0),
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[TournamentResponse]:
    current_participant_status = (
        select(TournamentParticipant.status)
        .where(
            TournamentParticipant.tournament_id == Tournament.id,
            TournamentParticipant.user_id == auth_session.user.id,
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
        )
        .correlate(Tournament)
        .scalar_subquery()
    )
    current_user_has_invite_access = exists(
        select(1).where(
            TournamentInviteAccess.tournament_id == Tournament.id,
            TournamentInviteAccess.user_id == auth_session.user.id,
        )
    )
    filters = [
        Tournament.format_slug == SOLO_TOURNAMENT_FORMAT,
        or_(
            Tournament.organizer_user_id == auth_session.user.id,
            current_participant_status.is_not(None),
            current_user_has_invite_access,
        ),
    ]
    if scope_filter == "mine":
        filters.append(Tournament.organizer_user_id == auth_session.user.id)
    elif scope_filter == "registered":
        filters.append(current_participant_status.is_not(None))

    normalized_search = (search or "").strip()
    if normalized_search:
        search_pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                Tournament.name.ilike(search_pattern),
                User.display_name.ilike(search_pattern),
            )
        )
    if status_filter:
        filters.append(Tournament.status == status_filter)

    allowed_rank_filter = normalize_tournament_allowed_ranks(rank)
    if allowed_rank_filter:
        filters.append(
            or_(
                Tournament.allowed_ranks.is_(None),
                func.json_array_length(Tournament.allowed_ranks) == 0,
                cast(Tournament.allowed_ranks, postgresql.JSONB).has_any(
                    postgresql.array(list(allowed_rank_filter))
                ),
            )
        )

    total = int(
        await db_session.scalar(
            select(func.count())
            .select_from(Tournament)
            .join(User, User.id == Tournament.organizer_user_id)
            .where(*filters)
        )
        or 0
    )
    page_stmt = (
        select(
            Tournament.id.label("id"),
            Tournament.created_at.label("created_at"),
            Tournament.starts_at.label("starts_at"),
            current_participant_status.label("current_user_participant_status"),
            current_user_has_invite_access.label("current_user_has_invite_access"),
        )
        .join(User, User.id == Tournament.organizer_user_id)
        .where(*filters)
    )
    if date_sort == "nearest":
        page_stmt = page_stmt.order_by(
            Tournament.starts_at.asc().nulls_last(),
            Tournament.created_at.desc(),
            Tournament.id.desc(),
        )
    elif date_sort == "farthest":
        page_stmt = page_stmt.order_by(
            Tournament.starts_at.desc().nulls_last(),
            Tournament.created_at.desc(),
            Tournament.id.desc(),
        )
    else:
        page_stmt = page_stmt.order_by(Tournament.created_at.desc(), Tournament.id.desc())
    tournament_page = page_stmt.limit(limit).offset(offset).cte("tournament_page")

    stmt = tournament_with_counts_stmt(tournament_page).add_columns(
        tournament_page.c.current_user_participant_status,
        tournament_page.c.current_user_has_invite_access,
    )
    if date_sort == "nearest":
        stmt = stmt.order_by(
            tournament_page.c.starts_at.asc().nulls_last(),
            tournament_page.c.created_at.desc(),
            tournament_page.c.id.desc(),
        )
    elif date_sort == "farthest":
        stmt = stmt.order_by(
            tournament_page.c.starts_at.desc().nulls_last(),
            tournament_page.c.created_at.desc(),
            tournament_page.c.id.desc(),
        )
    else:
        stmt = stmt.order_by(tournament_page.c.created_at.desc(), tournament_page.c.id.desc())

    rows = (
        await db_session.execute(stmt)
    ).all()
    media_descriptors = await load_media_descriptors(
        db_session,
        tuple(
            asset_id
            for row in rows
            for asset_id in (row[0].banner_asset_id, row[3])
        ),
    )
    serialized = [
        serialize_tournament(
            tournament,
            organizer_display_name,
            int(participant_count),
            organizer_avatar_url=organizer_avatar_url,
            cover_media=media_descriptors.get(tournament.banner_asset_id)
            if tournament.banner_asset_id
            else None,
            organizer_avatar_media=media_descriptors.get(organizer_avatar_asset_id)
            if organizer_avatar_asset_id
            else None,
            has_locked_deadlock_roster=bool(int(locked_roster_count)),
            current_user_participant_status=current_user_participant_status,
            current_user_has_invite_access=bool(current_user_has_invite_access),
        )
        for (
            tournament,
            organizer_display_name,
            organizer_avatar_url,
            organizer_avatar_asset_id,
            participant_count,
            locked_roster_count,
            current_user_participant_status,
            current_user_has_invite_access,
        ) in rows
    ]
    set_pagination_headers(
        response,
        total=total,
        limit=limit,
        offset=offset,
        returned=len(serialized),
    )
    response.headers["Access-Control-Expose-Headers"] = PAGINATION_EXPOSE_HEADERS
    return serialized


@router.post("", response_model=TournamentResponse, status_code=status.HTTP_201_CREATED)
async def create_tournament(
    payload: TournamentCreateRequest,
    request: Request,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentResponse:
    try:
        ensure_supported_tournament_format(payload.format_slug)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    ensure_tournament_schedule_is_future(payload, now=auth_session.now)
    idempotency = await reserve_mutation_idempotency(
        db_session,
        actor_user_id=auth_session.user.id,
        scope="tournament.create",
        key=request_idempotency_key(request),
        request_fingerprint=mutation_payload_fingerprint(
            payload.model_dump(mode="json")
        ),
    )
    if idempotency is not None and idempotency.replay:
        resource_id = idempotency.record.resource_id
        if resource_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The idempotent request completed without a resource reference.",
            )
        existing_tournament = await db_session.scalar(
            select(Tournament).where(Tournament.id == resource_id)
        )
        if existing_tournament is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The resource for this Idempotency-Key no longer exists.",
            )
        return serialize_tournament(
            existing_tournament,
            auth_session.user.display_name,
            await participant_count_for_tournament(
                db_session, tournament_id=existing_tournament.id
            ),
        )

    normalized_name = await lock_tournament_name(db_session, name=payload.name)
    if await public_tournament_name_exists(
        db_session,
        normalized_name=normalized_name,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Турнир с таким публичным названием уже существует.",
        )
    creation_allowance_source = "admin_public"
    creation_allowance_remaining: int | None = None
    if payload.visibility == "invite_only" or not auth_session_has_admin_role(auth_session):
        creation_allowance_source, creation_allowance_remaining = await consume_tournament_creation_allowance(
            db_session,
            organizer_user_id=auth_session.user.id,
            visibility=payload.visibility,
            now=auth_session.now,
        )

    allowed_ranks = list(normalize_tournament_allowed_ranks(payload.allowed_ranks))
    try:
        requested_teams_count = normalize_requested_teams_count(payload.teams_count)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    slug = await unique_slug_from_name(db_session, Tournament, payload.name)
    has_automation_schedule = payload.ready_check_starts_at is not None
    tournament = Tournament(
        slug=slug,
        name=payload.name.strip(),
        description=(payload.description or "").strip() or None,
        cover_url=(payload.cover_url or "").strip() or DEFAULT_TOURNAMENT_COVER_URL,
        visibility=payload.visibility,
        status="registration_open" if has_automation_schedule and (
            payload.registration_starts_at is None
            or payload.registration_starts_at <= auth_session.now
        ) else "registration_closed",
        format_slug=payload.format_slug,
        allowed_ranks=allowed_ranks,
        max_participants=payload.max_participants,
        registration_starts_at=payload.registration_starts_at,
        registration_closes_at=payload.registration_closes_at,
        ready_check_starts_at=payload.ready_check_starts_at,
        ready_check_ends_at=payload.ready_check_ends_at,
        captain_selection_starts_at=payload.captain_selection_starts_at,
        starts_at=payload.starts_at,
        match_format=payload.match_format,
        final_format=payload.final_format,
        captain_response_deadline_minutes=payload.captain_response_deadline_minutes,
        teams_count=requested_teams_count,
        organizer_user_id=auth_session.user.id,
    )
    db_session.add(tournament)
    try:
        await db_session.flush()
    except IntegrityError as exc:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Турнир с таким публичным названием уже существует.",
        ) from exc
    bind_mutation_idempotency_resource(idempotency, tournament.id)
    invite_code = normalize_invite_code(payload.invite_code or "")
    if payload.invite_code is not None and len(invite_code) < 10:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invite code must contain at least 10 letters or digits.",
        )
    if not invite_code:
        invite_code = await generate_unique_invite_code(db_session)
    if not await invite_code_is_available(db_session, invite_code):
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Invite code is already taken.",
        )
    invite = TournamentInvite(
        tournament_id=tournament.id,
        code=invite_code,
        note="Automatic invite code",
        max_uses=min(payload.max_participants or 99_999, 99_999),
        use_count=0,
        expires_at=None,
        created_by_user_id=auth_session.user.id,
    )
    db_session.add(invite)
    try:
        await db_session.flush()
    except IntegrityError as exc:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Invite code is already taken.",
        ) from exc
    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.create",
        subject_type="tournament",
        subject_id=tournament.id,
        payload={
            "name": tournament.name,
            "slug": tournament.slug,
            "cover_url": tournament.cover_url,
            "visibility": tournament.visibility,
            "format_slug": tournament.format_slug,
            "allowed_ranks": list(tournament.allowed_ranks or []),
            "max_participants": tournament.max_participants,
            "registration_starts_at": tournament.registration_starts_at.isoformat()
            if tournament.registration_starts_at is not None
            else None,
            "registration_closes_at": tournament.registration_closes_at.isoformat()
            if tournament.registration_closes_at is not None
            else None,
            "ready_check_starts_at": tournament.ready_check_starts_at.isoformat()
            if tournament.ready_check_starts_at is not None
            else None,
            "ready_check_ends_at": tournament.ready_check_ends_at.isoformat()
            if tournament.ready_check_ends_at is not None
            else None,
            "captain_selection_starts_at": tournament.captain_selection_starts_at.isoformat()
            if tournament.captain_selection_starts_at is not None
            else None,
            "starts_at": tournament.starts_at.isoformat()
            if tournament.starts_at is not None
            else None,
            "match_format": tournament.match_format,
            "final_format": tournament.final_format,
            "captain_response_deadline_minutes": tournament.captain_response_deadline_minutes,
            "teams_count": tournament.teams_count,
            "automatic_invite_id": invite.id if invite is not None else None,
            "creation_allowance_source": creation_allowance_source,
            "creation_allowance_remaining": creation_allowance_remaining,
        },
    )
    await db_session.commit()
    await db_session.refresh(tournament)
    return serialize_tournament(tournament, auth_session.user.display_name, 0)


@router.get("/invites/suggest-code", response_model=TournamentInviteCodeAvailabilityResponse)
async def suggest_tournament_invite_code(
    request: Request,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentInviteCodeAvailabilityResponse:
    await check_invite_rate_limit(
        request,
        user_id=auth_session.user.id,
        operation="lookup",
    )
    return TournamentInviteCodeAvailabilityResponse(
        code=await generate_unique_invite_code(db_session),
        available=True,
    )


@router.get("/invites/code-status", response_model=TournamentInviteCodeAvailabilityResponse)
async def get_tournament_invite_code_status(
    request: Request,
    code: str = Query(min_length=1, max_length=64),
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentInviteCodeAvailabilityResponse:
    await check_invite_rate_limit(
        request,
        user_id=auth_session.user.id,
        operation="lookup",
    )
    normalized_code = normalize_invite_code(code)
    return TournamentInviteCodeAvailabilityResponse(
        code=normalized_code,
        available=await invite_code_is_available(db_session, normalized_code),
    )


@router.post(
    "/{slug}/cover",
    response_model=MediaAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
)
@router.post(
    "/{slug}/banner",
    response_model=MediaAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_tournament_banner(
    slug: str,
    request: Request,
    file: UploadFile = File(...),
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> MediaAcceptedResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_manager(auth_session, tournament)
    try:
        await check_media_upload_rate_limit(
            request,
            user_id=auth_session.user.id,
            upload_bytes=upload_size_hint(request, file),
        )
        service = api_media_service(db_session)

        async def audit_acceptance(
            staged: StagedSource,
            superseded_asset_ids: tuple[str, ...],
        ) -> None:
            await write_audit_log(
                db_session,
                actor_user_id=auth_session.user.id,
                action="tournament.banner.upload.accepted",
                subject_type="tournament",
                subject_id=tournament.id,
                payload={
                    "asset_id": staged.asset_id,
                    "tournament_slug": tournament.slug,
                    "content_type": staged.mime_type,
                    "size": staged.byte_size,
                    "superseded_asset_ids": list(superseded_asset_ids),
                },
            )

        accepted = await service.accept_upload(
            chunks=upload_file_chunks(file),
            declared_mime=file.content_type,
            purpose="tournament_banner",
            tournament_id=tournament.id,
            enqueue=enqueue_media_asset,
            before_commit=audit_acceptance,
        )
    except MediaError as exc:
        raise_media_http_error(exc)
    finally:
        await file.close()
    return accepted_media_response(accepted)


@router.delete(
    "/{slug}/cover",
    response_model=MediaDeleteAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
)
@router.delete(
    "/{slug}/banner",
    response_model=MediaDeleteAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_tournament_banner(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> MediaDeleteAcceptedResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_manager(auth_session, tournament)
    service = api_media_service(db_session)

    async def audit_unlink(asset_id: str | None) -> None:
        await write_audit_log(
            db_session,
            actor_user_id=auth_session.user.id,
            action="tournament.banner.delete.accepted",
            subject_type="tournament",
            subject_id=tournament.id,
            payload={"asset_id": asset_id, "tournament_slug": tournament.slug},
        )

    asset_id = await service.unlink_active(
        purpose="tournament_banner",
        owner_id=tournament.id,
        before_commit=audit_unlink,
    )
    return MediaDeleteAcceptedResponse(
        asset_id=asset_id,
        status="cleanup_pending" if asset_id else "deleted",
    )


@router.post("/invites/claim", response_model=TournamentInviteRedeemResponse, status_code=status.HTTP_201_CREATED)
async def redeem_tournament_invite(
    payload: TournamentInviteClaimRequest,
    request: Request,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentInviteRedeemResponse:
    await check_invite_rate_limit(
        request,
        user_id=auth_session.user.id,
        operation="claim",
    )
    code = normalize_invite_code(payload.code)
    row = (
        await db_session.execute(
            tournament_with_counts_stmt()
            .join(TournamentInvite, TournamentInvite.tournament_id == Tournament.id)
            .where(TournamentInvite.code == code)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite code was not found.")
    (
        tournament,
        organizer_display_name,
        organizer_avatar_url,
        organizer_avatar_asset_id,
        participant_count,
        locked_roster_count,
    ) = row
    invite = await db_session.scalar(
        select(TournamentInvite).where(
            TournamentInvite.tournament_id == tournament.id,
            TournamentInvite.code == code,
        )
    )
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite code was not found.")

    participant = await participant_for_user(
        db_session,
        tournament_id=tournament.id,
        user_id=auth_session.user.id,
    )
    access = await invite_access_for_user(
        db_session,
        tournament_id=tournament.id,
        user_id=auth_session.user.id,
    )
    if access is None:
        try:
            ensure_invite_claimable(
                tournament_visibility=tournament.visibility,
                tournament_status=tournament.status,
                max_uses=invite.max_uses,
                use_count=invite.use_count,
                revoked_at=invite.revoked_at,
                expires_at=invite.expires_at,
                now=auth_session.now,
            )
        except TournamentWorkflowError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        access = TournamentInviteAccess(
            tournament_id=tournament.id,
            user_id=auth_session.user.id,
            invite_id=invite.id,
            claimed_at=auth_session.now,
        )
        db_session.add(access)
        invite.use_count += 1
        invite.last_claimed_at = auth_session.now
        invite.last_claimed_by_user_id = auth_session.user.id
        await db_session.flush()
        await write_audit_log(
            db_session,
            actor_user_id=auth_session.user.id,
            action="tournament.invite.access_grant",
            subject_type="tournament_invite",
            subject_id=invite.id,
            payload={
                "tournament_slug": tournament.slug,
                "access_id": access.id,
            },
        )
    await db_session.commit()
    await db_session.refresh(invite)
    await db_session.refresh(tournament)
    cover_media, organizer_avatar_media = await tournament_media_descriptors(
        db_session,
        tournament,
        organizer_avatar_asset_id=organizer_avatar_asset_id,
    )
    return TournamentInviteRedeemResponse(
        tournament=serialize_tournament(
            tournament,
            organizer_display_name,
            int(participant_count),
            organizer_avatar_url=organizer_avatar_url,
            cover_media=cover_media,
            organizer_avatar_media=organizer_avatar_media,
            has_locked_deadlock_roster=bool(int(locked_roster_count)),
            current_user_participant_status=participant.status if participant is not None else None,
            current_user_has_invite_access=True,
        ),
        participant=serialize_participant(participant, auth_session.user.display_name)
        if participant is not None
        else None,
        invite=serialize_invite(tournament, invite, now=datetime.now(UTC)),
    )


@router.patch("/{slug}/status", response_model=TournamentResponse)
async def update_tournament_status(
    slug: str,
    payload: TournamentStatusUpdateRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_organizer(auth_session, tournament)
    try:
        transition = await transition_tournament_status(
            db_session,
            tournament_id=tournament.id,
            next_status=payload.status,
            now=auth_session.now,
            actor_user_id=auth_session.user.id,
            audit_action="tournament.status.update",
        )
    except TournamentStatusTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except TournamentCompletionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    tournament = transition.tournament
    await db_session.commit()
    invalidate_tournament_runtime_caches(tournament.id)
    await db_session.refresh(tournament)
    cover_media, organizer_avatar_media = await tournament_media_descriptors(
        db_session,
        tournament,
    )
    return serialize_tournament(
        tournament,
        auth_session.user.display_name,
        await participant_count_for_tournament(db_session, tournament.id),
        cover_media=cover_media,
        organizer_avatar_media=organizer_avatar_media,
        has_locked_deadlock_roster=transition.has_locked_deadlock_roster,
    )


@router.get("/{slug}/participants", response_model=list[TournamentParticipantResponse])
async def list_tournament_participants(
    slug: str,
    response: Response,
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(
        default=PARTICIPANT_LIST_DEFAULT_LIMIT,
        ge=1,
        le=PARTICIPANT_LIST_MAX_LIMIT,
    ),
    offset: int = Query(default=0, ge=0),
    auth_session=Depends(get_optional_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[TournamentParticipantResponse]:
    tournament = await get_tournament_or_404(db_session, slug)
    has_participant_record = False
    if auth_session is not None:
        has_participant_record = (
            await participant_for_user(
                db_session,
                tournament_id=tournament.id,
                user_id=auth_session.user.id,
            )
        ) is not None
    ensure_tournament_workspace_visible(
        tournament,
        auth_session=auth_session,
        has_participant_record=has_participant_record,
    )

    serialized, total, _ = await tournament_participant_page(
        db_session,
        tournament_id=tournament.id,
        search=search,
        limit=limit,
        offset=offset,
    )
    set_pagination_headers(
        response,
        total=total,
        limit=limit,
        offset=offset,
        returned=len(serialized),
    )
    return serialized


@router.post(
    "/{slug}/participants/manage",
    response_model=TournamentParticipantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def organizer_add_participant(
    slug: str,
    payload: TournamentParticipantManageRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentParticipantResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_organizer(auth_session, tournament)
    try:
        ensure_organizer_can_manage_participants(tournament.status)
        ensure_deadlock_registration_changes_allowed(
            format_slug=tournament.format_slug,
            has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                db_session,
                tournament=tournament,
            ),
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    user_email = payload.user_email.lower()
    target_user = await db_session.scalar(select(User).where(User.email == user_email))
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User with this email was not found.",
        )

    await ensure_no_existing_participant(
        db_session,
        tournament_id=tournament.id,
        user_id=target_user.id,
    )
    team_name = normalized_team_name(payload.entry_type, payload.team_name)
    await ensure_participant_join_limits(
        db_session,
        tournament=tournament,
        user_id=target_user.id,
    )
    try:
        participant = await create_participant(
            db_session,
            tournament=tournament,
            user=target_user,
            entry_type=payload.entry_type,
            team_name=team_name,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.participant.manage_add",
        subject_type="tournament_participant",
        subject_id=participant.id,
        payload={
            "tournament_slug": tournament.slug,
            "user_id": target_user.id,
            "user_email": target_user.email,
            "entry_type": participant.entry_type,
        },
    )
    await db_session.commit()
    invalidate_tournament_runtime_caches(tournament.id)
    await db_session.refresh(participant)
    return serialize_participant(participant, target_user.display_name)


@router.delete("/{slug}/participants/{participant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def organizer_remove_participant(
    slug: str,
    participant_id: str,
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_organizer(auth_session, tournament)
    try:
        ensure_organizer_can_moderate_participants(tournament.status)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    participant = await get_participant_or_404(
        db_session,
        tournament_id=tournament.id,
        participant_id=participant_id,
    )
    await db_session.execute(
        select(User.id).where(User.id == participant.user_id).with_for_update()
    )
    previous_status = participant.status
    try:
        participant.status = transition_participant_status(
            participant.status,
            "disqualified",
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    participant.moderation_note = "Removed by organizer."
    participant.moderated_at = auth_session.now
    participant.moderated_by_user_id = auth_session.user.id
    if (
        is_solo_tournament_format(tournament.format_slug)
        and not participant_status_is_inactive(previous_status)
    ):
        await prune_participant_from_active_ready_round(
            db_session,
            tournament=tournament,
            user_id=participant.user_id,
            actor_user_id=auth_session.user.id,
            now=auth_session.now,
            participant_status=participant.status,
        )
        await prune_participant_from_active_captain_round(
            db_session,
            tournament=tournament,
            user_id=participant.user_id,
            actor_user_id=auth_session.user.id,
            now=auth_session.now,
            participant_status=participant.status,
        )
        await release_active_commitments(
            db_session,
            tournament_id=tournament.id,
            user_ids=[participant.user_id],
            released_at=auth_session.now,
            release_reason="participant_disqualified",
        )

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.participant.manage_remove",
        subject_type="tournament_participant",
        subject_id=participant.id,
        payload={
            "tournament_slug": tournament.slug,
            "user_id": participant.user_id,
            "from_status": previous_status,
            "to_status": participant.status,
            "retained_record": True,
        },
    )
    await db_session.commit()
    invalidate_tournament_runtime_caches(tournament.id)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.patch(
    "/{slug}/participants/{participant_id}/moderation",
    response_model=TournamentParticipantResponse,
)
async def organizer_moderate_participant(
    slug: str,
    participant_id: str,
    payload: TournamentParticipantModerationRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentParticipantResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_organizer(auth_session, tournament)
    try:
        ensure_organizer_can_moderate_participants(tournament.status)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    participant = await get_participant_or_404(
        db_session,
        tournament_id=tournament.id,
        participant_id=participant_id,
    )
    previous_status = participant.status
    previous_note = participant.moderation_note
    next_note = (payload.moderation_note or "").strip() or None

    restoring_inactive_participant = (
        participant_status_is_inactive(previous_status)
        and not participant_status_is_inactive(payload.status)
    )

    # The write dependency already owns Tournament's row lock. Keep the
    # secondary lock order as Tournament -> User -> workflow rows before
    # checking the locked roster or mutating participant workflow state.
    await db_session.execute(
        select(User.id).where(User.id == participant.user_id).with_for_update()
    )

    if restoring_inactive_participant:
        try:
            ensure_participant_restoration_allowed(
                tournament_status=tournament.status,
                has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                    db_session,
                    tournament=tournament,
                ),
            )
        except TournamentWorkflowError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        try:
            await claim_slot_for_existing_participant(
                db_session,
                tournament_id=tournament.id,
                participant=participant,
                max_participants=tournament.max_participants,
                claimed_at=auth_session.now,
            )
        except TournamentWorkflowError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

    try:
        participant.status = transition_participant_status(participant.status, payload.status)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    if participant.status == previous_status and next_note == previous_note:
        return serialize_participant(
            participant,
            await participant_display_name_for_user(db_session, participant.user_id),
        )

    participant.moderation_note = next_note
    participant.moderated_at = auth_session.now
    participant.moderated_by_user_id = auth_session.user.id
    if (
        is_solo_tournament_format(tournament.format_slug)
        and participant_status_is_inactive(participant.status)
        and not participant_status_is_inactive(previous_status)
    ):
        await prune_participant_from_active_ready_round(
            db_session,
            tournament=tournament,
            user_id=participant.user_id,
            actor_user_id=auth_session.user.id,
            now=auth_session.now,
            participant_status=participant.status,
        )
        await prune_participant_from_active_captain_round(
            db_session,
            tournament=tournament,
            user_id=participant.user_id,
            actor_user_id=auth_session.user.id,
            now=auth_session.now,
            participant_status=participant.status,
        )
        await release_active_commitments(
            db_session,
            tournament_id=tournament.id,
            user_ids=[participant.user_id],
            released_at=auth_session.now,
            release_reason=f"participant_{participant.status}",
        )

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.participant.moderate",
        subject_type="tournament_participant",
        subject_id=participant.id,
        payload={
            "tournament_slug": tournament.slug,
            "user_id": participant.user_id,
            "from_status": previous_status,
            "to_status": participant.status,
            "moderation_note": participant.moderation_note,
        },
    )
    await db_session.commit()
    invalidate_tournament_runtime_caches(tournament.id)
    await db_session.refresh(participant)
    return serialize_participant(
        participant,
        await participant_display_name_for_user(db_session, participant.user_id),
    )


@router.get("/{slug}/invites", response_model=list[TournamentInviteResponse])
async def list_tournament_invites(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[TournamentInviteResponse]:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_organizer(auth_session, tournament)

    rows = await db_session.scalars(
        select(TournamentInvite)
        .where(TournamentInvite.tournament_id == tournament.id)
        .order_by(TournamentInvite.created_at.desc())
    )
    now = datetime.now(UTC)
    return [serialize_invite(tournament, invite, now=now) for invite in rows]


@router.post("/{slug}/invites", response_model=TournamentInviteResponse, status_code=status.HTTP_201_CREATED)
async def create_tournament_invite(
    slug: str,
    payload: TournamentInviteCreateRequest,
    request: Request,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentInviteResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_organizer(auth_session, tournament)
    idempotency = await reserve_mutation_idempotency(
        db_session,
        actor_user_id=auth_session.user.id,
        scope=f"tournament.invite.create:{tournament.id}",
        key=request_idempotency_key(request),
        request_fingerprint=mutation_payload_fingerprint(
            {
                "tournament_id": tournament.id,
                "payload": payload.model_dump(mode="json"),
            }
        ),
    )
    if idempotency is not None and idempotency.replay:
        resource_id = idempotency.record.resource_id
        if resource_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The idempotent request completed without a resource reference.",
            )
        existing_invite = await db_session.scalar(
            select(TournamentInvite).where(
                TournamentInvite.id == resource_id,
                TournamentInvite.tournament_id == tournament.id,
            )
        )
        if existing_invite is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The resource for this Idempotency-Key no longer exists.",
            )
        return serialize_invite(tournament, existing_invite, now=datetime.now(UTC))

    await check_invite_rate_limit(
        request,
        user_id=auth_session.user.id,
        operation="manage",
    )
    try:
        ensure_organizer_can_manage_participants(tournament.status)
        ensure_deadlock_registration_changes_allowed(
            format_slug=tournament.format_slug,
            has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                db_session,
                tournament=tournament,
            ),
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    invite = TournamentInvite(
        tournament_id=tournament.id,
        code=await generate_unique_invite_code(db_session),
        note=(payload.note or "").strip() or None,
        max_uses=payload.max_uses,
        use_count=0,
        expires_at=payload.expires_at,
        created_by_user_id=auth_session.user.id,
    )
    db_session.add(invite)
    await db_session.flush()
    bind_mutation_idempotency_resource(idempotency, invite.id)
    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.invite.create",
        subject_type="tournament_invite",
        subject_id=invite.id,
        payload={
            "tournament_slug": tournament.slug,
            "code_length": len(invite.code),
            "max_uses": invite.max_uses,
        },
    )
    await db_session.commit()
    await db_session.refresh(invite)
    return serialize_invite(tournament, invite, now=datetime.now(UTC))


@router.delete("/{slug}/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_tournament_invite(
    slug: str,
    invite_id: str,
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_organizer(auth_session, tournament)
    invite = await get_invite_or_404(
        db_session,
        tournament_id=tournament.id,
        invite_id=invite_id,
    )

    if invite.revoked_at is None:
        invite.revoked_at = auth_session.now
        await write_audit_log(
            db_session,
            actor_user_id=auth_session.user.id,
            action="tournament.invite.revoke",
            subject_type="tournament_invite",
            subject_id=invite.id,
            payload={
                "tournament_slug": tournament.slug,
                "code_length": len(invite.code),
            },
        )
        await db_session.commit()

    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/{slug}/matches", response_model=list[TournamentMatchResponse])
async def list_tournament_matches(
    slug: str,
    auth_session=Depends(get_optional_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[TournamentMatchResponse]:
    tournament = await get_tournament_or_404(db_session, slug)
    has_participant_record = False
    if auth_session is not None:
        has_participant_record = (
            await participant_for_user(
                db_session,
                tournament_id=tournament.id,
                user_id=auth_session.user.id,
            )
        ) is not None
    ensure_tournament_workspace_visible(
        tournament,
        auth_session=auth_session,
        has_participant_record=has_participant_record,
    )
    rows = await tournament_matches_in_order(db_session, tournament_id=tournament.id)
    if not rows:
        return []
    latest_round_number = rows[-1].round_number
    return [
        serialize_match(
            match,
            tournament_status=tournament.status,
            latest_round_number=latest_round_number,
        )
        for match in rows
    ]


@router.get("/{slug}/bracket", response_model=TournamentBracketResponse)
async def get_tournament_bracket(
    slug: str,
    request: Request,
    response: Response,
    auth_session=Depends(get_optional_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
    teams_view: Literal["full", "summary"] = Query(default="full"),
) -> TournamentBracketResponse | Response:
    tournament = await get_tournament_or_404(db_session, slug)
    etag = _representation_etag(
        "bracket",
        tournament.id,
        tournament.updated_at,
        tournament.bracket_revision,
        teams_view,
        auth_session.user.id if auth_session is not None else "anonymous",
        tuple(sorted(auth_session.role_slugs)) if auth_session is not None else (),
    )
    not_modified = _conditional_response(request, response, etag=etag)
    if not_modified is not None:
        return not_modified
    has_participant_record = False
    if auth_session is not None and tournament.visibility == "invite_only":
        participant_record = await participant_for_user(
            db_session,
            tournament_id=tournament.id,
            user_id=auth_session.user.id,
        )
        has_participant_record = participant_record is not None
    bracket = await build_tournament_bracket_response(
        db_session,
        tournament=tournament,
        auth_session=auth_session,
        has_participant_record=has_participant_record,
        include_team_members=teams_view == "full",
    )
    return bracket


@router.post(
    "/{slug}/matches/seed-opening-round",
    response_model=list[TournamentMatchResponse],
    status_code=status.HTTP_201_CREATED,
)
async def seed_deadlock_opening_round_matches(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[TournamentMatchResponse]:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_deadlock_tournament_format(tournament)
    ensure_tournament_manager(auth_session, tournament)
    has_locked_deadlock_roster = await tournament_has_locked_deadlock_roster(
        db_session,
        tournament=tournament,
    )
    try:
        ensure_deadlock_match_staging_allowed(
            format_slug=tournament.format_slug,
            has_locked_deadlock_roster=has_locked_deadlock_roster,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    existing_match_count = await db_session.scalar(
        select(func.count()).select_from(TournamentMatch).where(TournamentMatch.tournament_id == tournament.id)
    )
    if existing_match_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Matches already exist for this tournament. Seed the bracket only before any manual staging.",
        )

    locked_run = await deadlock_locked_auto_assignment_run_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    if locked_run is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lock a Deadlock roster before seeding the opening round.",
        )

    tournament = await lock_tournament_for_bracket(db_session, tournament.id)
    existing_match_count = await db_session.scalar(
        select(func.count())
        .select_from(TournamentMatch)
        .where(TournamentMatch.tournament_id == tournament.id)
    )
    if existing_match_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Matches already exist for this tournament. Seed the bracket only once.",
        )
    try:
        created_matches, opening_matches = await create_full_bracket_graph(
            db_session,
            tournament=tournament,
            locked_run=locked_run,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="match.seed_opening_round",
        subject_type="tournament",
        subject_id=tournament.id,
        payload={
            "tournament_slug": tournament.slug,
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
    await db_session.commit()
    for match in opening_matches:
        await db_session.refresh(match)
    latest_round_number = max(match.round_number for match in created_matches)
    response = [
        serialize_match(
            match,
            tournament_status=tournament.status,
            latest_round_number=latest_round_number,
        )
        for match in opening_matches
    ]
    return response


@router.post(
    "/{slug}/matches/seed-next-round",
    response_model=list[TournamentMatchResponse],
    status_code=status.HTTP_201_CREATED,
)
async def seed_next_round_matches(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[TournamentMatchResponse]:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_manager(auth_session, tournament)
    tournament = await lock_tournament_for_bracket(db_session, tournament.id)

    if tournament.status in {"completed", "cancelled"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Bracket progression is unavailable after the tournament is completed or cancelled.",
        )

    match_rows = (
        await db_session.scalars(
            select(TournamentMatch)
            .where(TournamentMatch.tournament_id == tournament.id)
            .order_by(
                TournamentMatch.round_number.asc(),
                TournamentMatch.sequence_number.asc(),
                TournamentMatch.created_at.asc(),
            )
        )
    ).all()
    if not match_rows:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Create or seed the opening round before generating the next round.",
        )

    rounds = sorted({match.round_number for match in match_rows})
    source_round_number = None
    for round_number in rounds:
        round_matches = [
            match for match in match_rows if match.round_number == round_number
        ]
        if len(round_matches) > 1 and all(
            match.status == "completed" for match in round_matches
        ):
            source_round_number = round_number
    if source_round_number is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Complete every match in the current round before advancing the bracket.",
        )

    winner_labels: list[str] = []
    source_round_matches = [
        match
        for match in match_rows
        if match.round_number == source_round_number
    ]
    for match in source_round_matches:
        winner_label = winner_label_for_match(match)
        if winner_label is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Winner labels are missing for one or more completed matches in round {source_round_number}.",
            )
        winner_labels.append(winner_label)

    existing_next_round = [
        match
        for match in match_rows
        if match.round_number == source_round_number + 1
    ]
    if existing_next_round:
        return [
            serialize_match(
                match,
                tournament_status=tournament.status,
                latest_round_number=max(rounds),
            )
            for match in existing_next_round
        ]

    try:
        next_round_drafts = build_next_round_matches(
            source_round_number,
            winner_labels,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    created_matches: list[TournamentMatch] = []
    for index, draft in enumerate(next_round_drafts):
        home_source = source_round_matches[index * 2]
        away_source = source_round_matches[index * 2 + 1]
        match = TournamentMatch(
            tournament_id=tournament.id,
            title=draft.title,
            round_number=draft.round_number,
            sequence_number=draft.sequence_number,
            home_label=draft.home_label,
            away_label=draft.away_label,
            home_team_id=home_source.winner_team_id,
            away_team_id=away_source.winner_team_id,
            home_source_match_id=home_source.id,
            away_source_match_id=away_source.id,
            status="scheduled",
        )
        db_session.add(match)
        created_matches.append(match)

    try:
        await db_session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A next-round match already exists for one of the generated bracket slots.",
        ) from exc

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="match.seed_next_round",
        subject_type="tournament",
        subject_id=tournament.id,
        payload={
            "tournament_slug": tournament.slug,
            "source_round_number": source_round_number,
            "match_count": len(created_matches),
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
    advance_revision(tournament)
    await db_session.commit()
    for match in created_matches:
        await db_session.refresh(match)
    latest_round_number = max(match.round_number for match in created_matches)
    response = [
        serialize_match(
            match,
            tournament_status=tournament.status,
            latest_round_number=latest_round_number,
        )
        for match in created_matches
    ]
    return response


@router.post("/{slug}/matches", response_model=TournamentMatchResponse, status_code=status.HTTP_201_CREATED)
async def create_tournament_match(
    slug: str,
    payload: TournamentMatchCreateRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentMatchResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_manager(auth_session, tournament)
    tournament = await lock_tournament_for_bracket(db_session, tournament.id)
    has_locked_deadlock_roster = await tournament_has_locked_deadlock_roster(
        db_session,
        tournament=tournament,
    )

    try:
        ensure_deadlock_match_staging_allowed(
            format_slug=tournament.format_slug,
            has_locked_deadlock_roster=has_locked_deadlock_roster,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if tournament.status in {"completed", "cancelled"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Matches cannot be added after the tournament is completed or cancelled.",
        )

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
    try:
        ensure_match_round_staging_allowed(
            payload.round_number,
            existing_match_states,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    home_label = payload.home_label.strip()
    away_label = payload.away_label.strip()
    ensure_distinct_match_sides(home_label, away_label)
    locked_run = await deadlock_locked_auto_assignment_run_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    try:
        home_team_id, away_team_id = ensure_match_team_ids_are_locked(
            home_team_id=deadlock_team_id_from_match_label(home_label),
            away_team_id=deadlock_team_id_from_match_label(away_label),
            locked_team_ids={
                team_id
                for label in locked_deadlock_team_labels_from_run(locked_run)
                if (team_id := deadlock_team_id_from_match_label(label)) is not None
            }
            if locked_run is not None
            else set(),
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    match = TournamentMatch(
        tournament_id=tournament.id,
        title=(payload.title or "").strip() or None,
        round_number=payload.round_number,
        sequence_number=payload.sequence_number,
        home_label=home_label,
        away_label=away_label,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        scheduled_at=payload.scheduled_at,
        status="scheduled",
    )
    db_session.add(match)
    try:
        await db_session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A match already exists for this round and slot.",
        ) from exc

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="match.create",
        subject_type="tournament_match",
        subject_id=match.id,
        payload={
            "tournament_slug": tournament.slug,
            "round_number": match.round_number,
            "sequence_number": match.sequence_number,
        },
    )
    advance_revision(tournament)
    await db_session.commit()
    await db_session.refresh(match)
    response = await serialize_single_match_for_tournament(
        db_session,
        tournament=tournament,
        match=match,
    )
    return response


@router.patch("/{slug}/matches/{match_id}/status", response_model=TournamentMatchResponse)
async def update_tournament_match_status(
    slug: str,
    match_id: str,
    payload: TournamentMatchStatusUpdateRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentMatchResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_manager(auth_session, tournament)
    tournament = await lock_tournament_for_bracket(db_session, tournament.id)
    try:
        ensure_expected_revision(tournament, payload.expected_revision)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    match = await get_match_or_404(db_session, tournament_id=tournament.id, match_id=match_id)

    try:
        ensure_match_admin_actions_allowed(tournament.status)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if payload.status == "live" and tournament.status != "in_progress":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tournament must be in progress before a match can go live.",
        )
    if payload.status == "live" and (not match.home_team_id or not match.away_team_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Both qualified teams must be known before a match can go live.",
        )

    current_status = match.status
    if current_status == "completed" and payload.status == "scheduled":
        eliminated_team_id = eliminated_team_id_for_single_elimination(
            home_team_id=match.home_team_id,
            away_team_id=match.away_team_id,
            winner_team_id=match.winner_team_id,
        )
        locked_run = await deadlock_locked_auto_assignment_run_for_tournament(
            db_session,
            tournament_id=tournament.id,
        )
        if locked_run is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The locked roster for this match is missing.",
            )
        try:
            reactivated_commitments = await reactivate_team_commitments(
                db_session,
                run_row=locked_run,
                team_id=eliminated_team_id,
                activated_at=auth_session.now,
            )
        except PlayerCommitmentConflict as exc:
            conflicts = ", ".join(
                f"{item.team_name} / {item.tournament_name}" for item in exc.commitments
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "The result cannot be reopened because an eliminated player is now "
                    f"committed elsewhere: {conflicts}."
                ),
            ) from exc
        try:
            await clear_match_result_and_progression(
                db_session,
                match=match,
            )
        except TournamentWorkflowError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    else:
        try:
            match.status = transition_match_status(current_status, payload.status)
        except TournamentWorkflowError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    if match.status == "scheduled":
        match.home_score = None
        match.away_score = None
        match.winner_side = None
        match.winner_team_id = None
        match.report_note = None
        match.reported_at = None
        match.reported_by_user_id = None

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="match.status.update",
        subject_type="tournament_match",
        subject_id=match.id,
        payload={
            "tournament_slug": tournament.slug,
            "from_status": current_status,
            "to_status": match.status,
            "reactivated_commitments": (
                reactivated_commitments
                if current_status == "completed" and payload.status == "scheduled"
                else 0
            ),
        },
    )
    advance_revision(tournament)
    await db_session.commit()
    await db_session.refresh(match)
    response = await serialize_single_match_for_tournament(
        db_session,
        tournament=tournament,
        match=match,
    )
    return response


@router.patch("/{slug}/matches/{match_id}/schedule", response_model=TournamentMatchResponse)
async def update_tournament_match_schedule(
    slug: str,
    match_id: str,
    payload: TournamentMatchScheduleUpdateRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentMatchResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_manager(auth_session, tournament)
    tournament = await lock_tournament_for_bracket(db_session, tournament.id)
    try:
        ensure_expected_revision(tournament, payload.expected_revision)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    match = await get_match_or_404(db_session, tournament_id=tournament.id, match_id=match_id)

    try:
        ensure_match_admin_actions_allowed(tournament.status)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    source_match_ids = {
        source_id
        for source_id in (match.home_source_match_id, match.away_source_match_id)
        if source_id is not None
    }
    schedule_rows = (
        await db_session.execute(
            select(
                TournamentMatch.id,
                TournamentMatch.scheduled_at,
                TournamentMatch.home_source_match_id,
                TournamentMatch.away_source_match_id,
            ).where(
                TournamentMatch.tournament_id == tournament.id,
                or_(
                    TournamentMatch.id.in_(source_match_ids),
                    TournamentMatch.home_source_match_id == match.id,
                    TournamentMatch.away_source_match_id == match.id,
                ),
            )
        )
    ).all()
    source_scheduled_at = [
        row.scheduled_at for row in schedule_rows if row.id in source_match_ids
    ]
    dependent_scheduled_at = [
        row.scheduled_at
        for row in schedule_rows
        if row.home_source_match_id == match.id or row.away_source_match_id == match.id
    ]
    try:
        ensure_match_schedule_allowed(
            scheduled_at=payload.scheduled_at,
            now=auth_session.now,
            source_scheduled_at=source_scheduled_at,
            dependent_scheduled_at=dependent_scheduled_at,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    previous_scheduled_at = match.scheduled_at
    match.scheduled_at = payload.scheduled_at
    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="match.schedule.update",
        subject_type="tournament_match",
        subject_id=match.id,
        payload={
            "tournament_slug": tournament.slug,
            "from_scheduled_at": previous_scheduled_at.isoformat() if previous_scheduled_at else None,
            "to_scheduled_at": payload.scheduled_at.isoformat() if payload.scheduled_at else None,
        },
    )
    advance_revision(tournament)
    await db_session.commit()
    await db_session.refresh(match)
    response = await serialize_single_match_for_tournament(
        db_session,
        tournament=tournament,
        match=match,
    )
    return response


@router.post("/{slug}/matches/{match_id}/report", response_model=TournamentMatchResponse)
async def report_tournament_match(
    slug: str,
    match_id: str,
    payload: TournamentMatchReportRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentMatchResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_manager(auth_session, tournament)
    tournament = await lock_tournament_for_bracket(db_session, tournament.id)
    try:
        ensure_expected_revision(tournament, payload.expected_revision)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    match = await get_match_or_404(db_session, tournament_id=tournament.id, match_id=match_id)

    try:
        ensure_match_report_allowed(tournament.status)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not match.home_team_id or not match.away_team_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Both qualified teams must be known before reporting a result.",
        )

    total_rounds = await tournament_latest_round_number(
        db_session,
        tournament_id=tournament.id,
    )
    matches_in_round = await db_session.scalar(
        select(func.count())
        .select_from(TournamentMatch)
        .where(
            TournamentMatch.tournament_id == tournament.id,
            TournamentMatch.round_number == match.round_number,
        )
    )
    is_final_match = bool(
        match.round_number == (total_rounds or match.round_number)
        and matches_in_round == 1
    )
    match_format = (
        tournament.final_format
        if is_final_match
        else tournament.match_format
    )

    try:
        report = resolve_match_report(
            current_status=match.status,
            home_score=payload.home_score,
            away_score=payload.away_score,
            note=(payload.note or "").strip() or None,
            match_format=match_format,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    match.status = report.status
    match.home_score = report.home_score
    match.away_score = report.away_score
    match.winner_side = report.winner_side
    match.winner_team_id = (
        match.home_team_id if report.winner_side == "home" else match.away_team_id
    )
    match.report_note = report.note
    match.reported_at = datetime.now(UTC)
    match.reported_by_user_id = auth_session.user.id
    try:
        await propagate_match_winner(db_session, match=match)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    eliminated_team_id = eliminated_team_id_for_single_elimination(
        home_team_id=match.home_team_id,
        away_team_id=match.away_team_id,
        winner_team_id=match.winner_team_id,
    )
    if is_final_match:
        try:
            transition = await complete_locked_tournament_after_final_match(
                db_session,
                tournament=tournament,
                now=auth_session.now,
                actor_user_id=auth_session.user.id,
                audit_payload={
                    "final_match_id": match.id,
                    "winner_team_id": match.winner_team_id,
                },
            )
        except TournamentStatusTransitionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except TournamentCompletionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        released_commitments = transition.released_commitments
    else:
        released_commitments = await release_active_commitments(
            db_session,
            tournament_id=tournament.id,
            team_ids=[eliminated_team_id],
            released_at=auth_session.now,
            release_reason="team_eliminated",
        )

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="match.report",
        subject_type="tournament_match",
        subject_id=match.id,
        payload={
            "tournament_slug": tournament.slug,
            "home_score": match.home_score,
            "away_score": match.away_score,
            "winner_side": match.winner_side,
            "winner_team_id": match.winner_team_id,
            "eliminated_team_id": eliminated_team_id,
            "match_format": match_format,
            "released_commitments": released_commitments,
            "tournament_auto_completed": is_final_match,
        },
    )
    advance_revision(tournament)
    await db_session.commit()
    await db_session.refresh(match)
    response = await serialize_single_match_for_tournament(
        db_session,
        tournament=tournament,
        match=match,
    )
    return response


@router.delete("/{slug}/matches/{match_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tournament_match(
    slug: str,
    match_id: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_tournament_manager(auth_session, tournament)
    tournament = await lock_tournament_for_bracket(db_session, tournament.id)
    match = await get_match_or_404(db_session, tournament_id=tournament.id, match_id=match_id)
    if match.home_source_match_id or match.away_source_match_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Generated bracket matches cannot be deleted. Use result recovery instead.",
        )
    dependent_match_id = await db_session.scalar(
        select(TournamentMatch.id).where(
            TournamentMatch.tournament_id == tournament.id,
            or_(
                TournamentMatch.home_source_match_id == match.id,
                TournamentMatch.away_source_match_id == match.id,
            ),
        )
    )
    if dependent_match_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Generated bracket matches cannot be deleted. Use result recovery instead.",
        )

    latest_round_number = await tournament_latest_round_number(
        db_session,
        tournament_id=tournament.id,
    )
    try:
        ensure_match_deletion_allowed(
            tournament_status=tournament.status,
            current_status=match.status,
            current_round_number=match.round_number,
            latest_round_number=latest_round_number or match.round_number,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="match.delete",
        subject_type="tournament_match",
        subject_id=match.id,
        payload={
            "tournament_slug": tournament.slug,
            "round_number": match.round_number,
            "sequence_number": match.sequence_number,
            "status": match.status,
        },
    )
    await db_session.delete(match)
    advance_revision(tournament)
    await db_session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{slug}/deadlock/ready-check", response_model=TournamentDeadlockReadyCheckStateResponse)
async def get_deadlock_ready_check_state(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockReadyCheckStateResponse:
    current_user_id = auth_session.user.id
    preflight = await deadlock_ready_check_read_preflight(
        db_session,
        slug=slug,
        user_id=current_user_id,
    )
    tournament = preflight.tournament
    ensure_deadlock_tournament_format(tournament)
    is_organizer = tournament.organizer_user_id == current_user_id
    if not preflight.has_participant and not is_organizer:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Join the tournament before viewing ready-check state.",
        )
    return await build_deadlock_ready_check_state_response(
        db_session,
        tournament_id=tournament.id,
        current_user_id=current_user_id,
        tournament_bracket_revision=tournament.bracket_revision,
        active_round=preflight.active_round,
        latest_round=preflight.latest_round,
        rounds_loaded=True,
    )


@router.post(
    "/{slug}/deadlock/ready-check/start",
    response_model=TournamentDeadlockReadyRoundResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_deadlock_ready_check(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockReadyRoundResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    tournament = await lock_tournament_for_workflow(db_session, tournament.id)
    await db_session.refresh(tournament)
    ensure_deadlock_tournament_format(tournament)
    ensure_tournament_organizer(auth_session, tournament)
    if tournament.automation_ready_check_closed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deadlock ready-check is already closed.",
        )
    if tournament.ready_check_starts_at is None and tournament.ready_check_ends_at is None:
        # The explicit organizer action is the legacy/manual way to start a
        # round. Give it the same server-known window as a scheduled check so
        # the page timer and vote authorization still share one clock.
        tournament.ready_check_starts_at = auth_session.now
        tournament.ready_check_ends_at = auth_session.now + MANUAL_READY_CHECK_DURATION
    elif tournament.ready_check_starts_at is None or tournament.ready_check_ends_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ready Check requires both starts_at and ends_at.",
        )
    try:
        ensure_deadlock_roster_staging_allowed(
            format_slug=tournament.format_slug,
            tournament_status=tournament.status,
            has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                db_session,
                tournament=tournament,
            ),
            action_name="Deadlock ready-check",
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

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
    active_round = await deadlock_ready_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    decision = prepare_ready_check_start(participant_user_ids, has_active_round=active_round is not None)
    if decision.status == "already_active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deadlock ready-check is already active.",
        )
    if decision.status == "empty":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No participants are available for ready-check.",
        )

    round_row = TournamentDeadlockReadyRound(
        tournament_id=tournament.id,
        status="active",
        eligible_user_ids=list(decision.user_ids),
        initiated_by_user_id=auth_session.user.id,
    )
    mark_ready_check_started(tournament, now=auth_session.now)
    _invalidate_ready_check_state_cache(tournament.id)
    db_session.add(round_row)
    await db_session.flush()
    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.deadlock.ready_check.start",
        subject_type="tournament_deadlock_ready_round",
        subject_id=str(round_row.id),
        payload={
            "tournament_slug": tournament.slug,
            "eligible_participant_count": len(decision.user_ids),
        },
    )
    await db_session.commit()
    await db_session.refresh(round_row)
    return await serialize_deadlock_ready_round(
        db_session,
        round_row,
        current_user_id=auth_session.user.id,
    )


@router.post("/{slug}/deadlock/ready-check/vote", response_model=TournamentDeadlockReadyVoteResponse)
async def vote_deadlock_ready_check(
    slug: str,
    payload: TournamentDeadlockReadyVoteRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockReadyVoteResponse:
    current_user_id = auth_session.user.id
    tournament: Tournament | None = None
    try:
        preflight = await prepare_deadlock_ready_vote(
            db_session,
            slug=slug,
            user_id=current_user_id,
            choice=payload.choice,
            now=auth_session.now,
        )
        tournament = preflight.tournament
        ensure_deadlock_tournament_format(tournament)
        ensure_deadlock_roster_staging_allowed(
            format_slug=tournament.format_slug,
            tournament_status=tournament.status,
            has_locked_deadlock_roster=preflight.has_locked_roster,
            action_name="Deadlock ready-check voting",
        )
        if not preflight.has_participant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only joined participants can vote in deadlock ready-check.",
            )
        active_round = preflight.active_round
        if active_round is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Deadlock ready-check is not active.",
            )

        if payload.choice == "yes" and not preflight.has_deadlock_profile:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Complete your Deadlock profile before confirming ready status.",
            )

        eligible_user_ids = {str(user_id) for user_id in list(active_round.eligible_user_ids or [])}
        if eligible_user_ids and current_user_id not in eligible_user_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not eligible for the active ready-check.",
            )

        vote_changed = await upsert_deadlock_ready_vote(
            db_session,
            round_id=active_round.id,
            user_id=current_user_id,
            choice=payload.choice,
            responded_at=auth_session.now,
        )

        if vote_changed:
            _invalidate_ready_check_state_cache(tournament.id)
            try:
                await db_session.commit()
            except IntegrityError as exc:
                await db_session.rollback()
                if "ready vote requires an active ready round" not in str(exc):
                    raise
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Deadlock ready-check is no longer active.",
                ) from exc

        # The conditional upsert is the authoritative idempotency check: an
        # unchanged existing choice returns no row, while an insert or changed
        # choice returns the affected vote id.  Avoid a second pre-read on the
        # hot vote path and keep the decision atomic under concurrent requests.
        outcome = "idempotent" if not vote_changed else "accepted"
        starts_at = tournament.ready_check_starts_at
        relative_ms = (
            round((auth_session.now - starts_at).total_seconds() * 1000)
            if starts_at is not None
            else None
        )
        logger.info(
            "ready_vote outcome=%s tournament=%s user=%s relative_ms=%s",
            outcome,
            tournament.slug,
            current_user_id,
            relative_ms,
        )
        return TournamentDeadlockReadyVoteResponse(
            round_id=active_round.id,
            tournament_id=active_round.tournament_id,
            status=active_round.status,
            eligible_participant_count=len(list(active_round.eligible_user_ids or [])),
            current_user_choice=payload.choice,
            changed=vote_changed,
            server_received_at=auth_session.now,
        )
    except TournamentWorkflowError as exc:
        relative_ms = getattr(exc, "relative_ms", None)
        if tournament is not None and tournament.ready_check_starts_at is not None:
            relative_ms = round(
                (auth_session.now - tournament.ready_check_starts_at).total_seconds() * 1000
            )
        logger.info(
            "ready_vote outcome=rejected tournament=%s reason=%s relative_ms=%s",
            slug,
            str(exc)[:160],
            relative_ms,
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except HTTPException as exc:
        relative_ms = None
        if tournament is not None and tournament.ready_check_starts_at is not None:
            relative_ms = round(
                (auth_session.now - tournament.ready_check_starts_at).total_seconds() * 1000
            )
        logger.info(
            "ready_vote outcome=rejected tournament=%s status=%s reason=%s relative_ms=%s",
            slug,
            exc.status_code,
            str(exc.detail)[:160],
            relative_ms,
        )
        raise


@router.post("/{slug}/deadlock/ready-check/close", response_model=TournamentDeadlockReadyRoundResponse)
async def close_deadlock_ready_check(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockReadyRoundResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    tournament = await lock_tournament_for_workflow(db_session, tournament.id)
    await db_session.refresh(tournament)
    ensure_deadlock_tournament_format(tournament)
    ensure_tournament_organizer(auth_session, tournament)
    try:
        ensure_deadlock_roster_staging_allowed(
            format_slug=tournament.format_slug,
            tournament_status=tournament.status,
            has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                db_session,
                tournament=tournament,
            ),
            action_name="Deadlock ready-check closure",
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    active_round = await deadlock_ready_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_round is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deadlock ready-check is not active.",
        )

    active_round.status = "closed"
    active_round.closed_at = auth_session.now
    mark_ready_check_closed(tournament, now=auth_session.now)
    _invalidate_ready_check_state_cache(tournament.id)
    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.deadlock.ready_check.close",
        subject_type="tournament_deadlock_ready_round",
        subject_id=str(active_round.id),
        payload={"tournament_slug": tournament.slug},
    )
    await db_session.commit()
    await db_session.refresh(active_round)
    return await serialize_deadlock_ready_round(
        db_session,
        active_round,
        current_user_id=auth_session.user.id,
    )


@router.get("/{slug}/deadlock/captain-preview", response_model=TournamentDeadlockCaptainPreviewResponse)
async def get_deadlock_captain_preview(
    slug: str,
    teams_count: int | None = Query(default=None, ge=2, le=8192),
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockCaptainPreviewResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_deadlock_tournament_format(tournament)
    ensure_tournament_organizer(auth_session, tournament)
    from apps.platform_api.app.services.deadlock_automation import advance_deadlock_tournament_automation

    await advance_deadlock_tournament_automation(
        db_session,
        tournament=tournament,
        now=auth_session.now,
        allow_assignment_generation=False,
    )

    source_round = await deadlock_ready_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if source_round is None:
        source_round = await deadlock_ready_round_for_tournament(
            db_session,
            tournament_id=tournament.id,
            active_only=False,
        )
    if source_round is None:
        return TournamentDeadlockCaptainPreviewResponse(
            teams_count=teams_count or DEFAULT_TEAM_COUNT_LIMIT,
            source_ready_round_id=None,
            ready_player_count=0,
            candidates=[],
        )

    prepared_rows = await deadlock_ready_candidate_rows_for_round(
        db_session,
        tournament_id=tournament.id,
        round_id=source_round.id,
    )
    try:
        effective_teams_count = resolve_effective_teams_count(
            requested_teams_count=teams_count,
            ready_player_count=len(prepared_rows),
        )
    except ValueError:
        effective_teams_count = 0
    preview = build_captain_preview(prepared_rows, effective_teams_count)
    return TournamentDeadlockCaptainPreviewResponse(
        teams_count=effective_teams_count,
        source_ready_round_id=source_round.id,
        ready_player_count=len(prepared_rows),
        candidates=[
            {
                "user_id": candidate.user_id,
                "display_name": candidate.display_name,
                "rank": candidate.rank,
                "subrank": candidate.subrank,
                "playtime": candidate.playtime,
                "captain_priority": candidate.captain_priority,
                "captain_priority_bucket": candidate.captain_priority_bucket,
                "strength": round(candidate.strength, 4),
                "projected_team_id": candidate.projected_team_id,
            }
            for candidate in preview
        ],
    )


@router.get("/{slug}/deadlock/captain-round", response_model=TournamentDeadlockCaptainRoundStateResponse)
async def get_deadlock_captain_round_state(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockCaptainRoundStateResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_deadlock_tournament_format(tournament)
    current_user_id = auth_session.user.id
    participant = await joined_participant_for_user(
        db_session,
        tournament_id=tournament.id,
        user_id=current_user_id,
    )
    is_organizer = tournament.organizer_user_id == current_user_id
    if participant is None and not is_organizer:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Join the tournament before viewing captain-round state.",
        )
    from apps.platform_api.app.services.deadlock_automation import advance_deadlock_tournament_automation

    await advance_deadlock_tournament_automation(
        db_session,
        tournament=tournament,
        now=auth_session.now,
        allow_assignment_generation=False,
    )

    active_round = await deadlock_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    latest_round = active_round or await deadlock_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=False,
    )
    return TournamentDeadlockCaptainRoundStateResponse(
        active_round=(
            await serialize_deadlock_captain_round(
                db_session,
                active_round,
                current_user_id=current_user_id,
                include_entries=is_organizer,
            )
            if active_round is not None
            else None
        ),
        latest_round=(
            await serialize_deadlock_captain_round(
                db_session,
                latest_round,
                current_user_id=current_user_id,
                include_entries=is_organizer,
            )
            if latest_round is not None
            else None
        ),
    )


@router.post(
    "/{slug}/deadlock/captain-round/start",
    response_model=TournamentDeadlockCaptainRoundResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_deadlock_captain_round(
    slug: str,
    payload: TournamentDeadlockCaptainRoundStartRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockCaptainRoundResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    tournament = await lock_tournament_for_workflow(db_session, tournament.id)
    await db_session.refresh(tournament)
    ensure_deadlock_tournament_format(tournament)
    ensure_tournament_organizer(auth_session, tournament)
    try:
        ensure_deadlock_roster_staging_allowed(
            format_slug=tournament.format_slug,
            tournament_status=tournament.status,
            has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                db_session,
                tournament=tournament,
            ),
            action_name="Deadlock captain selection",
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    active_round = await deadlock_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_round is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deadlock captain round is already active.",
        )

    active_ready_round = await deadlock_ready_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_ready_round is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Close the active ready-check round before starting captain selection.",
        )

    source_ready_round = await deadlock_closed_ready_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    if source_ready_round is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Close a ready-check round before starting captain selection.",
        )
    existing_captain_round_id = await db_session.scalar(
        select(TournamentDeadlockCaptainRound.id).where(
            TournamentDeadlockCaptainRound.source_ready_round_id == source_ready_round.id,
        )
    )
    if existing_captain_round_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deadlock captain selection already exists for this ready-check round.",
        )

    candidate_rows = await deadlock_ready_candidate_rows_for_round(
        db_session,
        tournament_id=tournament.id,
        round_id=source_ready_round.id,
    )
    try:
        teams_count = resolve_effective_teams_count(
            requested_teams_count=payload.teams_count,
            ready_player_count=len(candidate_rows),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    round_row = TournamentDeadlockCaptainRound(
        tournament_id=tournament.id,
        source_ready_round_id=source_ready_round.id,
        teams_count=teams_count,
        status="finalized",
        initiated_by_user_id=auth_session.user.id,
        closed_at=auth_session.now,
        finalized_at=auth_session.now,
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
                responded_at=auth_session.now if entry.state == "assigned" else None,
            )
        )

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.deadlock.captain_round.start",
        subject_type="tournament_deadlock_captain_round",
        subject_id=str(round_row.id),
        payload={
            "tournament_slug": tournament.slug,
            "source_ready_round_id": source_ready_round.id,
            "requested_teams_count": payload.teams_count,
            "teams_count": teams_count,
            "candidate_count": len(candidate_rows),
            "assigned_count": teams_count,
        },
    )
    await db_session.commit()
    await db_session.refresh(round_row)
    return await serialize_deadlock_captain_round(
        db_session,
        round_row,
        current_user_id=auth_session.user.id,
        include_entries=True,
    )


@router.post(
    "/{slug}/deadlock/captain-round/respond",
    response_model=TournamentDeadlockCaptainRoundResponse,
    include_in_schema=False,
)
async def respond_deadlock_captain_round(
    slug: str,
    payload: TournamentDeadlockCaptainRoundRespondRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockCaptainRoundResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_deadlock_tournament_format(tournament)
    current_user_id = auth_session.user.id
    if payload.decision == "decline":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Captain decline is disabled; captains are selected automatically.",
        )
    try:
        ensure_deadlock_roster_staging_allowed(
            format_slug=tournament.format_slug,
            tournament_status=tournament.status,
            has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                db_session,
                tournament=tournament,
            ),
            action_name="Deadlock captain offers",
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    participant = await joined_participant_for_user(
        db_session,
        tournament_id=tournament.id,
        user_id=current_user_id,
    )
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only joined participants can respond to captain offers.",
        )
    from apps.platform_api.app.services.deadlock_automation import advance_deadlock_tournament_automation

    await advance_deadlock_tournament_automation(
        db_session,
        tournament=tournament,
        now=auth_session.now,
        allow_assignment_generation=False,
    )

    active_round = await deadlock_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_round is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deadlock captain round is not active.",
        )

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
    next_state, decision = round_state.respond(current_user_id, payload.decision)
    if decision.status == "missing":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Captain offer for the current user was not found.",
        )
    if decision.status == "closed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deadlock captain round is already closed.",
        )
    if decision.status == "filled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Captain slots are already filled.",
        )
    if decision.status not in {"updated", "accepted", "declined"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Captain offer is currently {decision.status}.",
        )

    if decision.status == "updated":
        entry_rows_by_user_id = {row.user_id: row for row in entry_rows}
        next_entries_by_user_id = {entry.user_id: entry for entry in next_state.entries}
        for user_id, row in entry_rows_by_user_id.items():
            next_entry = next_entries_by_user_id[user_id]
            if row.state == next_entry.state and row.assigned_team_id == next_entry.assigned_team_id:
                continue
            row.state = next_entry.state
            row.assigned_team_id = next_entry.assigned_team_id
            if next_entry.state in {"accepted", "declined", "cancelled"}:
                row.responded_at = auth_session.now

        await write_audit_log(
            db_session,
            actor_user_id=current_user_id,
            action="tournament.deadlock.captain_round.respond",
            subject_type="tournament_deadlock_captain_round",
            subject_id=str(active_round.id),
            payload={
                "tournament_slug": tournament.slug,
                "decision": payload.decision,
                "newly_offered_user_ids": list(decision.newly_offered_user_ids),
                "cancelled_user_ids": list(decision.cancelled_user_ids),
                "accepted_count": decision.accepted_count,
                "offered_count": decision.offered_count,
            },
        )
        await db_session.commit()

    refreshed_round = await db_session.scalar(
        select(TournamentDeadlockCaptainRound).where(TournamentDeadlockCaptainRound.id == active_round.id)
    )
    if refreshed_round is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Captain round not found.",
        )
    return await serialize_deadlock_captain_round(
        db_session,
        refreshed_round,
        current_user_id=current_user_id,
        include_entries=tournament.organizer_user_id == current_user_id,
    )


@router.post(
    "/{slug}/deadlock/captain-round/close",
    response_model=TournamentDeadlockCaptainRoundResponse,
    include_in_schema=False,
)
async def close_deadlock_captain_round(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockCaptainRoundResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_deadlock_tournament_format(tournament)
    ensure_tournament_organizer(auth_session, tournament)
    try:
        ensure_deadlock_roster_staging_allowed(
            format_slug=tournament.format_slug,
            tournament_status=tournament.status,
            has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                db_session,
                tournament=tournament,
            ),
            action_name="Deadlock captain selection",
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    active_round = await deadlock_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_round is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deadlock captain round is not active.",
        )

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
    ).close()

    next_entries_by_user_id = {entry.user_id: entry for entry in round_state.entries}
    for row in entry_rows:
        next_entry = next_entries_by_user_id[row.user_id]
        if row.state != next_entry.state:
            row.state = next_entry.state
            if next_entry.state == "cancelled":
                row.responded_at = auth_session.now

    active_round.status = "closed"
    active_round.closed_at = auth_session.now
    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.deadlock.captain_round.close",
        subject_type="tournament_deadlock_captain_round",
        subject_id=str(active_round.id),
        payload={"tournament_slug": tournament.slug},
    )
    await db_session.commit()
    await db_session.refresh(active_round)
    return await serialize_deadlock_captain_round(
        db_session,
        active_round,
        current_user_id=auth_session.user.id,
        include_entries=True,
    )


@router.post(
    "/{slug}/deadlock/captain-round/finalize",
    response_model=TournamentDeadlockCaptainRoundResponse,
    include_in_schema=False,
)
async def finalize_deadlock_captain_round(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockCaptainRoundResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_deadlock_tournament_format(tournament)
    ensure_tournament_organizer(auth_session, tournament)
    try:
        ensure_deadlock_roster_staging_allowed(
            format_slug=tournament.format_slug,
            tournament_status=tournament.status,
            has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                db_session,
                tournament=tournament,
            ),
            action_name="Deadlock captain finalization",
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    active_round = await deadlock_captain_round_for_tournament(
        db_session,
        tournament_id=tournament.id,
        active_only=True,
    )
    if active_round is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deadlock captain round is not active.",
        )

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
    if not round_state.can_finalize:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Captain round cannot be finalized until every slot is accepted.",
        )

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
    assignments = assign_captain_team_numbers([dict(row._mapping) for row in accepted_candidates])
    assigned_team_by_user_id = {assignment.user_id: assignment.team_id for assignment in assignments}

    for row in entry_rows:
        if row.user_id in assigned_team_by_user_id:
            row.state = "assigned"
            row.assigned_team_id = assigned_team_by_user_id[row.user_id]
            row.responded_at = row.responded_at or auth_session.now
        elif row.state in {"queued", "offered"}:
            row.state = "cancelled"
            row.responded_at = auth_session.now

    active_round.status = "finalized"
    active_round.finalized_at = auth_session.now
    active_round.closed_at = active_round.closed_at or auth_session.now
    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.deadlock.captain_round.finalize",
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
    await db_session.commit()
    await db_session.refresh(active_round)
    return await serialize_deadlock_captain_round(
        db_session,
        active_round,
        current_user_id=auth_session.user.id,
        include_entries=True,
    )


@router.get("/{slug}/deadlock/auto-assignment", response_model=TournamentDeadlockAutoAssignmentStateResponse)
async def get_deadlock_auto_assignment_state(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockAutoAssignmentStateResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_deadlock_tournament_format(tournament)
    current_user_id = auth_session.user.id
    participant = await joined_participant_for_user(
        db_session,
        tournament_id=tournament.id,
        user_id=current_user_id,
    )
    is_organizer = tournament.organizer_user_id == current_user_id
    if participant is None and not is_organizer:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Join the tournament before viewing Deadlock auto-assignment state.",
        )
    return await build_deadlock_auto_assignment_state_response(
        db_session,
        tournament=tournament,
        auth_session=auth_session,
        include_freshness=True,
    )


@router.post(
    "/{slug}/deadlock/auto-assignment/run-async",
    response_model=TournamentDeadlockAutoAssignmentJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def queue_deadlock_auto_assignment(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockAutoAssignmentJobResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    ensure_deadlock_tournament_format(tournament)
    ensure_tournament_organizer(auth_session, tournament)
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

    from apps.platform_worker.worker import deadlock_auto_assignment_run

    task = deadlock_auto_assignment_run.apply_async(
        args=[tournament.id, auth_session.user.id],
        expires=900,
    )
    return TournamentDeadlockAutoAssignmentJobResponse(task_id=str(task.id))


@router.post(
    "/{slug}/deadlock/auto-assignment/{run_id}/publish",
    response_model=TournamentDeadlockAutoAssignmentRunResponse,
)
async def publish_deadlock_auto_assignment_run(
    slug: str,
    run_id: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockAutoAssignmentRunResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    tournament = await lock_tournament_for_workflow(db_session, tournament.id)
    await db_session.refresh(tournament)
    ensure_deadlock_tournament_format(tournament)
    ensure_tournament_organizer(auth_session, tournament)
    try:
        ensure_deadlock_roster_staging_allowed(
            format_slug=tournament.format_slug,
            tournament_status=tournament.status,
            has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                db_session,
                tournament=tournament,
            ),
            action_name="Deadlock roster publishing",
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    run_row = await deadlock_assignment_run_by_id_for_tournament(
        db_session,
        tournament_id=tournament.id,
        run_id=run_id,
    )
    if run_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deadlock auto-assignment run not found.",
        )

    current_inputs = await deadlock_latest_auto_assignment_inputs_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    run_freshness = await deadlock_auto_assignment_run_freshness(
        db_session,
        tournament_id=tournament.id,
        run_row=run_row,
        current_inputs=current_inputs,
    )
    if run_freshness.is_stale:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=deadlock_auto_assignment_stale_detail(run_freshness.stale_reasons),
        )

    try:
        current_published = await supersede_published_deadlock_assignment_run_for_tournament(
            db_session,
            tournament_id=tournament.id,
            replacement_run_id=run_row.id,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    if run_row.status != "locked":
        try:
            run_row.status = transition_auto_assignment_run_status(run_row.status, "published")
        except AutoAssignmentRunWorkflowError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        run_row.published_at = auth_session.now
        run_row.published_by_user_id = auth_session.user.id

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.deadlock.auto_assignment.publish",
        subject_type="tournament_deadlock_assignment_run",
        subject_id=run_row.id,
        payload={
            "tournament_slug": tournament.slug,
            "replaced_run_id": current_published.id if current_published is not None and current_published.id != run_row.id else None,
        },
    )
    await db_session.commit()
    await db_session.refresh(run_row)
    return serialize_deadlock_auto_assignment_run(run_row, freshness=run_freshness)


@router.post(
    "/{slug}/deadlock/auto-assignment/{run_id}/lock",
    response_model=TournamentDeadlockAutoAssignmentRunResponse,
)
async def lock_deadlock_auto_assignment_run(
    slug: str,
    run_id: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockAutoAssignmentRunResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    tournament = await lock_tournament_for_workflow(db_session, tournament.id)
    await db_session.refresh(tournament)
    ensure_deadlock_tournament_format(tournament)
    ensure_tournament_organizer(auth_session, tournament)
    try:
        ensure_deadlock_roster_staging_allowed(
            format_slug=tournament.format_slug,
            tournament_status=tournament.status,
            has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                db_session,
                tournament=tournament,
            ),
            action_name="Deadlock roster locking",
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    run_row = await deadlock_assignment_run_by_id_for_tournament(
        db_session,
        tournament_id=tournament.id,
        run_id=run_id,
    )
    if run_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deadlock auto-assignment run not found.",
        )

    if run_row.status == "locked":
        return serialize_deadlock_auto_assignment_run(run_row)

    try:
        rebalanced, unavailable_user_ids = await finalize_deadlock_assignment_with_commitments(
            db_session,
            tournament=tournament,
            run_row=run_row,
            actor_user_id=auth_session.user.id,
            now=auth_session.now,
        )
    except (AutoAssignmentError, AutoAssignmentRunWorkflowError, TournamentWorkflowError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.deadlock.auto_assignment.lock",
        subject_type="tournament_deadlock_assignment_run",
        subject_id=run_row.id,
        payload={
            "tournament_slug": tournament.slug,
            "rebalanced": rebalanced,
            "unavailable_user_ids": list(unavailable_user_ids),
        },
    )
    await db_session.commit()
    await db_session.refresh(run_row)
    return serialize_deadlock_auto_assignment_run(run_row)


@router.post("/{slug}/join", response_model=TournamentParticipantResponse, status_code=status.HTTP_201_CREATED)
async def join_tournament(
    slug: str,
    payload: TournamentParticipantJoinRequest,
    request: Request,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentParticipantResponse:
    preflight = await participant_join_preflight(
        db_session,
        slug=slug,
        user_id=auth_session.user.id,
    )
    if preflight is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tournament not found.")
    idempotency = await reserve_mutation_idempotency(
        db_session,
        actor_user_id=auth_session.user.id,
        scope=f"tournament.join:{preflight.tournament.id}",
        key=request_idempotency_key(request),
        request_fingerprint=mutation_payload_fingerprint(
            {
                "tournament_id": preflight.tournament.id,
                "payload": payload.model_dump(mode="json"),
            }
        ),
    )
    if idempotency is not None and idempotency.replay:
        resource_id = idempotency.record.resource_id
        if resource_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The idempotent join request has not completed yet.",
            )
        existing_participant = await db_session.scalar(
            select(TournamentParticipant).where(
                TournamentParticipant.id == resource_id,
                TournamentParticipant.tournament_id == preflight.tournament.id,
                TournamentParticipant.user_id == auth_session.user.id,
            )
        )
        if existing_participant is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The participant created by this Idempotency-Key no longer exists.",
            )
        return serialize_participant(
            existing_participant,
            auth_session.user.display_name,
        )
    # Without an idempotency reservation there was no await between the
    # initial preflight and this point, so repeating the same correlated query
    # only added database/serialization work. Capacity remains authoritative
    # in create_participant -> claim_participant_slot, and the insert
    # constraint still closes the concurrent duplicate-user race. Keep the
    # refresh for idempotent retries because reserving an existing key may
    # have waited on another request before returning.
    if idempotency is not None:
        refreshed_preflight = await participant_join_preflight(
            db_session,
            slug=slug,
            user_id=auth_session.user.id,
        )
        if refreshed_preflight is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tournament not found.")
        preflight = refreshed_preflight
    tournament = preflight.tournament
    if tournament.visibility == "invite_only":
        has_invite_access = tournament.organizer_user_id == auth_session.user.id or preflight.has_invite_access
        if not has_invite_access:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Redeem an invite code in Tournaments before joining this private tournament.",
            )
    elif tournament.visibility != "public":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self-service join is unavailable for this tournament visibility.",
        )
    try:
        ensure_deadlock_registration_changes_allowed(
            format_slug=tournament.format_slug,
            has_locked_deadlock_roster=preflight.has_locked_deadlock_roster,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not can_self_join_tournament(tournament.status):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tournament registration is not open right now.",
        )

    if preflight.has_existing_participant:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This user is already registered in the tournament.",
        )
    team_name = normalized_team_name(payload.entry_type, payload.team_name)
    ensure_participant_join_limits_from_values(
        tournament=tournament,
        player_rank=preflight.player_rank,
        has_free_participant_slot=preflight.has_free_participant_slot,
    )
    try:
        async with db_session.begin_nested():
            participant = await create_participant(
                db_session,
                tournament=tournament,
                user=auth_session.user,
                entry_type=payload.entry_type,
                team_name=team_name,
            )
    except (IntegrityError, TournamentWorkflowError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                str(exc)
                if isinstance(exc, TournamentWorkflowError)
                else "This user is already registered in the tournament."
            ),
        ) from exc
    bind_mutation_idempotency_resource(idempotency, participant.id)
    await db_session.commit()
    invalidate_tournament_runtime_caches(tournament.id)
    return serialize_participant(participant, auth_session.user.display_name)


@router.delete("/{slug}/join", status_code=status.HTTP_204_NO_CONTENT)
async def leave_tournament(
    slug: str,
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    tournament = await get_tournament_or_404(db_session, slug)
    if not can_self_leave_tournament(tournament.status):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tournament registrations can no longer be changed.",
        )
    try:
        ensure_deadlock_registration_changes_allowed(
            format_slug=tournament.format_slug,
            has_locked_deadlock_roster=await tournament_has_locked_deadlock_roster(
                db_session,
                tournament=tournament,
            ),
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    participant = await db_session.scalar(
        select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == tournament.id,
            TournamentParticipant.user_id == auth_session.user.id,
        )
    )
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You are not registered in this tournament.",
        )
    if participant.status == "disqualified":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Disqualified participant records are retained until the organizer "
                "explicitly restores the participant."
            ),
        )
    if participant.status in {"confirmed", "checked_in"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Confirmed participants cannot leave the tournament.",
        )
    active_ready_yes_vote_id = await db_session.scalar(
        select(TournamentDeadlockReadyVote.id)
        .join(
            TournamentDeadlockReadyRound,
            TournamentDeadlockReadyVote.round_id == TournamentDeadlockReadyRound.id,
        )
        .where(
            TournamentDeadlockReadyRound.tournament_id == tournament.id,
            TournamentDeadlockReadyRound.status == "active",
            TournamentDeadlockReadyVote.user_id == auth_session.user.id,
            TournamentDeadlockReadyVote.choice == "yes",
        )
    )
    if active_ready_yes_vote_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Confirmed participants cannot leave the tournament.",
        )

    await db_session.execute(
        select(User.id).where(User.id == participant.user_id).with_for_update()
    )
    await release_participant_slot(
        db_session,
        participant_id=participant.id,
    )
    if is_solo_tournament_format(tournament.format_slug):
        await prune_participant_from_active_ready_round(
            db_session,
            tournament=tournament,
            user_id=participant.user_id,
            actor_user_id=auth_session.user.id,
            now=auth_session.now,
            participant_status="withdrawn",
        )
        await prune_participant_from_active_captain_round(
            db_session,
            tournament=tournament,
            user_id=participant.user_id,
            actor_user_id=auth_session.user.id,
            now=auth_session.now,
            participant_status="withdrawn",
        )

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.leave",
        subject_type="tournament_participant",
        subject_id=participant.id,
        payload={"tournament_slug": tournament.slug},
    )
    await db_session.execute(
        delete(TournamentParticipant).where(TournamentParticipant.id == participant.id)
    )
    await db_session.commit()
    invalidate_tournament_runtime_caches(tournament.id)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/{slug}/profiles/{user_id}", response_model=TournamentScopedProfileResponse)
async def get_tournament_scoped_profile(
    slug: str,
    user_id: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentScopedProfileResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    current_user_id = auth_session.user.id
    is_organizer = tournament.organizer_user_id == current_user_id
    is_admin = auth_session_has_admin_role(auth_session)
    participant = await joined_participant_for_user(
        db_session,
        tournament_id=tournament.id,
        user_id=current_user_id,
    )
    if participant is None and not is_organizer and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tournament profiles are visible only to joined participants, the organizer, or platform admins.",
        )

    published_run = await deadlock_published_auto_assignment_run_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    visible_run = published_run
    if visible_run is None and (is_organizer or is_admin):
        visible_run = await deadlock_auto_assignment_run_for_tournament(
            db_session,
            tournament_id=tournament.id,
        )
    if visible_run is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tournament profiles are available after teams are formed.",
        )

    if user_id not in deadlock_assignment_run_user_ids(visible_run):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Player is not part of the visible tournament roster.",
        )

    profile = await db_session.scalar(select(PlayerProfile).where(PlayerProfile.user_id == user_id))
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    deadlock_profile = await db_session.scalar(
        select(DeadlockProfile).where(DeadlockProfile.user_id == user_id)
    )
    dream_slot_rows = (
        await db_session.scalars(
            select(DeadlockDreamSlot)
            .where(DeadlockDreamSlot.user_id == user_id)
            .order_by(DeadlockDreamSlot.slot_number.asc())
        )
    ).all()
    dream_slots_by_number = {row.slot_number: row for row in dream_slot_rows}
    dream_slots = [
        DeadlockDreamSlotResponse(
            user_id=user_id,
            slot_number=slot_number,
            allowed_roles=list(dream_slots_by_number[slot_number].allowed_roles or [])
            if slot_number in dream_slots_by_number
            else [],
            desired_heroes=list(dream_slots_by_number[slot_number].desired_heroes or [])
            if slot_number in dream_slots_by_number
            else [],
            updated_at=dream_slots_by_number[slot_number].updated_at
            if slot_number in dream_slots_by_number
            else None,
        )
        for slot_number in range(1, 7)
    ]

    tournaments_played = int(
        await db_session.scalar(
            select(func.count())
            .select_from(TournamentParticipant)
            .where(
                TournamentParticipant.user_id == user_id,
                TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
            )
        )
        or 0
    )
    tournaments_organized = int(
        await db_session.scalar(
            select(func.count())
            .select_from(Tournament)
            .where(Tournament.organizer_user_id == user_id)
        )
        or 0
    )
    recent_tournament_rows = (
        await db_session.scalars(
            select(Tournament.name)
            .join(TournamentParticipant, TournamentParticipant.tournament_id == Tournament.id)
            .where(
                TournamentParticipant.user_id == user_id,
                TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
            )
            .order_by(Tournament.created_at.desc())
            .limit(5)
        )
    ).all()

    profile_media = await load_media_descriptors(
        db_session,
        (profile.avatar_asset_id, profile.banner_asset_id),
    )
    avatar_media = (
        profile_media.get(profile.avatar_asset_id) if profile.avatar_asset_id else None
    )
    banner_media = (
        profile_media.get(profile.banner_asset_id) if profile.banner_asset_id else None
    )
    profile_response = TournamentProfileResponse.model_validate(profile).model_copy(
        update={
            "avatar_url": compatibility_media_url(
                avatar_media,
                preferred_variant="avatar-256",
                legacy_url=profile.avatar_url,
            ),
            "banner_url": compatibility_media_url(
                banner_media,
                preferred_variant="banner-1920",
                legacy_url=profile.banner_url,
            ),
            "avatar_media": avatar_media,
            "banner_media": banner_media,
        }
    )

    return TournamentScopedProfileResponse(
        profile=profile_response,
        deadlock_profile=DeadlockProfileResponse.model_validate(deadlock_profile)
        if deadlock_profile is not None
        else None,
        dream_slots=dream_slots,
        stats=TournamentProfileStatsResponse(
            tournaments_played=tournaments_played,
            tournaments_organized=tournaments_organized,
            tournaments_won=0,
            recent_tournaments=list(recent_tournament_rows),
        ),
    )


@router.get("/{slug}/workspace", response_model=TournamentWorkspaceResponse)
async def get_tournament_workspace(
    slug: str,
    request: Request,
    response: Response,
    participants_limit: int = Query(
        default=25,
        ge=0,
        le=PARTICIPANT_LIST_MAX_LIMIT,
    ),
    participants_offset: int = Query(default=0, ge=0),
    workspace_view: Literal["detail", "bracket", "bracket_summary"] = Query(default="bracket"),
    include_current_user: bool = Query(default=True),
    auth_session=Depends(get_optional_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentWorkspaceResponse | Response:
    # The schedule and this timestamp travel in one authoritative response.
    # The browser derives elapsed time from this anchor with performance.now()
    # and never needs an extra request just to cross a known boundary.
    public_snapshot_candidate = bool(
        workspace_view == "detail"
        and participants_limit == 0
        and participants_offset == 0
        and not include_current_user
    )
    if public_snapshot_candidate:
        snapshot = _get_public_workspace_snapshot_cache(slug)
        if snapshot is not None:
            current_tournament = await db_session.scalar(
                select(Tournament).where(Tournament.slug == slug)
            )
            if current_tournament is not None:
                ensure_tournament_summary_visible(current_tournament, auth_session)
                if (
                    str(current_tournament.id) == snapshot.tournament_id
                    and current_tournament.visibility == "public"
                    and current_tournament.status == "registration_open"
                    and current_tournament.updated_at == snapshot.tournament_updated_at
                ):
                    participant_record: TournamentParticipant | None = None
                    invite_access: TournamentInviteAccess | None = None
                    active_commitment: PlayerTournamentCommitmentResponse | None = None
                    if auth_session is not None:
                        participant_record, invite_access, active_commitment = await workspace_access_for_user(
                            db_session,
                            tournament_id=current_tournament.id,
                            user_id=auth_session.user.id,
                        )
                    current_user_is_organizer = bool(
                        auth_session is not None
                        and current_tournament.organizer_user_id == auth_session.user.id
                    )
                    if (
                        participant_record is None
                        and not current_user_is_organizer
                        and not auth_session_has_admin_role(auth_session)
                    ):
                        server_time = datetime.now(UTC)
                        workspace_response = snapshot.response.model_copy(
                            update={
                                "server_time": server_time,
                                "tournament": snapshot.response.tournament.model_copy(
                                    update={
                                        "current_user_has_invite_access": invite_access is not None,
                                    }
                                ),
                                "current_user_active_commitment": active_commitment,
                            }
                        )
                        etag = _workspace_response_etag(
                            workspace_response,
                            workspace_view=workspace_view,
                            participants_limit=participants_limit,
                            participants_offset=participants_offset,
                            include_current_user=include_current_user,
                            user_id=(
                                auth_session.user.id
                                if auth_session is not None
                                else "anonymous"
                            ),
                        )
                        not_modified = _conditional_response(request, response, etag=etag)
                        return not_modified or _serialized_model_response(
                            workspace_response,
                            etag=etag,
                        )

    row = (
        await db_session.execute(
            tournament_with_counts_stmt().where(Tournament.slug == slug)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tournament not found.")
    (
        tournament,
        organizer_display_name,
        organizer_avatar_url,
        organizer_avatar_asset_id,
        participant_count,
        locked_roster_count,
    ) = row
    ensure_tournament_summary_visible(tournament, auth_session)
    cover_media, organizer_avatar_media = await tournament_media_descriptors(
        db_session,
        tournament,
        organizer_avatar_asset_id=organizer_avatar_asset_id,
    )

    participant_record: TournamentParticipant | None = None
    invite_access: TournamentInviteAccess | None = None
    active_commitment: PlayerTournamentCommitmentResponse | None = None
    if auth_session is not None:
        participant_record, invite_access, active_commitment = await workspace_access_for_user(
            db_session,
            tournament_id=tournament.id,
            user_id=auth_session.user.id,
        )
    current_user = None
    if include_current_user and auth_session is not None:
        current_user = await serialize_current_user(
            db_session,
            auth_session.user,
            role_slugs=auth_session.role_slugs,
            private_tournament_monthly_remaining=await private_tournament_monthly_remaining(
                db_session,
                organizer_user_id=auth_session.user.id,
                now=auth_session.now,
            ),
            private_tournament_monthly_limit=PRIVATE_TOURNAMENT_MONTHLY_LIMIT,
        )

    has_participant_record = participant_record is not None
    workspace_visible = can_view_tournament_workspace_data(
        tournament,
        auth_session=auth_session,
        has_participant_record=has_participant_record,
    )
    tournament_response = serialize_tournament(
        tournament,
        organizer_display_name,
        int(participant_count),
        organizer_avatar_url=organizer_avatar_url,
        cover_media=cover_media,
        organizer_avatar_media=organizer_avatar_media,
        has_locked_deadlock_roster=bool(int(locked_roster_count)),
        current_user_participant_status=(
            participant_record.status if participant_record is not None else None
        ),
        current_user_has_invite_access=invite_access is not None,
    )
    if not workspace_visible:
        server_time = datetime.now(UTC)
        workspace_response = TournamentWorkspaceResponse(
            tournament=tournament_response,
            server_time=server_time,
            current_user=current_user,
            current_user_active_commitment=active_commitment,
            participants=[],
            participants_total=0,
            participants_limit=participants_limit,
            participants_offset=participants_offset,
            participants_has_more=False,
            participants_available=False,
            bracket=None,
            ready_check=None,
            auto_assignment=None,
            state_version=tournament_response.state_version,
        )
        etag = _workspace_response_etag(
            workspace_response,
            workspace_view=workspace_view,
            participants_limit=participants_limit,
            participants_offset=participants_offset,
            include_current_user=include_current_user,
            user_id=auth_session.user.id if auth_session is not None else "anonymous",
        )
        not_modified = _conditional_response(request, response, etag=etag)
        return not_modified or _serialized_model_response(workspace_response, etag=etag)

    if participants_limit > 0:
        participants, participants_total, participants_has_more = await tournament_participant_page(
            db_session,
            tournament_id=tournament.id,
            limit=participants_limit,
            offset=participants_offset,
        )
    else:
        participants = []
        participants_total = int(participant_count)
        participants_has_more = participants_total > participants_offset
    current_user_is_organizer = bool(
        auth_session is not None and tournament.organizer_user_id == auth_session.user.id
    )
    current_user_can_manage_bracket = bool(
        auth_session is not None
        and (
            current_user_is_organizer
            or auth_session_has_admin_role(auth_session)
        )
    )
    assignment_latest_run: TournamentDeadlockAssignmentRun | None = None
    assignment_published_run: TournamentDeadlockAssignmentRun | None = None
    is_solo_format = is_solo_tournament_format(tournament.format_slug)
    assignment_runs_loaded = not is_solo_format
    if is_solo_format and current_user_is_organizer and workspace_view != "bracket_summary":
        assignment_latest_run, assignment_published_run = await deadlock_auto_assignment_state_runs_for_tournament(
            db_session,
            tournament_id=tournament.id,
        )
        assignment_runs_loaded = True
    visible_assignment_run = assignment_published_run or (
        assignment_latest_run if current_user_can_manage_bracket else None
    )
    if workspace_view == "bracket_summary":
        bracket = build_tournament_workspace_bracket_summary_response(
            tournament=tournament,
            can_manage=current_user_can_manage_bracket,
        )
    elif workspace_view == "detail":
        bracket = await build_tournament_workspace_detail_bracket_response(
            db_session,
            tournament=tournament,
            can_manage=current_user_can_manage_bracket,
            visible_assignment_run=visible_assignment_run,
            assignment_run_loaded=assignment_runs_loaded,
        )
    else:
        bracket = await build_tournament_bracket_response(
            db_session,
            tournament=tournament,
            auth_session=auth_session,
            has_participant_record=has_participant_record,
            visible_assignment_run=visible_assignment_run,
            assignment_run_loaded=assignment_runs_loaded,
        )
    ready_check: TournamentDeadlockReadyCheckStateResponse | None = None
    auto_assignment: TournamentDeadlockAutoAssignmentStateResponse | None = None
    current_user_can_view_deadlock_state = bool(
        auth_session is not None
        and (
            tournament.organizer_user_id == auth_session.user.id
            or (
                participant_record is not None
                and not participant_status_is_inactive(participant_record.status)
            )
        )
    )
    if current_user_can_view_deadlock_state and is_solo_format and workspace_view != "bracket_summary":
        ready_check = await build_deadlock_ready_check_state_response(
            db_session,
            tournament_id=tournament.id,
            current_user_id=auth_session.user.id,
            tournament_bracket_revision=tournament.bracket_revision,
        )
    if current_user_is_organizer and is_solo_format and workspace_view != "bracket_summary":
        auto_assignment = await build_deadlock_auto_assignment_state_response(
            db_session,
            tournament=tournament,
            auth_session=auth_session,
            include_freshness=False,
            latest_run=assignment_latest_run,
            published_run=assignment_published_run,
            assignment_runs_loaded=assignment_runs_loaded,
        )

    server_time = datetime.now(UTC)
    workspace_response = TournamentWorkspaceResponse(
        tournament=tournament_response,
        server_time=server_time,
        current_user=current_user,
        current_user_active_commitment=active_commitment,
        participants=participants,
        participants_total=participants_total,
        participants_limit=participants_limit,
        participants_offset=participants_offset,
        participants_has_more=participants_has_more,
        participants_available=True,
        bracket=bracket,
        ready_check=ready_check,
        auto_assignment=auto_assignment,
        state_version=tournament_response.state_version,
    )
    if (
        public_snapshot_candidate
        and tournament.visibility == "public"
        and tournament.status == "registration_open"
        and participant_record is None
        and not current_user_is_organizer
        and not auth_session_has_admin_role(auth_session)
    ):
        _set_public_workspace_snapshot_cache(
            tournament.slug,
            tournament_id=str(tournament.id),
            tournament_updated_at=tournament.updated_at,
            response=workspace_response.model_copy(
                update={
                    "tournament": tournament_response.model_copy(
                        update={"current_user_has_invite_access": False}
                    ),
                    "current_user": None,
                    "current_user_active_commitment": None,
                }
            ),
        )
    etag = _workspace_response_etag(
        workspace_response,
        workspace_view=workspace_view,
        participants_limit=participants_limit,
        participants_offset=participants_offset,
        include_current_user=include_current_user,
        user_id=auth_session.user.id if auth_session is not None else "anonymous",
    )
    not_modified = _conditional_response(request, response, etag=etag)
    return not_modified or _serialized_model_response(workspace_response, etag=etag)


@router.get("/{slug}", response_model=TournamentResponse)
async def get_tournament(
    slug: str,
    auth_session=Depends(get_optional_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentResponse:
    row = (
        await db_session.execute(
            tournament_with_counts_stmt().where(Tournament.slug == slug)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tournament not found.")
    (
        tournament,
        organizer_display_name,
        organizer_avatar_url,
        organizer_avatar_asset_id,
        participant_count,
        locked_roster_count,
    ) = row
    ensure_tournament_summary_visible(tournament, auth_session)
    cover_media, organizer_avatar_media = await tournament_media_descriptors(
        db_session,
        tournament,
        organizer_avatar_asset_id=organizer_avatar_asset_id,
    )
    return serialize_tournament(
        tournament,
        organizer_display_name,
        int(participant_count),
        organizer_avatar_url=organizer_avatar_url,
        cover_media=cover_media,
        organizer_avatar_media=organizer_avatar_media,
        has_locked_deadlock_roster=bool(int(locked_roster_count)),
    )
