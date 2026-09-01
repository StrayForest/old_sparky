"""Materialize the denormalized tournament catalog card projection."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_0051"
down_revision = "20260901_0050"
branch_labels = None
depends_on = None


INACTIVE_STATUSES = "('withdrawn', 'disqualified')"


def _projection_select() -> str:
    participant_count = (
        "(SELECT count(tp.id) FROM platform.tournament_participants tp "
        "WHERE tp.tournament_id = t.id "
        f"AND tp.status NOT IN {INACTIVE_STATUSES})"
    )
    locked_roster = (
        "EXISTS (SELECT 1 FROM platform.tournament_deadlock_assignment_runs ar "
        "WHERE ar.tournament_id = t.id AND ar.status = 'locked')"
    )
    return f"""
        SELECT
            t.id,
            t.slug,
            t.name,
            t.description,
            t.cover_url,
            t.banner_asset_id,
            t.visibility,
            t.status,
            t.format_slug,
            t.allowed_ranks,
            t.max_participants,
            t.registration_starts_at,
            t.registration_closes_at,
            t.ready_check_starts_at,
            t.ready_check_ends_at,
            t.captain_selection_starts_at,
            t.starts_at,
            t.match_format,
            t.final_format,
            t.captain_response_deadline_minutes,
            t.teams_count,
            t.automation_ready_check_started_at,
            t.automation_ready_check_closed_at,
            t.automation_captain_round_started_at,
            t.automation_captain_round_finalized_at,
            t.automation_assignment_generated_at,
            t.automation_last_error,
            t.automation_failure_count,
            t.automation_retry_after,
            t.organizer_user_id,
            u.display_name,
            pp.avatar_asset_id,
            {participant_count},
            {locked_roster},
            t.bracket_revision,
            t.created_at,
            t.updated_at
        FROM platform.tournaments t
        JOIN platform.users u ON u.id = t.organizer_user_id
        LEFT JOIN platform.player_profiles pp ON pp.user_id = t.organizer_user_id
    """


def _create_indexes() -> None:
    index_statements = (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_tournament_list_public_created_at_id "
        "ON platform.tournament_list_read_models (created_at DESC, id DESC) "
        "WHERE visibility = 'public' AND format_slug = 'solo'",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_tournament_list_public_status_created_at_id "
        "ON platform.tournament_list_read_models "
        "(status, created_at DESC, id DESC) "
        "WHERE visibility = 'public' AND format_slug = 'solo'",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_tournament_list_public_starts_nearest "
        "ON platform.tournament_list_read_models "
        "(starts_at ASC NULLS LAST, created_at DESC, id DESC) "
        "WHERE visibility = 'public' AND format_slug = 'solo'",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_tournament_list_public_starts_farthest "
        "ON platform.tournament_list_read_models "
        "(starts_at DESC NULLS LAST, created_at DESC, id DESC) "
        "WHERE visibility = 'public' AND format_slug = 'solo'",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_tournament_list_public_participants_asc "
        "ON platform.tournament_list_read_models "
        "(participant_count ASC, created_at DESC, id DESC) "
        "WHERE visibility = 'public' AND format_slug = 'solo'",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_tournament_list_public_participants_desc "
        "ON platform.tournament_list_read_models "
        "(participant_count DESC, created_at DESC, id DESC) "
        "WHERE visibility = 'public' AND format_slug = 'solo'",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_tournament_list_organizer_created_at_id "
        "ON platform.tournament_list_read_models "
        "(organizer_user_id, created_at DESC, id DESC)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_tournament_list_allowed_ranks_gin "
        "ON platform.tournament_list_read_models USING gin (allowed_ranks)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_tournament_list_name_lower_trgm "
        "ON platform.tournament_list_read_models USING gin (lower(name) gin_trgm_ops)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_tournament_list_organizer_name_lower_trgm "
        "ON platform.tournament_list_read_models "
        "USING gin (lower(organizer_display_name) gin_trgm_ops)",
    )
    with op.get_context().autocommit_block():
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        for statement in index_statements:
            op.execute(statement)


def upgrade() -> None:
    op.create_table(
        "tournament_list_read_models",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=140), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("cover_url", sa.String(length=512), nullable=True),
        sa.Column("banner_asset_id", sa.String(length=36), nullable=True),
        sa.Column("visibility", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("format_slug", sa.String(length=64), nullable=False),
        sa.Column("allowed_ranks", postgresql.JSONB(), nullable=False),
        sa.Column("max_participants", sa.Integer(), nullable=True),
        sa.Column("registration_starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registration_closes_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_check_starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_check_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("captain_selection_starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("match_format", sa.String(length=20), nullable=False),
        sa.Column("final_format", sa.String(length=20), nullable=False),
        sa.Column("captain_response_deadline_minutes", sa.Integer(), nullable=True),
        sa.Column("teams_count", sa.Integer(), nullable=True),
        sa.Column("automation_ready_check_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("automation_ready_check_closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("automation_captain_round_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("automation_captain_round_finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("automation_assignment_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("automation_last_error", sa.Text(), nullable=True),
        sa.Column("automation_failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("automation_retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("organizer_user_id", sa.String(length=36), nullable=False),
        sa.Column("organizer_display_name", sa.String(length=40), nullable=False),
        sa.Column("organizer_avatar_asset_id", sa.String(length=36), nullable=True),
        sa.Column("participant_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("has_locked_deadlock_roster", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("bracket_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["id"], ["platform.tournaments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tournament_list_read_models"),
        sa.UniqueConstraint("slug", name="uq_tournament_list_read_models_slug"),
        schema="platform",
    )

    op.execute(
        "INSERT INTO platform.tournament_list_read_models "
        "(id, slug, name, description, cover_url, banner_asset_id, visibility, status, "
        "format_slug, allowed_ranks, max_participants, registration_starts_at, "
        "registration_closes_at, ready_check_starts_at, ready_check_ends_at, "
        "captain_selection_starts_at, starts_at, match_format, final_format, "
        "captain_response_deadline_minutes, teams_count, "
        "automation_ready_check_started_at, automation_ready_check_closed_at, "
        "automation_captain_round_started_at, automation_captain_round_finalized_at, "
        "automation_assignment_generated_at, automation_last_error, "
        "automation_failure_count, automation_retry_after, organizer_user_id, "
        "organizer_display_name, organizer_avatar_asset_id, participant_count, "
        "has_locked_deadlock_roster, bracket_revision, created_at, updated_at) "
        + _projection_select()
    )
    _create_indexes()


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for index_name in (
            "ix_tournament_list_public_created_at_id",
            "ix_tournament_list_public_status_created_at_id",
            "ix_tournament_list_public_starts_nearest",
            "ix_tournament_list_public_starts_farthest",
            "ix_tournament_list_public_participants_asc",
            "ix_tournament_list_public_participants_desc",
            "ix_tournament_list_organizer_created_at_id",
            "ix_tournament_list_allowed_ranks_gin",
            "ix_tournament_list_name_lower_trgm",
            "ix_tournament_list_organizer_name_lower_trgm",
        ):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS platform.{index_name}")
    op.drop_table("tournament_list_read_models", schema="platform")
