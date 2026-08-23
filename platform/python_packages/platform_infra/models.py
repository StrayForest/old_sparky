from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from python_packages.platform_infra.db import Base


def new_uuid() -> str:
    return str(uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, nullable=True)
    display_name: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="active")
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    public_tournament_credits: Mapped[int] = mapped_column(Integer, default=0)
    private_tournament_credits: Mapped[int] = mapped_column(Integer, default=0)


class PasswordCredential(TimestampMixin, Base):
    __tablename__ = "password_credentials"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    password_hash: Mapped[str] = mapped_column(String(512))
    password_version: Mapped[str] = mapped_column(String(32))


class ExternalIdentity(Base):
    """A provider identity linked to exactly one platform user."""

    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint(
            "provider", "subject", name="uq_external_identities_provider_subject"
        ),
        UniqueConstraint(
            "user_id", "provider", name="uq_external_identities_user_provider"
        ),
        CheckConstraint("provider = 'steam'", name="provider_steam"),
        CheckConstraint("subject ~ '^[0-9]{17}$'", name="subject_steam_id64"),
        Index("ix_external_identities_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
    )
    provider: Mapped[str] = mapped_column(String(16), default="steam")
    subject: Mapped[str] = mapped_column(String(20))
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_authenticated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SteamAuthFlow(Base):
    """Single-use, browser-bound Steam OpenID callback state."""

    __tablename__ = "steam_auth_flows"
    __table_args__ = (
        CheckConstraint("purpose IN ('login', 'link')", name="purpose_allowed"),
        CheckConstraint(
            "((purpose = 'login' AND user_id IS NULL AND session_id IS NULL) OR "
            "(purpose = 'link' AND user_id IS NOT NULL AND session_id IS NOT NULL))",
            name="purpose_owner_matches",
        ),
        CheckConstraint("length(state_digest) = 64", name="state_digest_length"),
        CheckConstraint(
            "length(browser_grant_digest) = 64", name="browser_grant_digest_length"
        ),
        Index("ix_steam_auth_flows_cleanup", "expires_at", "consumed_at"),
        Index("ix_steam_auth_flows_user_id", "user_id"),
        Index("ix_steam_auth_flows_session_id", "session_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    state_digest: Mapped[str] = mapped_column(String(64), unique=True)
    browser_grant_digest: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("platform.users.id", ondelete="CASCADE"), nullable=True
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.sessions.id", ondelete="CASCADE"),
        nullable=True,
    )
    return_path: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SteamEmailLinkIntent(Base):
    """Pending email attachment for a Steam-only account; never writes User.email early."""

    __tablename__ = "steam_email_link_intents"
    __table_args__ = (
        CheckConstraint("length(code_digest) = 64", name="code_digest_length"),
        CheckConstraint(
            "length(browser_grant_digest) = 64", name="browser_grant_digest_length"
        ),
        CheckConstraint(
            "candidate_email = lower(candidate_email)",
            name="candidate_email_normalized",
        ),
        Index("ix_steam_email_link_intents_cleanup", "expires_at", "consumed_at"),
        Index("ix_steam_email_link_intents_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform.users.id", ondelete="CASCADE")
    )
    candidate_email: Mapped[str] = mapped_column(String(320))
    code_digest: Mapped[str] = mapped_column(String(64))
    browser_grant_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("platform.roles.id", ondelete="CASCADE"),
        primary_key=True,
    )


class UserSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        index=True,
    )
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ApiMutationIdempotencyKey(TimestampMixin, Base):
    __tablename__ = "api_mutation_idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id",
            "scope",
            "key",
            name="uq_api_mutation_idempotency_keys_actor_scope_key",
        ),
        CheckConstraint("length(request_fingerprint) = 64", name="request_fingerprint_length"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    actor_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        index=True,
    )
    scope: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        CheckConstraint("length(token_digest) = 64", name="token_digest_length"),
        Index("ix_password_reset_tokens_cleanup", "expires_at", "consumed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        index=True,
    )
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        CheckConstraint("length(token_digest) = 64", name="token_digest_length"),
        Index("ix_email_verification_tokens_cleanup", "expires_at", "consumed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        index=True,
    )
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MediaAsset(TimestampMixin, Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('profile_avatar', 'profile_banner', 'tournament_banner')",
            name="purpose_allowed",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed', 'replaced', 'deleted')",
            name="status_allowed",
        ),
        CheckConstraint(
            "((purpose IN ('profile_avatar', 'profile_banner') "
            "AND owner_user_id IS NOT NULL AND tournament_id IS NULL) OR "
            "(purpose = 'tournament_banner' "
            "AND owner_user_id IS NULL AND tournament_id IS NOT NULL))",
            name="ownership_matches_purpose",
        ),
        CheckConstraint("source_bytes > 0", name="source_bytes_positive"),
        CheckConstraint("length(source_sha256) = 64", name="source_sha256_length"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        Index(
            "ix_media_assets_reconciliation",
            "status",
            "next_retry_at",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    owner_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    tournament_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.tournaments.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    purpose: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    source_mime: Mapped[str] = mapped_column(String(32))
    source_bytes: Mapped[int] = mapped_column(Integer)
    source_sha256: Mapped[str] = mapped_column(String(64))
    version_id: Mapped[str] = mapped_column(String(36), default=new_uuid, unique=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cleanup_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class MediaVariant(Base):
    __tablename__ = "media_variants"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "variant_name", name="uq_media_variants_asset_variant"
        ),
        CheckConstraint("mime_type = 'image/webp'", name="mime_type_webp"),
        CheckConstraint("width > 0 AND height > 0", name="dimensions_positive"),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    asset_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.media_assets.id", ondelete="CASCADE"),
    )
    variant_name: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(512), unique=True)
    mime_type: Mapped[str] = mapped_column(String(32), default="image/webp")
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PlayerProfile(TimestampMixin, Base):
    __tablename__ = "player_profiles"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    display_name: Mapped[str] = mapped_column(String(40))
    handle: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    banner_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    avatar_asset_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.media_assets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    banner_asset_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.media_assets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    region: Mapped[str | None] = mapped_column(String(40), nullable=True)
    steam_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discord_account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    captain_team_name: Mapped[str | None] = mapped_column(String(15), nullable=True)


class DeadlockProfile(TimestampMixin, Base):
    __tablename__ = "deadlock_profiles"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    rank: Mapped[str] = mapped_column(String(32))
    subrank: Mapped[int] = mapped_column(Integer)
    playtime: Mapped[str] = mapped_column(String(20))
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    pool: Mapped[list[str]] = mapped_column(JSON, default=list)
    captain_priority: Mapped[str | None] = mapped_column(String(20), nullable=True)


class DeadlockDreamSlot(TimestampMixin, Base):
    __tablename__ = "deadlock_dream_slots"
    __table_args__ = (
        CheckConstraint(
            "slot_number BETWEEN 1 AND 6",
            name="slot_number_in_range",
        ),
        UniqueConstraint(
            "user_id",
            "slot_number",
            name="uq_deadlock_dream_slots_user_slot",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        index=True,
    )
    slot_number: Mapped[int] = mapped_column(Integer)
    allowed_roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    desired_heroes: Mapped[list[str]] = mapped_column(JSON, default=list)


class Tournament(TimestampMixin, Base):
    __tablename__ = "tournaments"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('public', 'invite_only')",
            name="visibility_allowed",
        ),
        CheckConstraint(
            "status IN ('registration_open', 'registration_closed', 'in_progress', 'completed', 'cancelled')",
            name="status_allowed",
        ),
        CheckConstraint(
            "max_participants IS NULL OR max_participants > 0",
            name="max_participants_positive",
        ),
        CheckConstraint("bracket_revision >= 0", name="bracket_revision_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    slug: Mapped[str] = mapped_column(String(140), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    banner_asset_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "platform.media_assets.id",
            name="fk_tournaments_banner_asset_id_media_assets",
            use_alter=True,
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )
    visibility: Mapped[str] = mapped_column(String(20), default="public")
    status: Mapped[str] = mapped_column(String(20), default="registration_closed")
    format_slug: Mapped[str] = mapped_column(String(64))
    allowed_ranks: Mapped[list[str]] = mapped_column(JSON, default=list)
    max_participants: Mapped[int | None] = mapped_column(Integer, nullable=True)
    registration_starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    registration_closes_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ready_check_starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ready_check_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    captain_selection_starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    match_format: Mapped[str] = mapped_column(String(20), default="bo1")
    final_format: Mapped[str] = mapped_column(String(20), default="bo3")
    bracket_revision: Mapped[int] = mapped_column(Integer, default=0)
    captain_response_deadline_minutes: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    teams_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    automation_ready_check_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    automation_ready_check_closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    automation_captain_round_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    automation_captain_round_finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    automation_assignment_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    automation_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    automation_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    automation_retry_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    organizer_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="RESTRICT"),
        index=True,
    )


Index(
    "uq_tournaments_public_name_normalized",
    func.lower(func.btrim(Tournament.name)),
    unique=True,
    postgresql_where=Tournament.visibility == "public",
)


class TournamentInviteAccess(TimestampMixin, Base):
    __tablename__ = "tournament_invite_accesses"
    __table_args__ = (
        UniqueConstraint(
            "tournament_id",
            "user_id",
            name="uq_tournament_invite_accesses_tournament_user",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tournament_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.tournaments.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        index=True,
    )
    invite_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.tournament_invites.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TournamentParticipant(TimestampMixin, Base):
    __tablename__ = "tournament_participants"
    __table_args__ = (
        UniqueConstraint(
            "tournament_id",
            "user_id",
            name="uq_tournament_participants_tournament_user",
        ),
        CheckConstraint("entry_type = 'solo'", name="entry_type_solo"),
        CheckConstraint(
            "status IN ('registered', 'confirmed', 'checked_in', 'withdrawn', 'disqualified')",
            name="status_allowed",
        ),
        Index(
            "ix_tournament_participants_active_tournament",
            "tournament_id",
            postgresql_where=text("status NOT IN ('withdrawn', 'disqualified')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tournament_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.tournaments.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        index=True,
    )
    entry_type: Mapped[str] = mapped_column(String(32), default="solo")
    status: Mapped[str] = mapped_column(String(20), default="registered")
    team_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    moderation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    moderated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    moderated_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class TournamentMatch(TimestampMixin, Base):
    __tablename__ = "tournament_matches"
    __table_args__ = (
        UniqueConstraint(
            "tournament_id",
            "round_number",
            "sequence_number",
            name="uq_tournament_matches_tournament_round_sequence",
        ),
        CheckConstraint("round_number > 0", name="round_number_positive"),
        CheckConstraint("sequence_number > 0", name="sequence_number_positive"),
        CheckConstraint(
            "status IN ('scheduled', 'live', 'completed', 'cancelled')",
            name="status_allowed",
        ),
        CheckConstraint(
            "winner_side IS NULL OR winner_side IN ('home', 'away')",
            name="winner_side_allowed",
        ),
        CheckConstraint(
            "home_score IS NULL OR home_score >= 0",
            name="home_score_nonnegative",
        ),
        CheckConstraint(
            "away_score IS NULL OR away_score >= 0",
            name="away_score_nonnegative",
        ),
        CheckConstraint(
            "status <> 'completed' OR ("
            "(winner_side = 'home' AND home_score IS NOT NULL AND away_score IS NOT NULL "
            "AND home_score > away_score) OR "
            "(winner_side = 'away' AND home_score IS NOT NULL AND away_score IS NOT NULL "
            "AND away_score > home_score))",
            name="completed_result_consistent",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tournament_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.tournaments.id", ondelete="CASCADE"),
        index=True,
    )
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    round_number: Mapped[int] = mapped_column(Integer, default=1)
    sequence_number: Mapped[int] = mapped_column(Integer, default=1)
    home_label: Mapped[str] = mapped_column(String(120))
    away_label: Mapped[str] = mapped_column(String(120))
    home_team_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    away_team_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    winner_team_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    home_source_match_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.tournament_matches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    away_source_match_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.tournament_matches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winner_side: Mapped[str | None] = mapped_column(String(10), nullable=True)
    report_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reported_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TournamentInvite(TimestampMixin, Base):
    __tablename__ = "tournament_invites"
    __table_args__ = (
        CheckConstraint("max_uses > 0", name="max_uses_positive"),
        CheckConstraint("use_count >= 0", name="use_count_nonnegative"),
        CheckConstraint("use_count <= max_uses", name="use_count_within_limit"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tournament_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.tournaments.id", ondelete="CASCADE"),
        index=True,
    )
    code: Mapped[str] = mapped_column(String(24), unique=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    last_claimed_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    last_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TournamentDeadlockReadyRound(TimestampMixin, Base):
    __tablename__ = "tournament_deadlock_ready_rounds"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'closed', 'stopped')",
            name="status_allowed",
        ),
        Index(
            "uq_tournament_deadlock_ready_rounds_active_tournament",
            "tournament_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_tournament_deadlock_ready_rounds_tournament_status_latest",
            "tournament_id",
            "status",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tournament_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.tournaments.id", ondelete="CASCADE"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20), default="active")
    eligible_user_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    initiated_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TournamentDeadlockReadyVote(TimestampMixin, Base):
    __tablename__ = "tournament_deadlock_ready_votes"
    __table_args__ = (
        CheckConstraint("choice IN ('yes', 'no')", name="choice_allowed"),
        Index(
            "ix_tournament_deadlock_ready_votes_round_choice",
            "round_id",
            "choice",
        ),
        UniqueConstraint(
            "round_id",
            "user_id",
            name="uq_tournament_deadlock_ready_votes_round_user",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("platform.tournament_deadlock_ready_rounds.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        index=True,
    )
    choice: Mapped[str] = mapped_column(String(10))
    responded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TournamentDeadlockReadyVoteCountShard(TimestampMixin, Base):
    __tablename__ = "tournament_deadlock_ready_vote_count_shards"
    __table_args__ = (
        CheckConstraint("choice IN ('yes', 'no')", name="choice_allowed"),
        CheckConstraint("shard BETWEEN 0 AND 31", name="shard_in_range"),
        CheckConstraint("vote_count >= 0", name="vote_count_nonnegative"),
    )

    round_id: Mapped[int] = mapped_column(
        ForeignKey("platform.tournament_deadlock_ready_rounds.id", ondelete="CASCADE"),
        primary_key=True,
    )
    choice: Mapped[str] = mapped_column(String(10), primary_key=True)
    shard: Mapped[int] = mapped_column(Integer, primary_key=True)
    vote_count: Mapped[int] = mapped_column(Integer, default=0)


class TournamentDeadlockCaptainRound(TimestampMixin, Base):
    __tablename__ = "tournament_deadlock_captain_rounds"
    __table_args__ = (
        CheckConstraint("teams_count > 0", name="teams_count_positive"),
        CheckConstraint(
            "status IN ('active', 'closed', 'finalized')",
            name="status_allowed",
        ),
        UniqueConstraint(
            "source_ready_round_id",
            name="uq_tournament_deadlock_captain_rounds_source_ready_round",
        ),
        Index(
            "uq_tournament_deadlock_captain_rounds_active_tournament",
            "tournament_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tournament_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.tournaments.id", ondelete="CASCADE"),
        index=True,
    )
    source_ready_round_id: Mapped[int] = mapped_column(
        ForeignKey("platform.tournament_deadlock_ready_rounds.id", ondelete="RESTRICT"),
        index=True,
    )
    teams_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="active")
    initiated_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TournamentDeadlockCaptainEntry(TimestampMixin, Base):
    __tablename__ = "tournament_deadlock_captain_entries"
    __table_args__ = (
        CheckConstraint("offer_order > 0", name="offer_order_positive"),
        CheckConstraint(
            "state IN ('queued', 'offered', 'accepted', 'declined', 'cancelled', 'assigned')",
            name="state_allowed",
        ),
        UniqueConstraint(
            "round_id",
            "user_id",
            name="uq_tournament_deadlock_captain_entries_round_user",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    round_id: Mapped[int] = mapped_column(
        ForeignKey(
            "platform.tournament_deadlock_captain_rounds.id", ondelete="CASCADE"
        ),
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        index=True,
    )
    offer_order: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="queued")
    assigned_team_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TournamentDeadlockAssignmentRun(TimestampMixin, Base):
    __tablename__ = "tournament_deadlock_assignment_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('generated', 'published', 'superseded', 'locked')",
            name="status_allowed",
        ),
        Index(
            "uq_tournament_deadlock_assignment_runs_current_tournament",
            "tournament_id",
            unique=True,
            postgresql_where=text("status IN ('published', 'locked')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tournament_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.tournaments.id", ondelete="CASCADE"),
        index=True,
    )
    source_captain_round_id: Mapped[int] = mapped_column(
        ForeignKey(
            "platform.tournament_deadlock_captain_rounds.id", ondelete="RESTRICT"
        ),
        index=True,
    )
    source_ready_round_id: Mapped[int] = mapped_column(
        ForeignKey("platform.tournament_deadlock_ready_rounds.id", ondelete="RESTRICT"),
        index=True,
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20), default="generated")
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    summary_text: Mapped[str] = mapped_column(Text)
    result_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    candidate_pool_user_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    leftover_user_ids: Mapped[list[str]] = mapped_column(JSON, default=list)


class PlayerTournamentCommitment(TimestampMixin, Base):
    __tablename__ = "player_tournament_commitments"
    __table_args__ = (
        CheckConstraint(
            "(released_at IS NULL AND release_reason IS NULL) OR "
            "(released_at IS NOT NULL AND release_reason IS NOT NULL)",
            name="release_state_consistent",
        ),
        Index(
            "uq_player_tournament_commitments_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        Index(
            "ix_player_tournament_commitments_active_tournament_team",
            "tournament_id",
            "team_id",
            postgresql_where=text("released_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="CASCADE"),
        index=True,
    )
    tournament_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("platform.tournaments.id", ondelete="CASCADE"),
        index=True,
    )
    assignment_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "platform.tournament_deadlock_assignment_runs.id", ondelete="CASCADE"
        ),
        index=True,
    )
    team_id: Mapped[str] = mapped_column(String(20))
    team_name: Mapped[str] = mapped_column(String(120))
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    release_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("platform.users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(120), index=True)
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PreprodTestRun(TimestampMixin, Base):
    __tablename__ = "preprod_test_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    marker: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    origin: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_users: Mapped[int] = mapped_column(Integer, default=0)
    created_users: Mapped[int] = mapped_column(Integer, default=0)
    tournaments_created: Mapped[int] = mapped_column(Integer, default=0)
    active_participants: Mapped[int] = mapped_column(Integer, default=0)
    teams_count: Mapped[int] = mapped_column(Integer, default=0)
    matches_count: Mapped[int] = mapped_column(Integer, default=0)
    report_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    cleanup_state: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
