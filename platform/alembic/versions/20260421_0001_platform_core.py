"""Initial platform schema and auth core tables."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260421_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS platform")

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("email", name="uq_users_email"),
        schema="platform",
    )
    op.create_index("ix_users_email", "users", ["email"], unique=False, schema="platform")

    op.create_table(
        "password_credentials",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("password_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name="pk_password_credentials"),
        schema="platform",
    )

    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.UniqueConstraint("slug", name="uq_roles_slug"),
        schema="platform",
    )

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["platform.roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role_id", name="pk_user_roles"),
        schema="platform",
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_digest", name="uq_sessions_token_digest"),
        schema="platform",
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False, schema="platform")
    op.create_index("ix_sessions_token_digest", "sessions", ["token_digest"], unique=False, schema="platform")
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False, schema="platform")

    op.create_table(
        "player_profiles",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("display_name", sa.String(length=40), nullable=False),
        sa.Column("handle", sa.String(length=40), nullable=True),
        sa.Column("avatar_url", sa.String(length=512), nullable=True),
        sa.Column("banner_url", sa.String(length=512), nullable=True),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("region", sa.String(length=40), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name="pk_player_profiles"),
        sa.UniqueConstraint("handle", name="uq_player_profiles_handle"),
        schema="platform",
    )

    op.create_table(
        "tournaments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("slug", sa.String(length=140), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("visibility", sa.String(length=20), nullable=False, server_default="public"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("format_slug", sa.String(length=64), nullable=False),
        sa.Column("organizer_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organizer_user_id"], ["platform.users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("slug", name="uq_tournaments_slug"),
        schema="platform",
    )
    op.create_index(
        "ix_tournaments_organizer_user_id",
        "tournaments",
        ["organizer_user_id"],
        unique=False,
        schema="platform",
    )
    op.create_index("ix_tournaments_slug", "tournaments", ["slug"], unique=False, schema="platform")

    op.create_table(
        "tournament_participants",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tournament_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("entry_type", sa.String(length=32), nullable=False, server_default="solo"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="registered"),
        sa.Column("team_name", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tournament_id"], ["platform.tournaments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tournament_id", "user_id", name="uq_tournament_participants_unique_member"),
        schema="platform",
    )
    op.create_index(
        "ix_tournament_participants_tournament_id",
        "tournament_participants",
        ["tournament_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_participants_user_id",
        "tournament_participants",
        ["user_id"],
        unique=False,
        schema="platform",
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["actor_user_id"], ["platform.users.id"], ondelete="SET NULL"),
        schema="platform",
    )
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"], unique=False, schema="platform")
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"], unique=False, schema="platform")

    op.bulk_insert(
        sa.table(
            "roles",
            sa.column("slug", sa.String()),
            sa.column("name", sa.String()),
            sa.column("description", sa.String()),
            schema="platform",
        ),
        [
            {"slug": "authenticated_user", "name": "Authenticated User", "description": "Default signed-in account."},
            {"slug": "player", "name": "Player", "description": "Tournament participant profile."},
            {"slug": "organizer", "name": "Organizer", "description": "Tournament organizer role."},
            {"slug": "moderator", "name": "Moderator", "description": "Content and tournament moderation role."},
            {"slug": "editor", "name": "Editor", "description": "News and editorial role."},
            {"slug": "admin", "name": "Admin", "description": "Platform administrator role."},
            {"slug": "superadmin", "name": "Superadmin", "description": "Highest privilege role."},
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_action", table_name="audit_logs", schema="platform")
    op.drop_index("ix_audit_logs_actor_user_id", table_name="audit_logs", schema="platform")
    op.drop_table("audit_logs", schema="platform")
    op.drop_index("ix_tournament_participants_user_id", table_name="tournament_participants", schema="platform")
    op.drop_index("ix_tournament_participants_tournament_id", table_name="tournament_participants", schema="platform")
    op.drop_table("tournament_participants", schema="platform")
    op.drop_index("ix_tournaments_slug", table_name="tournaments", schema="platform")
    op.drop_index("ix_tournaments_organizer_user_id", table_name="tournaments", schema="platform")
    op.drop_table("tournaments", schema="platform")
    op.drop_table("player_profiles", schema="platform")
    op.drop_index("ix_sessions_expires_at", table_name="sessions", schema="platform")
    op.drop_index("ix_sessions_token_digest", table_name="sessions", schema="platform")
    op.drop_index("ix_sessions_user_id", table_name="sessions", schema="platform")
    op.drop_table("sessions", schema="platform")
    op.drop_table("user_roles", schema="platform")
    op.drop_table("roles", schema="platform")
    op.drop_table("password_credentials", schema="platform")
    op.drop_index("ix_users_email", table_name="users", schema="platform")
    op.drop_table("users", schema="platform")
    op.execute("DROP SCHEMA IF EXISTS platform CASCADE")
