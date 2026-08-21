"""Add Steam OpenID identity and one-time auth-flow records.

Revision ID: 20260813_0038
Revises: 20260801_0037
Create Date: 2026-08-13 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260813_0038"
down_revision = "20260801_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "email",
        existing_type=sa.String(length=320),
        nullable=True,
        schema="platform",
    )

    op.create_table(
        "external_identities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "provider", sa.String(length=16), nullable=False, server_default="steam"
        ),
        sa.Column("subject", sa.String(length=20), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_authenticated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("provider = 'steam'", name="provider_steam"),
        sa.CheckConstraint("subject ~ '^[0-9]{17}$'", name="subject_steam_id64"),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_external_identities"),
        sa.UniqueConstraint(
            "provider", "subject", name="uq_external_identities_provider_subject"
        ),
        sa.UniqueConstraint(
            "user_id", "provider", name="uq_external_identities_user_provider"
        ),
        schema="platform",
    )
    op.create_index(
        "ix_external_identities_user_id",
        "external_identities",
        ["user_id"],
        schema="platform",
    )

    op.create_table(
        "steam_auth_flows",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("state_digest", sa.String(length=64), nullable=False),
        sa.Column("browser_grant_digest", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("return_path", sa.String(length=2048), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("purpose IN ('login', 'link')", name="purpose_allowed"),
        sa.CheckConstraint(
            "((purpose = 'login' AND user_id IS NULL AND session_id IS NULL) OR "
            "(purpose = 'link' AND user_id IS NOT NULL AND session_id IS NOT NULL))",
            name="purpose_owner_matches",
        ),
        sa.CheckConstraint("length(state_digest) = 64", name="state_digest_length"),
        sa.CheckConstraint(
            "length(browser_grant_digest) = 64", name="browser_grant_digest_length"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["platform.sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_steam_auth_flows"),
        sa.UniqueConstraint("state_digest", name="uq_steam_auth_flows_state_digest"),
        schema="platform",
    )
    op.create_index(
        "ix_steam_auth_flows_cleanup",
        "steam_auth_flows",
        ["expires_at", "consumed_at"],
        schema="platform",
    )
    op.create_index(
        "ix_steam_auth_flows_user_id",
        "steam_auth_flows",
        ["user_id"],
        schema="platform",
    )
    op.create_index(
        "ix_steam_auth_flows_session_id",
        "steam_auth_flows",
        ["session_id"],
        schema="platform",
    )

    op.create_table(
        "steam_email_link_intents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_email", sa.String(length=320), nullable=False),
        sa.Column("code_digest", sa.String(length=64), nullable=False),
        sa.Column("browser_grant_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(code_digest) = 64", name="code_digest_length"),
        sa.CheckConstraint(
            "length(browser_grant_digest) = 64", name="browser_grant_digest_length"
        ),
        sa.CheckConstraint(
            "candidate_email = lower(candidate_email)",
            name="candidate_email_normalized",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_steam_email_link_intents"),
        schema="platform",
    )
    op.create_index(
        "ix_steam_email_link_intents_cleanup",
        "steam_email_link_intents",
        ["expires_at", "consumed_at"],
        schema="platform",
    )
    op.create_index(
        "ix_steam_email_link_intents_user_id",
        "steam_email_link_intents",
        ["user_id"],
        schema="platform",
    )


def downgrade() -> None:
    # Steam-created users have no email; refusing to erase their accounts makes
    # this rollback safe and leaves the schema untouched until remediated.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM platform.users WHERE email IS NULL) THEN "
        "RAISE EXCEPTION 'Cannot downgrade Steam auth while users without email exist'; "
        "END IF; END $$"
    )
    op.drop_index(
        "ix_steam_email_link_intents_user_id",
        table_name="steam_email_link_intents",
        schema="platform",
    )
    op.drop_index(
        "ix_steam_email_link_intents_cleanup",
        table_name="steam_email_link_intents",
        schema="platform",
    )
    op.drop_table("steam_email_link_intents", schema="platform")
    op.drop_index(
        "ix_steam_auth_flows_session_id",
        table_name="steam_auth_flows",
        schema="platform",
    )
    op.drop_index(
        "ix_steam_auth_flows_user_id", table_name="steam_auth_flows", schema="platform"
    )
    op.drop_index(
        "ix_steam_auth_flows_cleanup", table_name="steam_auth_flows", schema="platform"
    )
    op.drop_table("steam_auth_flows", schema="platform")
    op.drop_index(
        "ix_external_identities_user_id",
        table_name="external_identities",
        schema="platform",
    )
    op.drop_table("external_identities", schema="platform")
    op.alter_column(
        "users",
        "email",
        existing_type=sa.String(length=320),
        nullable=False,
        schema="platform",
    )
