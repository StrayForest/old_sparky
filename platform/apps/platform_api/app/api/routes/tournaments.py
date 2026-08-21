from __future__ import annotations

import asyncio
from dataclasses import dataclass
import secrets
import string
import time
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, case, cast, delete, exists, func, literal, or_, select, union
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
    ProfileResponse,
    TournamentCreateRequest,
    TournamentInviteClaimRequest,
    TournamentInviteCodeAvailabilityResponse,
    TournamentInviteCreateRequest,
    TournamentInviteRedeemResponse,
    TournamentInviteResponse,
    TournamentBracketMatchResponse,
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
from apps.platform_api.app.services.bracket_events import (
    publish_bracket_event,
    stream_bracket_events,
)
from apps.platform_api.app.services.brackets import (
    advance_revision,
    bracket_event_payload,
    clear_match_result_and_progression,
    create_full_bracket_graph,
    ensure_expected_revision,
    lock_tournament_for_bracket,
    propagate_match_winner,
)
from apps.platform_api.app.services.player_commitments import (
    PlayerCommitmentConflict,
    create_assignment_commitments,
    lock_commitment_users,
    reactivate_team_commitments,
    release_active_commitments,
)
from apps.platform_api.app.services.tournament_allowances import (
    PRIVATE_TOURNAMENT_MONTHLY_LIMIT,
    private_tournament_monthly_remaining,
)
from python_packages.platform_domain.deadlock import (
    AutoAssignmentEngine,
    AutoAssignmentError,
    AutoAssignmentRunFreshness,
    AutoAssignmentRunWorkflowError,
    CaptainRoundState,
    DEFAULT_TEAM_COUNT_LIMIT,
    normalize_requested_teams_count,
    resolve_effective_teams_count,
    assign_captain_team_numbers,
    ReadyCheckRoundState,
    build_auto_assignment_input_fingerprint,
    build_captain_team_dream_slot_rows,
    build_captain_preview,
    calculate_player_strength,
    captain_priority_bucket,
    evaluate_auto_assignment_run_freshness,
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
    ensure_tournament_capacity_allows_join,
    ensure_tournament_rank_allows_join,
    ensure_organizer_can_moderate_participants,
    can_self_join_tournament,
    can_self_leave_tournament,
    eliminated_team_id_for_single_elimination,
    ensure_deadlock_match_staging_allowed,
    ensure_match_admin_actions_allowed,
    ensure_match_deletion_allowed,
    ensure_match_report_allowed,
    ensure_match_round_staging_allowed,
    ensure_match_schedule_allowed,
    ensure_tournament_completion_has_final_result,
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
    transition_tournament_status,
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
    TournamentDeadlockReadyVoteCountShard,
    TournamentInvite,
    TournamentInviteAccess,
    TournamentMatch,
    TournamentParticipant,
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
from python_packages.platform_infra.performance import measure_compute_block
from python_packages.platform_infra.slugs import unique_slug_from_name

router = APIRouter()

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
POLLING_DISABLED_MS = 0
PUBLIC_SUMMARY_POLL_MS = 45_000
WORKSPACE_VIEWER_POLL_MS = 45_000
WORKSPACE_PARTICIPANT_POLL_MS = 15_000
WORKSPACE_MANAGER_POLL_MS = 20_000
BRACKET_ACTIVE_POLL_MS = 12_000
BRACKET_IDLE_POLL_MS = 30_000
READY_CHECK_ACTIVE_PARTICIPANT_POLL_MS = 5_000
READY_CHECK_ACTIVE_VIEWER_POLL_MS = 15_000
TERMINAL_TOURNAMENT_STATUSES = frozenset(("completed", "cancelled"))


def tournament_poll_state_version(
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


def tournament_summary_poll_delay_ms(tournament: Tournament) -> int:
    if tournament.status in TERMINAL_TOURNAMENT_STATUSES:
        return POLLING_DISABLED_MS
    return PUBLIC_SUMMARY_POLL_MS


def tournament_workspace_poll_delay_ms(
    tournament: Tournament,
    *,
    has_participant_record: bool,
    can_manage: bool,
) -> int:
    if tournament.status in TERMINAL_TOURNAMENT_STATUSES:
        return POLLING_DISABLED_MS
    if can_manage:
        return WORKSPACE_MANAGER_POLL_MS
    if has_participant_record:
        return WORKSPACE_PARTICIPANT_POLL_MS
    return WORKSPACE_VIEWER_POLL_MS


def tournament_bracket_poll_delay_ms(
    tournament: Tournament,
    *,
    bracket_status: str,
) -> int:
    if tournament.status in TERMINAL_TOURNAMENT_STATUSES:
        return POLLING_DISABLED_MS
    if bracket_status == "ready":
        return BRACKET_ACTIVE_POLL_MS
    return BRACKET_IDLE_POLL_MS


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


def ready_check_poll_delay_ms(
    *,
    active_round: TournamentDeadlockReadyRoundResponse | None,
    latest_round: TournamentDeadlockReadyRoundResponse | None,
    has_participant_context: bool = True,
) -> int:
    if active_round is not None:
        return (
            READY_CHECK_ACTIVE_PARTICIPANT_POLL_MS
            if has_participant_context
            else READY_CHECK_ACTIVE_VIEWER_POLL_MS
        )
    if latest_round is not None and latest_round.status == "closed":
        return POLLING_DISABLED_MS
    return POLLING_DISABLED_MS


@dataclass(frozen=True, slots=True)
class DeadlockAutoAssignmentInputs:
    captain_round: TournamentDeadlockCaptainRound
    captain_rows: tuple[dict[str, Any], ...]
    ready_player_rows: tuple[dict[str, Any], ...]
    dream_slot_rows: tuple[dict[str, Any], ...]
    input_fingerprint: dict[str, list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class BracketResponseCacheEntry:
    expires_at: float
    response: TournamentBracketResponse


@dataclass(frozen=True, slots=True)
class ReadyRoundStateSnapshot:
    response: TournamentDeadlockReadyRoundResponse
    choices_by_user_id: dict[str, str]


@dataclass(frozen=True, slots=True)
class ReadyCheckStateCacheEntry:
    expires_at: float
    active_round: ReadyRoundStateSnapshot | None
    latest_round: ReadyRoundStateSnapshot | None


@dataclass(frozen=True, slots=True)
class ParticipantJoinPreflight:
    tournament: Tournament
    has_existing_participant: bool
    active_participant_count: int
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
    bracket_keys = [key for key in _bracket_response_cache if key[0] == tournament_id]
    for key in bracket_keys:
        _bracket_response_cache.pop(key, None)
    _invalidate_participant_page_cache(tournament_id)
    _invalidate_ready_check_state_cache(tournament_id)


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
    has_participant_context: bool = True,
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
        next_poll_after_ms=ready_check_poll_delay_ms(
            active_round=active_round,
            latest_round=latest_round,
            has_participant_context=has_participant_context,
        ),
        state_version=ready_check_state_version(active_round, latest_round),
    )


def tournament_with_counts_stmt(
    tournament_page=None,
) -> Select[tuple[Tournament, str, str | None, str | None, int, int]]:
    participant_counts_stmt = select(
        TournamentParticipant.tournament_id.label("tournament_id"),
        func.count(TournamentParticipant.id).label("participant_count"),
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
        next_poll_after_ms=tournament_summary_poll_delay_ms(tournament),
        state_version=tournament_poll_state_version(
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
    count = await db_session.scalar(
        select(func.count()).select_from(TournamentParticipant).where(
            TournamentParticipant.tournament_id == tournament_id
        )
    )
    return int(count or 0)


async def active_participant_count_for_tournament(db_session: AsyncSession, tournament_id: str) -> int:
    count = await db_session.scalar(
        select(func.count()).select_from(TournamentParticipant).where(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
        )
    )
    return int(count or 0)


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
    active_count_subquery = (
        select(func.count())
        .select_from(TournamentParticipant)
        .where(
            TournamentParticipant.tournament_id == tournament.id,
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
        )
        .scalar_subquery()
    )
    player_rank_subquery = (
        select(DeadlockProfile.rank)
        .where(DeadlockProfile.user_id == user_id)
        .scalar_subquery()
    )
    active_count, player_rank = (
        await db_session.execute(
            select(
                active_count_subquery.label("active_participant_count"),
                player_rank_subquery.label("player_rank"),
            )
        )
    ).one()
    try:
        ensure_tournament_capacity_allows_join(
            max_participants=tournament.max_participants,
            active_participant_count=int(active_count or 0),
        )
        ensure_tournament_rank_allows_join(
            allowed_ranks=tournament.allowed_ranks,
            player_rank=player_rank,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def ensure_participant_join_limits_from_values(
    *,
    tournament: Tournament,
    active_participant_count: int,
    player_rank: str | None,
) -> None:
    try:
        ensure_tournament_capacity_allows_join(
            max_participants=tournament.max_participants,
            active_participant_count=active_participant_count,
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
    active_count = (
        select(func.count())
        .select_from(TournamentParticipant)
        .where(
            TournamentParticipant.tournament_id == Tournament.id,
            TournamentParticipant.status.not_in(INACTIVE_PARTICIPANT_STATUSES),
        )
        .scalar_subquery()
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
                active_count.label("active_participant_count"),
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
        active_participant_count=int(row.active_participant_count or 0),
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


async def tournament_has_locked_deadlock_roster(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
) -> bool:
    if not is_solo_tournament_format(tournament.format_slug):
        return False
    return (
        await deadlock_locked_auto_assignment_run_for_tournament(
            db_session,
            tournament_id=tournament.id,
        )
    ) is not None


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
        status=bracket_status,
        revision=int(tournament.bracket_revision or 0),
        can_manage=can_manage,
        teams=teams,
        matches=[
            serialize_bracket_match_projection(
                match,
                tournament=tournament,
                total_rounds=total_rounds,
            )
            for match in match_rows
        ],
        next_poll_after_ms=tournament_bracket_poll_delay_ms(
            tournament,
            bracket_status=bracket_status,
        ),
        state_version=int(tournament.bracket_revision or 0),
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
        status=bracket_status,
        revision=int(tournament.bracket_revision or 0),
        can_manage=can_manage,
        teams=teams,
        matches=[],
        next_poll_after_ms=tournament_bracket_poll_delay_ms(
            tournament,
            bracket_status=bracket_status,
        ),
        state_version=int(tournament.bracket_revision or 0),
    )


def build_tournament_workspace_bracket_summary_response(
    *,
    tournament: Tournament,
    can_manage: bool,
) -> TournamentBracketResponse:
    bracket_status = "ready" if int(tournament.bracket_revision or 0) > 0 else "pending"
    return TournamentBracketResponse(
        tournament_id=tournament.id,
        status=bracket_status,
        revision=int(tournament.bracket_revision or 0),
        can_manage=can_manage,
        teams=[],
        matches=[],
        next_poll_after_ms=tournament_bracket_poll_delay_ms(
            tournament,
            bracket_status=bracket_status,
        ),
        state_version=int(tournament.bracket_revision or 0),
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
    has_participant_context: bool = True,
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
                has_participant_context=has_participant_context,
            )

    if cache_revision > 0:
        async with _ready_check_state_cache_lock(cache_key):
            cached_state = _get_ready_check_state_cache(cache_key)
            if cached_state is not None:
                return _ready_check_state_response_from_cache(
                    cached_state,
                    current_user_id=current_user_id,
                    has_participant_context=has_participant_context,
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
                    has_participant_context=has_participant_context,
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
        next_poll_after_ms=ready_check_poll_delay_ms(
            active_round=active_response,
            latest_round=latest_response,
            has_participant_context=has_participant_context,
        ),
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
        tournament_id=tournament.id,
        user_id=user.id,
        entry_type=entry_type,
        status="registered",
        team_name=team_name,
    )
    db_session.add(participant)
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
        .where(TournamentParticipant.tournament_id == Tournament.id)
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
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentResponse:
    try:
        ensure_supported_tournament_format(payload.format_slug)
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    ensure_tournament_schedule_is_future(payload, now=auth_session.now)
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
    if not invite_is_active(
        max_uses=invite.max_uses,
        use_count=invite.use_count,
        revoked_at=invite.revoked_at,
        expires_at=invite.expires_at,
        now=auth_session.now,
    ) and access is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Invite code is not active.")

    if access is None:
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
    has_locked_deadlock_roster = await tournament_has_locked_deadlock_roster(
        db_session,
        tournament=tournament,
    )

    current_status = tournament.status
    try:
        tournament.status = transition_tournament_status(
            current_status,
            payload.status,
            format_slug=tournament.format_slug,
            has_locked_deadlock_roster=has_locked_deadlock_roster,
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    if tournament.status == "completed":
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
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="All matches must be completed or cancelled before finishing the tournament.",
            )
        try:
            ensure_tournament_completion_has_final_result(existing_match_states)
        except TournamentWorkflowError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if tournament.status == "registration_closed":
        tournament.registration_closes_at = auth_session.now

    released_commitments = 0
    if tournament.status in {"completed", "cancelled"}:
        released_commitments = await release_active_commitments(
            db_session,
            tournament_id=tournament.id,
            released_at=auth_session.now,
            release_reason=(
                "tournament_completed"
                if tournament.status == "completed"
                else "tournament_cancelled"
            ),
        )

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="tournament.status.update",
        subject_type="tournament",
        subject_id=tournament.id,
        payload={
            "tournament_slug": tournament.slug,
            "from_status": current_status,
            "to_status": tournament.status,
            "released_commitments": released_commitments,
        },
    )
    await db_session.commit()
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
        has_locked_deadlock_roster=has_locked_deadlock_roster,
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
    participant = await create_participant(
        db_session,
        tournament=tournament,
        user=target_user,
        entry_type=payload.entry_type,
        team_name=team_name,
    )
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
    _invalidate_participant_page_cache(tournament.id)
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

    participant = await get_participant_or_404(
        db_session,
        tournament_id=tournament.id,
        participant_id=participant_id,
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
        },
    )
    await db_session.execute(
        delete(TournamentParticipant).where(TournamentParticipant.id == participant.id)
    )
    await db_session.commit()
    _invalidate_participant_page_cache(tournament.id)
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
    _invalidate_participant_page_cache(tournament.id)
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
    await check_invite_rate_limit(
        request,
        user_id=auth_session.user.id,
        operation="manage",
    )
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
    auth_session=Depends(get_optional_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
    teams_view: Literal["full", "summary"] = Query(default="full"),
) -> TournamentBracketResponse:
    tournament = await get_tournament_or_404(db_session, slug)
    return await build_tournament_bracket_response(
        db_session,
        tournament=tournament,
        auth_session=auth_session,
        include_team_members=teams_view == "full",
    )


@router.get("/{slug}/bracket/events")
async def get_tournament_bracket_events(
    slug: str,
    auth_session=Depends(get_optional_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
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
    return StreamingResponse(
        stream_bracket_events(tournament.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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
    await publish_bracket_event(
        tournament.id,
        bracket_event_payload(
            tournament=tournament,
            event_type="bracket_seeded",
        ),
    )
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
    await publish_bracket_event(
        tournament.id,
        bracket_event_payload(
            tournament=tournament,
            event_type="bracket_round_seeded",
        ),
    )
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

    match = TournamentMatch(
        tournament_id=tournament.id,
        title=(payload.title or "").strip() or None,
        round_number=payload.round_number,
        sequence_number=payload.sequence_number,
        home_label=home_label,
        away_label=away_label,
        home_team_id=deadlock_team_id_from_match_label(home_label),
        away_team_id=deadlock_team_id_from_match_label(away_label),
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
    await publish_bracket_event(
        tournament.id,
        bracket_event_payload(
            tournament=tournament,
            event_type="match_created",
            match_id=match.id,
        ),
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
    await publish_bracket_event(
        tournament.id,
        bracket_event_payload(
            tournament=tournament,
            event_type="match_status_changed",
            match_id=match.id,
        ),
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
    await publish_bracket_event(
        tournament.id,
        bracket_event_payload(
            tournament=tournament,
            event_type="match_schedule_changed",
            match_id=match.id,
        ),
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
        previous_tournament_status = tournament.status
        tournament.status = "completed"
        released_commitments = await release_active_commitments(
            db_session,
            tournament_id=tournament.id,
            released_at=auth_session.now,
            release_reason="tournament_completed",
        )
        await write_audit_log(
            db_session,
            actor_user_id=auth_session.user.id,
            action="tournament.status.auto_complete",
            subject_type="tournament",
            subject_id=tournament.id,
            payload={
                "tournament_slug": tournament.slug,
                "from_status": previous_tournament_status,
                "to_status": tournament.status,
                "final_match_id": match.id,
                "winner_team_id": match.winner_team_id,
                "released_commitments": released_commitments,
            },
        )
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
    await publish_bracket_event(
        tournament.id,
        bracket_event_payload(
            tournament=tournament,
            event_type="tournament_completed" if is_final_match else "match_reported",
            match_id=match.id,
        ),
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
    await publish_bracket_event(
        tournament.id,
        bracket_event_payload(
            tournament=tournament,
            event_type="match_deleted",
            match_id=match.id,
        ),
    )
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
        has_participant_context=preflight.has_participant,
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
    route_preflight = await deadlock_ready_vote_route_preflight(
        db_session,
        slug=slug,
        user_id=current_user_id,
    )
    tournament = route_preflight.tournament
    ensure_deadlock_tournament_format(tournament)
    preflight = ReadyVotePreflight(
        active_round=route_preflight.active_round,
        has_participant=route_preflight.has_participant,
        has_deadlock_profile=route_preflight.has_deadlock_profile,
        has_locked_roster=route_preflight.has_locked_roster,
    )
    try:
        ensure_deadlock_roster_staging_allowed(
            format_slug=tournament.format_slug,
            tournament_status=tournament.status,
            has_locked_deadlock_roster=preflight.has_locked_roster,
            action_name="Deadlock ready-check voting",
        )
    except TournamentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not preflight.has_participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only joined participants can vote in deadlock ready-check.",
        )
    active_round = preflight.active_round
    if ready_vote_requires_automation(tournament, active_round, now=auth_session.now):
        from apps.platform_api.app.services.deadlock_automation import advance_deadlock_tournament_automation

        await advance_deadlock_tournament_automation(
            db_session,
            tournament=tournament,
            now=auth_session.now,
            allow_assignment_generation=False,
        )
        preflight = await deadlock_ready_vote_preflight(
            db_session,
            tournament_id=tournament.id,
            user_id=current_user_id,
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
        await db_session.commit()
    return TournamentDeadlockReadyVoteResponse(
        round_id=active_round.id,
        tournament_id=active_round.tournament_id,
        status=active_round.status,
        eligible_participant_count=len(list(active_round.eligible_user_ids or [])),
        current_user_choice=payload.choice,
        changed=vote_changed,
        server_received_at=auth_session.now,
    )


@router.post("/{slug}/deadlock/ready-check/close", response_model=TournamentDeadlockReadyRoundResponse)
async def close_deadlock_ready_check(
    slug: str,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> TournamentDeadlockReadyRoundResponse:
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


@router.post("/{slug}/deadlock/captain-round/respond", response_model=TournamentDeadlockCaptainRoundResponse)
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


@router.post("/{slug}/deadlock/captain-round/close", response_model=TournamentDeadlockCaptainRoundResponse)
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


@router.post("/{slug}/deadlock/captain-round/finalize", response_model=TournamentDeadlockCaptainRoundResponse)
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

    current_published = await deadlock_published_auto_assignment_run_for_tournament(
        db_session,
        tournament_id=tournament.id,
    )
    if current_published is not None and current_published.id != run_row.id and current_published.status == "locked":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The currently published roster is locked and cannot be replaced.",
        )

    if current_published is not None and current_published.id != run_row.id and current_published.status == "published":
        try:
            current_published.status = transition_auto_assignment_run_status(
                current_published.status,
                "superseded",
            )
        except AutoAssignmentRunWorkflowError as exc:
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
        active_participant_count=preflight.active_participant_count,
        player_rank=preflight.player_rank,
    )
    participant = await create_participant(
        db_session,
        tournament=tournament,
        user=auth_session.user,
        entry_type=payload.entry_type,
        team_name=team_name,
    )
    await db_session.commit()
    _invalidate_participant_page_cache(tournament.id)
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
    if participant.status in {"confirmed", "checked_in"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Confirmed participants cannot leave the tournament.",
        )
    confirmed_ready_vote_id = await db_session.scalar(
        select(TournamentDeadlockReadyVote.id)
        .join(
            TournamentDeadlockReadyRound,
            TournamentDeadlockReadyVote.round_id == TournamentDeadlockReadyRound.id,
        )
        .where(
            TournamentDeadlockReadyRound.tournament_id == tournament.id,
            TournamentDeadlockReadyVote.user_id == auth_session.user.id,
            TournamentDeadlockReadyVote.choice == "yes",
        )
    )
    if confirmed_ready_vote_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Confirmed participants cannot leave the tournament.",
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
    _invalidate_participant_page_cache(tournament.id)
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
    profile_response = ProfileResponse.model_validate(profile).model_copy(
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
) -> TournamentWorkspaceResponse:
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
        return TournamentWorkspaceResponse(
            tournament=tournament_response,
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
            next_poll_after_ms=tournament_summary_poll_delay_ms(tournament),
            state_version=tournament_response.state_version,
        )

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
            has_participant_context=participant_record is not None,
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

    return TournamentWorkspaceResponse(
        tournament=tournament_response,
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
        next_poll_after_ms=tournament_workspace_poll_delay_ms(
            tournament,
            has_participant_record=has_participant_record,
            can_manage=current_user_can_manage_bracket,
        ),
        state_version=tournament_response.state_version,
    )


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
