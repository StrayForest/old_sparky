from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from python_packages.platform_domain.deadlock.constants import RANKS
from python_packages.platform_domain.tournaments import READY_CHECK_MAX_DURATION_SECONDS


class HealthResponse(BaseModel):
    status: str
    service: str


class HomePatchResponse(BaseModel):
    id: str
    title: str
    excerpt: str
    published_at: datetime
    url: str


class HomeVideoResponse(BaseModel):
    id: str
    title: str
    published_at: datetime
    url: str
    thumbnail_url: str


class HomeContentResponse(BaseModel):
    patches: list[HomePatchResponse] = Field(default_factory=list)
    videos: list[HomeVideoResponse] = Field(default_factory=list)
    generated_at: datetime
    patches_available: bool = False
    videos_available: bool = False


class DeadlockGameAssetResponse(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    image_url: str = Field(min_length=1, max_length=1000)
    source_available: bool = True


class DeadlockGameAssetsResponse(BaseModel):
    heroes: list[DeadlockGameAssetResponse] = Field(default_factory=list, max_length=200)
    ranks: list[DeadlockGameAssetResponse] = Field(default_factory=list, max_length=32)


class PatchAbilityResponse(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    icon_url: str | None = Field(default=None, max_length=1000)
    changes: list[str] = Field(default_factory=list, max_length=100)


class PatchSectionResponse(BaseModel):
    kind: Literal["general", "objective", "item", "hero"]
    title: str = Field(min_length=1, max_length=120)
    hero_name: str | None = Field(default=None, max_length=80)
    item_name: str | None = Field(default=None, max_length=120)
    item_category: Literal["weapon", "vitality", "spirit"] | None = None
    item_icon_url: str | None = Field(default=None, max_length=1000)
    objective_key: Literal["urn", "unstable_rift"] | None = None
    objective_icon_url: str | None = Field(default=None, max_length=1000)
    changes: list[str] = Field(default_factory=list, max_length=500)
    abilities: list[PatchAbilityResponse] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_kind_metadata(self) -> "PatchSectionResponse":
        if self.kind == "hero":
            if self.hero_name is None:
                raise ValueError("Hero patch sections require hero_name.")
            if any(
                value is not None
                for value in (
                    self.item_name,
                    self.item_category,
                    self.item_icon_url,
                    self.objective_key,
                    self.objective_icon_url,
                )
            ):
                raise ValueError("Hero patch sections cannot include item or objective metadata.")
            return self

        if self.kind == "item":
            if self.item_name is None or self.item_category is None:
                raise ValueError("Item patch sections require item_name and item_category.")
            if any(
                value is not None
                for value in (
                    self.hero_name,
                    self.objective_key,
                    self.objective_icon_url,
                )
            ) or self.abilities:
                raise ValueError("Item patch sections cannot include hero or objective metadata.")
            return self

        if self.kind == "objective":
            if self.objective_key is None:
                raise ValueError("Objective patch sections require objective_key.")
            if any(
                value is not None
                for value in (
                    self.hero_name,
                    self.item_name,
                    self.item_category,
                    self.item_icon_url,
                )
            ) or self.abilities:
                raise ValueError("Objective patch sections cannot include hero or item metadata.")
            return self

        if any(
            value is not None
            for value in (
                self.hero_name,
                self.item_name,
                self.item_category,
                self.item_icon_url,
                self.objective_key,
                self.objective_icon_url,
            )
        ) or self.abilities:
            raise ValueError("General patch sections cannot include entity metadata.")
        return self


class PatchDetailResponse(BaseModel):
    id: str
    title: str = Field(min_length=1, max_length=180)
    published_at: datetime
    url: str = Field(max_length=1000)
    content: str = Field(max_length=30000)
    sections: list[PatchSectionResponse] = Field(default_factory=list, max_length=100)


class SupportStatusResponse(BaseModel):
    configured: bool


class SupportMessageRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr = Field(max_length=254)
    category: Literal["account", "tournament", "technical", "rules", "other"]
    message: str = Field(min_length=10, max_length=1000)
    website: str = Field(default="", max_length=200)

    @field_validator("name", "message", mode="before")
    @classmethod
    def strip_support_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class SupportMessageResponse(BaseModel):
    accepted: bool


class RegisterRequest(BaseModel):
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(min_length=2, max_length=15)
    turnstile_token: str | None = Field(default=None, min_length=1, max_length=2048)


class LoginRequest(BaseModel):
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=10, max_length=128)
    turnstile_token: str | None = Field(default=None, min_length=1, max_length=2048)


class PasswordResetRequest(BaseModel):
    email: EmailStr = Field(max_length=254)
    turnstile_token: str | None = Field(default=None, min_length=1, max_length=2048)


class PasswordResetCodeVerifyRequest(BaseModel):
    email: EmailStr = Field(max_length=254)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class PasswordResetConfirmRequest(BaseModel):
    email: EmailStr = Field(max_length=254)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    new_password: str = Field(min_length=10, max_length=128)


class EmailVerificationConfirmRequest(BaseModel):
    email: EmailStr = Field(max_length=254)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class EmailVerificationResendRequest(BaseModel):
    email: EmailStr = Field(max_length=254)
    turnstile_token: str | None = Field(default=None, min_length=1, max_length=2048)


class SteamAuthStartRequest(BaseModel):
    return_to: str = Field(default="/", min_length=1, max_length=512)
    turnstile_token: str | None = Field(default=None, min_length=1, max_length=2048)


class SteamAuthStartResponse(BaseModel):
    authorization_url: str
    expires_at: datetime


class EmailLinkRequest(BaseModel):
    email: EmailStr = Field(max_length=254)


class EmailLinkConfirmRequest(EmailLinkRequest):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class EmailChangeRequest(EmailLinkRequest):
    current_password: str = Field(min_length=1, max_length=128)


class AccountSecurityUpdateRequest(BaseModel):
    current_password: str | None = Field(default=None, min_length=1, max_length=128)
    email: EmailStr | None = Field(default=None, max_length=254)
    new_password: str | None = Field(default=None, min_length=10, max_length=128)

    @model_validator(mode="after")
    def validate_change(self) -> "AccountSecurityUpdateRequest":
        if self.email is None and self.new_password is None:
            raise ValueError("Provide a new email or password.")
        return self


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr | None
    display_name: str
    status: str
    created_at: datetime
    roles: list[str]
    can_create_public_tournaments: bool = False
    public_tournament_credits: int = 0
    private_tournament_credits: int = 0
    private_tournament_monthly_remaining: int = Field(default=1, ge=0, le=1)
    private_tournament_monthly_limit: int = Field(default=1, ge=1, le=1)
    avatar_url: str | None = None
    avatar_media: MediaDescriptorResponse | None = None
    steam_id: str | None = None
    steam_linked: bool = False
    has_password: bool = False
    can_unlink_steam: bool = False


class AdminUserResponse(UserResponse):
    """Admin view may include retained QA users with reserved email domains."""

    email: str | None


class AdminUserTournamentCreditsUpdateRequest(BaseModel):
    public_tournament_credits: int = Field(ge=0, le=1000)
    private_tournament_credits: int = Field(ge=0, le=1000)
    note: str = Field(min_length=3, max_length=500)


class AdminUserRoleUpdateRequest(BaseModel):
    is_admin: bool
    note: str = Field(min_length=3, max_length=500)


class AuthSessionResponse(BaseModel):
    user: UserResponse
    expires_at: datetime


class RegistrationResponse(BaseModel):
    # Verified registration withholds the account object. A duplicate active
    # account receives the same accepted result as a new/pending registration,
    # so account fields cannot become an enumeration oracle.
    user: UserResponse | None = None
    expires_at: datetime | None = None
    verification_required: bool = False
    retry_after_seconds: int | None = Field(default=None, ge=1, le=3600)


class AuthActionAcceptedResponse(BaseModel):
    accepted: bool = True
    retry_after_seconds: int | None = Field(default=None, ge=1, le=3600)


class AuthSessionListItemResponse(BaseModel):
    id: str
    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    is_current: bool = False


class CsrfTokenResponse(BaseModel):
    csrf_token: str = Field(min_length=32, max_length=128)


class AuthSecurityConfigResponse(BaseModel):
    public_registration_enabled: bool
    email_verification_required: bool
    turnstile_mode: str
    turnstile_site_key: str | None = None
    steam_login_enabled: bool = False


class MediaVariantResponse(BaseModel):
    name: str
    width: int
    height: int
    byte_size: int
    url: str


class MediaDescriptorResponse(BaseModel):
    asset_id: str
    purpose: str
    status: str
    error_code: str | None = None
    variants: list[MediaVariantResponse] = Field(default_factory=list)


# UserResponse is declared before media contracts so auth contracts stay
# grouped together. Resolve that one intentional forward reference once the
# descriptor class exists.
UserResponse.model_rebuild()
AdminUserResponse.model_rebuild()
AuthSessionResponse.model_rebuild()
RegistrationResponse.model_rebuild()


class MediaAcceptedResponse(BaseModel):
    asset_id: str
    status: str
    status_url: str


class MediaDeleteAcceptedResponse(BaseModel):
    asset_id: str | None = None
    status: Literal["cleanup_pending", "deleted"]


class ProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    display_name: str
    handle: str | None
    avatar_url: str | None
    banner_url: str | None
    avatar_media: MediaDescriptorResponse | None = None
    banner_media: MediaDescriptorResponse | None = None
    bio: str | None
    contact_email: str | None
    region: str | None
    steam_id: str | None
    steam_linked: bool = False
    discord_account: str | None
    captain_team_name: str | None = None
    updated_at: datetime


class MyProfileResponse(ProfileResponse):
    account_email: EmailStr | None


class ProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=2, max_length=15)
    handle: str | None = Field(default=None, min_length=2, max_length=40)
    bio: str | None = Field(default=None, max_length=800)
    contact_email: EmailStr | None = Field(default=None, max_length=254)
    region: str | None = Field(default=None, max_length=40)
    discord_account: str | None = Field(default=None, max_length=64)
    captain_team_name: str | None = Field(default=None, max_length=15)


class DeadlockProfileUpdateRequest(BaseModel):
    rank: str = Field(min_length=3, max_length=32)
    subrank: int = Field(ge=1, le=6)
    playtime: str = Field(min_length=3, max_length=20)
    roles: list[str] = Field(min_length=1, max_length=4)
    pool: list[str] = Field(default_factory=list, max_length=12)
    captain_priority: str | None = Field(default=None, max_length=20)


class DeadlockProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    rank: str
    subrank: int
    playtime: str
    roles: list[str]
    pool: list[str]
    captain_priority: str | None
    updated_at: datetime


class PublicProfileResponse(BaseModel):
    user_id: str
    display_name: str
    handle: str
    avatar_url: str | None
    banner_url: str | None
    avatar_media: MediaDescriptorResponse | None = None
    banner_media: MediaDescriptorResponse | None = None
    bio: str | None
    region: str | None
    discord_account: str | None
    captain_team_name: str | None = None
    deadlock_profile: DeadlockProfileResponse | None = None


class TournamentCreateRequest(BaseModel):
    name: str = Field(min_length=3, max_length=25, pattern=r"^[A-Za-z0-9][A-Za-z0-9 .,'!?&#():+\-_/]*$")
    description: str | None = Field(default=None, max_length=200)
    cover_url: str | None = Field(default=None, max_length=512)
    visibility: str = Field(default="invite_only", pattern="^(public|invite_only)$")
    invite_code: str | None = Field(default=None, min_length=10, max_length=24)
    format_slug: str = Field(default="solo", pattern="^solo$")
    allowed_ranks: list[str] = Field(default_factory=list, max_length=len(RANKS))
    max_participants: int | None = Field(default=None, ge=1, le=999_999_999)
    registration_starts_at: datetime | None = None
    registration_closes_at: datetime | None = None
    ready_check_starts_at: datetime | None = None
    ready_check_ends_at: datetime | None = None
    captain_selection_starts_at: datetime | None = None
    starts_at: datetime | None = None
    match_format: str = Field(default="bo1", pattern="^(bo1|bo3|bo5)$")
    final_format: str = Field(default="bo3", pattern="^(bo1|bo3|bo5)$")
    captain_response_deadline_minutes: int | None = Field(default=None, ge=1, le=1440)
    teams_count: int | None = Field(default=None, ge=2, le=8192)

    @field_validator("cover_url")
    @classmethod
    def validate_cover_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized.startswith("/assets/tournament-covers/"):
            raise ValueError("Tournament cover must use a bundled cover template.")
        return normalized

    @field_validator("allowed_ranks")
    @classmethod
    def validate_allowed_ranks(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for rank in value:
            rank_name = str(rank).strip()
            if rank_name not in RANKS:
                raise ValueError("Allowed ranks contain an unsupported rank.")
            if rank_name not in normalized:
                normalized.append(rank_name)
        return normalized

    @field_validator(
        "registration_starts_at",
        "registration_closes_at",
        "ready_check_starts_at",
        "ready_check_ends_at",
        "captain_selection_starts_at",
        "starts_at",
    )
    @classmethod
    def validate_scheduled_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Schedule datetimes must include timezone information.")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_automation_schedule(self) -> "TournamentCreateRequest":
        if self.registration_closes_at is None and self.ready_check_starts_at is not None:
            self.registration_closes_at = self.ready_check_starts_at

        schedule_values = (
            self.registration_closes_at,
            self.ready_check_starts_at,
            self.captain_selection_starts_at,
        )
        provided_count = sum(value is not None for value in schedule_values)
        if provided_count:
            if provided_count != len(schedule_values):
                raise ValueError("Deadlock automation schedule requires ready-check start and team formation start.")
            assert self.ready_check_starts_at is not None
            assert self.captain_selection_starts_at is not None
            assert self.registration_closes_at is not None
            if self.ready_check_starts_at < self.registration_closes_at:
                raise ValueError("Ready-check start must be later than or equal to registration close.")
            effective_ready_check_ends_at = self.ready_check_ends_at or self.captain_selection_starts_at
            if effective_ready_check_ends_at <= self.ready_check_starts_at:
                raise ValueError("Team formation start must be later than ready-check start.")
            if effective_ready_check_ends_at - self.ready_check_starts_at < timedelta(minutes=10):
                raise ValueError("Ready-check duration must be at least 10 minutes.")
            if effective_ready_check_ends_at - self.ready_check_starts_at > timedelta(
                seconds=READY_CHECK_MAX_DURATION_SECONDS
            ):
                raise ValueError("Ready-check duration cannot exceed 24 hours.")
            if self.captain_selection_starts_at < effective_ready_check_ends_at:
                raise ValueError("Captain selection cannot start before ready-check ends.")
            self.ready_check_ends_at = effective_ready_check_ends_at

        ordered_dates = [
            ("registration start", self.registration_starts_at),
            ("registration close", self.registration_closes_at),
            ("ready-check start", self.ready_check_starts_at),
            ("ready-check end", self.ready_check_ends_at),
            ("captain selection start", self.captain_selection_starts_at),
            ("tournament start", self.starts_at),
        ]
        previous_label: str | None = None
        previous_value: datetime | None = None
        for label, value in ordered_dates:
            if value is None:
                continue
            if previous_value is not None and value < previous_value:
                raise ValueError(f"{label} must be later than or equal to {previous_label}.")
            previous_label = label
            previous_value = value
        return self


class TournamentResponse(BaseModel):
    id: str
    slug: str
    name: str
    description: str | None
    cover_url: str | None = None
    cover_media: MediaDescriptorResponse | None = None
    visibility: str
    invite_code: str | None = None
    status: str
    format_slug: str
    organizer_user_id: str
    organizer_display_name: str | None = None
    organizer_avatar_url: str | None = None
    organizer_avatar_media: MediaDescriptorResponse | None = None
    participant_count: int = 0
    allowed_ranks: list[str] = Field(default_factory=list)
    max_participants: int | None = None
    has_locked_deadlock_roster: bool = False
    current_user_participant_status: str | None = None
    registration_starts_at: datetime | None = None
    registration_closes_at: datetime | None = None
    ready_check_starts_at: datetime | None = None
    ready_check_ends_at: datetime | None = None
    captain_selection_starts_at: datetime | None = None
    starts_at: datetime | None = None
    match_format: str = "bo1"
    final_format: str = "bo3"
    captain_response_deadline_minutes: int | None = None
    teams_count: int | None = None
    automation_ready_check_started_at: datetime | None = None
    automation_ready_check_closed_at: datetime | None = None
    automation_captain_round_started_at: datetime | None = None
    automation_captain_round_finalized_at: datetime | None = None
    automation_assignment_generated_at: datetime | None = None
    automation_last_error: str | None = None
    automation_failure_count: int = 0
    automation_retry_after: datetime | None = None
    created_at: datetime
    available_next_statuses: list[str] = Field(default_factory=list)
    state_version: int | None = None


class TournamentStatusUpdateRequest(BaseModel):
    status: str = Field(
        pattern="^(registration_open|registration_closed|in_progress|completed|cancelled)$"
    )


class TournamentParticipantJoinRequest(BaseModel):
    entry_type: str = Field(default="solo", pattern="^solo$")
    team_name: None = None
    invite_code: str | None = Field(default=None, min_length=6, max_length=64)


class TournamentParticipantManageRequest(BaseModel):
    user_email: EmailStr = Field(max_length=254)
    entry_type: str = Field(default="solo", pattern="^solo$")
    team_name: None = None


class TournamentParticipantModerationRequest(BaseModel):
    status: str = Field(
        pattern="^(registered|confirmed|checked_in|withdrawn|disqualified)$"
    )
    moderation_note: str | None = Field(default=None, max_length=1000)


class TournamentParticipantResponse(BaseModel):
    id: str
    tournament_id: str
    user_id: str
    display_name: str
    status: str
    entry_type: str
    team_name: str | None
    created_at: datetime


class TournamentParticipantManagementResponse(TournamentParticipantResponse):
    moderation_note: str | None
    moderated_at: datetime | None
    moderated_by_user_id: str | None


class TournamentDeadlockReadyVoteRequest(BaseModel):
    choice: str = Field(pattern="^(yes|no)$")


class TournamentDeadlockReadyVoteResponse(BaseModel):
    round_id: int
    tournament_id: str
    status: str
    eligible_participant_count: int
    current_user_choice: str
    changed: bool
    server_received_at: datetime


class TournamentDeadlockReadyRoundResponse(BaseModel):
    id: int
    tournament_id: str
    status: str
    eligible_participant_count: int
    ready_count: int
    declined_count: int
    initiated_by_user_id: str | None
    created_at: datetime
    closed_at: datetime | None
    current_user_choice: str | None = None


class TournamentDeadlockReadyCheckStateResponse(BaseModel):
    active_round: TournamentDeadlockReadyRoundResponse | None = None
    latest_round: TournamentDeadlockReadyRoundResponse | None = None
    state_version: int | None = None


class TournamentDeadlockCaptainPreviewCandidateResponse(BaseModel):
    user_id: str
    display_name: str
    rank: str
    subrank: int
    playtime: str
    captain_priority: str | None
    captain_priority_bucket: int
    strength: float
    projected_team_id: str | None


class TournamentDeadlockCaptainPreviewResponse(BaseModel):
    teams_count: int
    source_ready_round_id: int | None
    ready_player_count: int
    candidates: list[TournamentDeadlockCaptainPreviewCandidateResponse] = Field(default_factory=list)


class TournamentDeadlockCaptainRoundStartRequest(BaseModel):
    teams_count: int | None = Field(default=None, ge=2, le=8192)


class TournamentDeadlockCaptainRoundRespondRequest(BaseModel):
    decision: str = Field(pattern="^(accept|decline)$")


class TournamentDeadlockCaptainEntryResponse(BaseModel):
    user_id: str
    display_name: str
    rank: str | None
    subrank: int | None
    playtime: str | None
    captain_priority: str | None
    captain_priority_bucket: int | None
    strength: float | None
    offer_order: int
    state: str
    assigned_team_id: str | None
    responded_at: datetime | None
    updated_at: datetime


class TournamentDeadlockCaptainRoundResponse(BaseModel):
    id: int
    tournament_id: str
    source_ready_round_id: int
    teams_count: int
    status: str
    candidate_count: int
    accepted_count: int
    offered_count: int
    declined_count: int
    queued_count: int
    assigned_count: int
    initiated_by_user_id: str | None
    created_at: datetime
    closed_at: datetime | None
    finalized_at: datetime | None
    can_finalize: bool
    current_user_entry: TournamentDeadlockCaptainEntryResponse | None = None
    entries: list[TournamentDeadlockCaptainEntryResponse] = Field(default_factory=list)


class TournamentDeadlockCaptainRoundStateResponse(BaseModel):
    active_round: TournamentDeadlockCaptainRoundResponse | None = None
    latest_round: TournamentDeadlockCaptainRoundResponse | None = None


class TournamentDeadlockAutoAssignmentPlayerResponse(BaseModel):
    user_id: str
    username: str | None
    rank: str
    subrank: int
    playtime: str | None
    strength: float
    pool: list[str]
    roles: list[str]


class TournamentDeadlockAutoAssignmentCaptainResponse(TournamentDeadlockAutoAssignmentPlayerResponse):
    assigned_role: str


class TournamentDeadlockAutoAssignmentSlotResponse(BaseModel):
    slot_number: int
    allowed_roles: list[str]
    desired_heroes: list[str]
    assigned_player: TournamentDeadlockAutoAssignmentPlayerResponse
    assigned_role: str
    matched_desired_heroes: list[str]
    desired_match_count: int
    role_match: bool


class TournamentDeadlockAutoAssignmentTeamResponse(BaseModel):
    team_id: str
    starter_strength: float
    starter_average_strength: float
    captain: TournamentDeadlockAutoAssignmentCaptainResponse
    starter_slots: list[TournamentDeadlockAutoAssignmentSlotResponse] = Field(default_factory=list)
    reserve_slot: TournamentDeadlockAutoAssignmentSlotResponse | None = None


class TournamentDeadlockAutoAssignmentOptimizationSummaryResponse(BaseModel):
    threshold: float
    spread_percent: float
    mad_percent: float
    std_percent: float
    candidate_pool_size: int
    selected_player_count: int
    source: str
    pool_step: float | None
    role_rescue_used: bool
    accepted_swap_moves: int
    accepted_replacement_moves: int
    accepted_hierarchy_moves: int
    stage: int | None


class TournamentDeadlockAutoAssignmentPreferenceMetricsResponse(BaseModel):
    starter_slots_total: int
    starter_preference_slots_total: int
    starter_role_restricted_slots_total: int
    starter_role_match_count: int
    starter_role_match_rate_percent: float
    starter_desired_slots_total: int
    starter_desired_slots_with_any_match: int
    starter_desired_slot_hit_rate_percent: float
    starter_desired_heroes_requested_total: int
    starter_desired_heroes_hit_total: int
    starter_desired_hero_hit_rate_percent: float
    starter_preference_slots_fully_honored: int
    starter_preference_slots_fully_honored_rate_percent: float
    reserve_slots_total: int
    reserve_desired_slots_total: int
    reserve_desired_slots_with_any_match: int


class TournamentDeadlockAutoAssignmentRunResponse(BaseModel):
    id: str
    tournament_id: str
    source_captain_round_id: int
    source_ready_round_id: int
    created_by_user_id: str | None
    status: str
    published_at: datetime | None
    published_by_user_id: str | None
    locked_at: datetime | None
    locked_by_user_id: str | None
    summary_text: str
    teams: list[TournamentDeadlockAutoAssignmentTeamResponse] = Field(default_factory=list)
    optimization_summary: TournamentDeadlockAutoAssignmentOptimizationSummaryResponse
    preference_metrics: TournamentDeadlockAutoAssignmentPreferenceMetricsResponse
    candidate_pool_user_ids: list[str] = Field(default_factory=list)
    leftover_user_ids: list[str] = Field(default_factory=list)
    is_stale: bool = False
    stale_reasons: list[str] = Field(default_factory=list)
    created_at: datetime


class TournamentDeadlockAutoAssignmentStateResponse(BaseModel):
    latest_run: TournamentDeadlockAutoAssignmentRunResponse | None = None
    published_run: TournamentDeadlockAutoAssignmentRunResponse | None = None


class TournamentDeadlockAutoAssignmentJobResponse(BaseModel):
    task_id: str
    status: str = "queued"


class PlayerTournamentCommitmentResponse(BaseModel):
    id: str
    tournament_id: str
    tournament_slug: str
    tournament_name: str
    assignment_run_id: str
    team_id: str
    team_name: str
    activated_at: datetime


class TournamentWorkspaceResponse(BaseModel):
    tournament: TournamentResponse
    # Captured by the same HTTP response that carries the schedule. The web
    # client uses it as the origin of a monotonic, server-relative timer.
    server_time: datetime
    current_user: UserResponse | None = None
    current_user_active_commitment: PlayerTournamentCommitmentResponse | None = None
    participants: list[TournamentParticipantResponse] = Field(default_factory=list)
    participants_total: int = 0
    participants_limit: int = 0
    participants_offset: int = 0
    participants_has_more: bool = False
    participants_available: bool = False
    bracket: TournamentBracketResponse | None = None
    ready_check: TournamentDeadlockReadyCheckStateResponse | None = None
    auto_assignment: TournamentDeadlockAutoAssignmentStateResponse | None = None
    state_version: int | None = None


class DeadlockDreamSlotUpdateRequest(BaseModel):
    allowed_roles: list[str] = Field(default_factory=list, max_length=4)
    desired_heroes: list[str] = Field(default_factory=list, max_length=5)


class DeadlockDreamSlotBulkItemRequest(DeadlockDreamSlotUpdateRequest):
    slot_number: int = Field(ge=1, le=6)


class DeadlockDreamSlotsBulkUpdateRequest(BaseModel):
    slots: list[DeadlockDreamSlotBulkItemRequest] = Field(default_factory=list, max_length=6)


class DeadlockDreamSlotResponse(BaseModel):
    user_id: str
    slot_number: int
    allowed_roles: list[str]
    desired_heroes: list[str]
    updated_at: datetime | None = None


class StatsRankDistributionItemResponse(BaseModel):
    rank: str
    count: int


class PlatformStatsOverviewResponse(BaseModel):
    total_tournaments: int
    completed_tournaments: int
    active_upcoming_tournaments: int
    registered_participants: int
    completed_matches: int
    deadlock_profiles_total: int
    registered_participants_with_deadlock_profile: int
    deadlock_profile_coverage_percent: float
    deadlock_rank_distribution: list[StatsRankDistributionItemResponse] = Field(default_factory=list)


class TournamentProfileResponse(BaseModel):
    """Profile fields intentionally shared with tournament participants.

    Account contact email remains private. Steam ID is deliberately visible
    within the tournament profile scope.
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    display_name: str
    handle: str | None
    avatar_url: str | None
    banner_url: str | None
    avatar_media: MediaDescriptorResponse | None = None
    banner_media: MediaDescriptorResponse | None = None
    bio: str | None
    region: str | None
    steam_id: str | None
    steam_linked: bool = False
    discord_account: str | None
    captain_team_name: str | None = None
    updated_at: datetime


class TournamentScopedProfileResponse(BaseModel):
    profile: TournamentProfileResponse
    deadlock_profile: DeadlockProfileResponse | None = None


class TournamentInviteCreateRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    max_uses: int = Field(default=1, ge=1, le=500)
    expires_at: datetime | None = None


class TournamentInviteCodeAvailabilityResponse(BaseModel):
    code: str
    available: bool


class TournamentInviteClaimRequest(BaseModel):
    code: str = Field(min_length=6, max_length=64)
    entry_type: str = Field(default="solo", pattern="^solo$")
    team_name: None = None


class TournamentInviteResponse(BaseModel):
    id: str
    tournament_id: str
    code: str
    note: str | None
    max_uses: int
    use_count: int
    remaining_uses: int
    expires_at: datetime | None
    revoked_at: datetime | None
    last_claimed_by_user_id: str | None
    last_claimed_at: datetime | None
    created_at: datetime
    is_active: bool


class TournamentInviteRedeemResponse(BaseModel):
    tournament: TournamentResponse
    participant: TournamentParticipantResponse | None = None
    invite: TournamentInviteResponse


class TournamentMatchCreateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=120)
    round_number: int = Field(default=1, ge=1, le=64)
    sequence_number: int = Field(default=1, ge=1, le=512)
    home_label: str = Field(min_length=2, max_length=120)
    away_label: str = Field(min_length=2, max_length=120)
    scheduled_at: datetime | None = None


class TournamentMatchStatusUpdateRequest(BaseModel):
    status: str = Field(pattern="^(scheduled|live|cancelled)$")
    expected_revision: int | None = Field(default=None, ge=0)


class TournamentMatchScheduleUpdateRequest(BaseModel):
    scheduled_at: datetime | None = None
    expected_revision: int | None = Field(default=None, ge=0)


class TournamentMatchReportRequest(BaseModel):
    home_score: int = Field(ge=0, le=99)
    away_score: int = Field(ge=0, le=99)
    note: str | None = Field(default=None, max_length=1000)
    expected_revision: int | None = Field(default=None, ge=0)


class TournamentMatchResponse(BaseModel):
    id: str
    tournament_id: str
    title: str | None
    round_number: int
    sequence_number: int
    home_label: str
    away_label: str
    home_team_id: str | None = None
    away_team_id: str | None = None
    winner_team_id: str | None = None
    home_source_match_id: str | None = None
    away_source_match_id: str | None = None
    scheduled_at: datetime | None
    status: str
    home_score: int | None
    away_score: int | None
    winner_side: str | None
    report_note: str | None
    reported_by_user_id: str | None
    reported_at: datetime | None
    created_at: datetime
    available_next_statuses: list[str] = Field(default_factory=list)


class TournamentBracketTeamMemberResponse(BaseModel):
    user_id: str
    handle: str
    avatar_url: str | None = None
    rank: str | None = None
    subrank: int | None = None
    is_captain: bool = False
    is_substitute: bool = False


class TournamentBracketTeamResponse(BaseModel):
    id: str
    name: str
    seed: int | None = None
    starter_strength: float | None = None
    starter_average_strength: float | None = None
    captain_id: str | None = None
    color: str | None = None
    emblem: str | None = None
    members: list[TournamentBracketTeamMemberResponse] = Field(default_factory=list)


class TournamentBracketMatchResponse(BaseModel):
    id: str
    round_number: int
    match_order: int
    sequence_number: int
    team_a_id: str | None = None
    team_b_id: str | None = None
    home_label: str
    away_label: str
    score_a: int | None = None
    score_b: int | None = None
    home_score: int | None = None
    away_score: int | None = None
    winner_team_id: str | None = None
    winner_side: str | None = None
    home_source_match_id: str | None = None
    away_source_match_id: str | None = None
    status: str
    match_format: str
    ready: bool
    scheduled_at: datetime | None = None


class TournamentBracketCapabilitiesResponse(BaseModel):
    """Authoritative actions available for the current tournament viewer."""

    can_manage: bool = False
    can_schedule_matches: bool = False
    can_report_matches: bool = False


class TournamentBracketResponse(BaseModel):
    tournament_id: str
    tournament_status: Literal[
        "registration_open",
        "registration_closed",
        "in_progress",
        "completed",
        "cancelled",
    ]
    status: str
    revision: int = 0
    can_manage: bool = False
    capabilities: TournamentBracketCapabilitiesResponse = Field(
        default_factory=TournamentBracketCapabilitiesResponse,
    )
    teams: list[TournamentBracketTeamResponse] = Field(default_factory=list)
    matches: list[TournamentBracketMatchResponse] = Field(default_factory=list)


class AuditLogResponse(BaseModel):
    id: int
    action: str
    subject_type: str
    subject_id: str | None
    payload: dict[str, Any]
    created_at: datetime


class AdminAuditLogResponse(AuditLogResponse):
    actor_display_name: str | None = None
    actor_email: EmailStr | None = None


class AdminOverviewResponse(BaseModel):
    users_total: int
    tournaments_total: int
    tournaments_attention_total: int = 0
    audit_events_total: int
    preprod_test_runs_total: int = 0
    preprod_test_users_total: int = 0


class AdminTournamentResponse(TournamentResponse):
    match_count: int = 0
    latest_round_number: int | None = None
    unfinished_match_count: int = 0
    completed_match_count: int = 0
    cancelled_match_count: int = 0
    admin_override_warning: str | None = None
    admin_recovery_hint: str | None = None


class AdminTournamentOverrideRequest(BaseModel):
    status: str | None = Field(
        default=None,
        pattern="^(registration_open|registration_closed|in_progress|completed|cancelled)$",
    )
    visibility: str | None = Field(default=None, pattern="^(public|invite_only)$")
    registration_closes_at: datetime | None = None
    ready_check_starts_at: datetime | None = None
    ready_check_ends_at: datetime | None = None
    captain_selection_starts_at: datetime | None = None
    starts_at: datetime | None = None
    note: str | None = Field(default=None, max_length=1000)

    @field_validator(
        "registration_closes_at",
        "ready_check_starts_at",
        "ready_check_ends_at",
        "captain_selection_starts_at",
        "starts_at",
    )
    @classmethod
    def validate_override_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Schedule datetimes must include timezone information.")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_registration_reopen_schedule(self) -> "AdminTournamentOverrideRequest":
        schedule = (
            self.registration_closes_at,
            self.ready_check_starts_at,
            self.ready_check_ends_at,
            self.captain_selection_starts_at,
            self.starts_at,
        )
        if self.status == "registration_open":
            if any(value is None for value in schedule):
                raise ValueError(
                    "Opening registration requires new registration close, ready-check start/end, "
                    "captain selection, and tournament start dates."
                )
            ordered = [value for value in schedule if value is not None]
            if any(right <= left for left, right in zip(ordered, ordered[1:])):
                raise ValueError("New tournament workflow dates must be strictly increasing.")
            assert self.ready_check_starts_at is not None
            assert self.ready_check_ends_at is not None
            if self.ready_check_ends_at - self.ready_check_starts_at < timedelta(minutes=10):
                raise ValueError("Ready-check duration must be at least 10 minutes.")
            if self.ready_check_ends_at - self.ready_check_starts_at > timedelta(
                seconds=READY_CHECK_MAX_DURATION_SECONDS
            ):
                raise ValueError("Ready-check duration cannot exceed 24 hours.")
        elif any(value is not None for value in schedule):
            raise ValueError("Workflow dates can be changed only while opening registration.")
        return self


class AdminTournamentDeleteRequest(BaseModel):
    confirmation_name: str = Field(min_length=1, max_length=120)
    note: str = Field(min_length=3, max_length=1000)


class AdminRosterMemberResponse(BaseModel):
    id: str
    user_id: str
    display_name: str
    handle: str | None = None
    participant_status: str | None = None
    slot_number: int
    roster_role: Literal["captain", "starter", "substitute"]
    assigned_role: str | None = None
    strength: float
    rank: str | None = None
    subrank: int | None = None


class AdminRosterTeamResponse(BaseModel):
    id: str
    team_key: str
    name: str
    captain_user_id: str | None = None
    starter_strength: float
    starter_average_strength: float
    members: list[AdminRosterMemberResponse] = Field(default_factory=list)


class AdminRosterUnassignedParticipantResponse(BaseModel):
    participant_id: str
    user_id: str
    display_name: str
    handle: str | None = None
    status: str | None = None
    rank: str | None = None
    subrank: int | None = None
    playtime: str | None = None
    strength: float | None = None


class AdminRosterBracketResponse(BaseModel):
    exists: bool = False
    revision: int = 0
    match_count: int = 0
    started_count: int = 0
    completed_count: int = 0


class AdminRosterCapabilitiesResponse(BaseModel):
    can_add_player: bool = False
    can_remove_player: bool = False
    can_move_player: bool = False
    can_replace_player: bool = False
    can_change_captain: bool = False
    requires_override: bool = False
    can_override: bool = False
    blocked_reason: str | None = None


class AdminRosterResponse(BaseModel):
    tournament_id: str
    tournament_slug: str
    tournament_status: Literal[
        "registration_open",
        "registration_closed",
        "in_progress",
        "completed",
        "cancelled",
    ]
    active_participant_count: int = 0
    state_version: int
    source_assignment_run_id: str | None = None
    source_assignment_status: str | None = None
    locked: bool = False
    manually_modified: bool = False
    last_modified_at: datetime | None = None
    bracket: AdminRosterBracketResponse = Field(default_factory=AdminRosterBracketResponse)
    teams: list[AdminRosterTeamResponse] = Field(default_factory=list)
    unassigned_participants: list[AdminRosterUnassignedParticipantResponse] = Field(
        default_factory=list
    )
    capabilities: AdminRosterCapabilitiesResponse = Field(
        default_factory=AdminRosterCapabilitiesResponse
    )


class AdminRosterMutationRequest(BaseModel):
    expected_state_version: int = Field(ge=0)
    reason: str = Field(min_length=3, max_length=1000)
    override: bool = False


class AdminRosterAddPlayerRequest(AdminRosterMutationRequest):
    team_key: str = Field(min_length=1, max_length=20)
    user_id: str = Field(min_length=1, max_length=36)
    slot_number: int = Field(ge=1, le=6)
    assigned_role: Literal["Carry", "Semi-Carry", "Support", "Semi-Support"] | None = None


class AdminRosterRemovePlayerRequest(AdminRosterMutationRequest):
    team_key: str = Field(min_length=1, max_length=20)
    user_id: str = Field(min_length=1, max_length=36)


class AdminRosterMovePlayerRequest(AdminRosterMutationRequest):
    team_key: str = Field(min_length=1, max_length=20)
    user_id: str = Field(min_length=1, max_length=36)
    destination_team_key: str = Field(min_length=1, max_length=20)
    destination_slot: int = Field(ge=1, le=6)


class AdminRosterReplacePlayerRequest(AdminRosterMutationRequest):
    team_key: str = Field(min_length=1, max_length=20)
    slot_number: int = Field(ge=0, le=6)
    replacement_user_id: str = Field(min_length=1, max_length=36)
    assigned_role: Literal["Carry", "Semi-Carry", "Support", "Semi-Support"] | None = None


class AdminRosterChangeCaptainRequest(AdminRosterMutationRequest):
    team_key: str = Field(min_length=1, max_length=20)
    user_id: str = Field(min_length=1, max_length=36)
    assigned_role: Literal["Carry", "Semi-Carry", "Support", "Semi-Support"] | None = None


class AdminPreprodTestRunResponse(BaseModel):
    marker: str
    status: str
    origin: str | None = None
    requested_users: int = 0
    created_users: int = 0
    tournaments_created: int = 0
    active_participants: int = 0
    teams_count: int = 0
    matches_count: int = 0
    report_path: str | None = None
    report: dict[str, Any] = Field(default_factory=dict)
    cleanup_state: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AdminPreprodCleanupRequest(BaseModel):
    confirm: str = Field(pattern="^DELETE_TEST_DATA$")
    note: str = Field(min_length=3, max_length=500)


class AdminPreprodCleanupResponse(BaseModel):
    ok: bool
    runs_updated: int = 0
    tournaments_deleted: int = 0
    users_deleted: int = 0
    audit_logs_deleted: int = 0
    markers: list[str] = Field(default_factory=list)
    remaining_users: int = 0
    remaining_tournaments: int = 0
