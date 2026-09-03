"""Add Google OAuth identities and browser-bound authorization-code state.

Revision ID: 20260903_0052
Revises: 20260901_0051
Create Date: 2026-09-03 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260903_0052"
down_revision = "20260901_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "provider_steam",
        "external_identities",
        schema="platform",
        type_="check",
    )
    op.drop_constraint(
        "subject_steam_id64",
        "external_identities",
        schema="platform",
        type_="check",
    )
    op.alter_column(
        "external_identities",
        "subject",
        existing_type=sa.String(length=20),
        type_=sa.String(length=255),
        existing_nullable=False,
        schema="platform",
    )
    op.create_check_constraint(
        "provider_allowed",
        "external_identities",
        "provider IN ('steam', 'google')",
        schema="platform",
    )
    op.create_check_constraint(
        "subject_length",
        "external_identities",
        "length(subject) BETWEEN 1 AND 255",
        schema="platform",
    )
    op.create_check_constraint(
        "subject_steam_id64",
        "external_identities",
        "provider <> 'steam' OR subject ~ '^[0-9]{17}$'",
        schema="platform",
    )

    op.create_table(
        "google_auth_flows",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("state_digest", sa.String(length=64), nullable=False),
        sa.Column("browser_grant_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "purpose", sa.String(length=16), nullable=False, server_default="login"
        ),
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
        sa.CheckConstraint("purpose = 'login'", name="purpose_login_only"),
        sa.CheckConstraint(
            "user_id IS NULL AND session_id IS NULL", name="purpose_owner_empty"
        ),
        sa.CheckConstraint("length(state_digest) = 64", name="state_digest_length"),
        sa.CheckConstraint(
            "length(browser_grant_digest) = 64", name="browser_grant_digest_length"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["platform.sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_google_auth_flows"),
        sa.UniqueConstraint("state_digest", name="uq_google_auth_flows_state_digest"),
        schema="platform",
    )
    op.create_index(
        "ix_google_auth_flows_cleanup",
        "google_auth_flows",
        ["expires_at", "consumed_at"],
        schema="platform",
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM platform.external_identities WHERE provider = 'google') THEN "
        "RAISE EXCEPTION 'Cannot downgrade Google auth while Google identities exist'; "
        "END IF; END $$"
    )
    op.drop_index(
        "ix_google_auth_flows_cleanup",
        table_name="google_auth_flows",
        schema="platform",
    )
    op.drop_table("google_auth_flows", schema="platform")
    op.drop_constraint(
        "subject_steam_id64",
        "external_identities",
        schema="platform",
        type_="check",
    )
    op.drop_constraint(
        "subject_length",
        "external_identities",
        schema="platform",
        type_="check",
    )
    op.drop_constraint(
        "provider_allowed",
        "external_identities",
        schema="platform",
        type_="check",
    )
    op.alter_column(
        "external_identities",
        "subject",
        existing_type=sa.String(length=255),
        type_=sa.String(length=20),
        existing_nullable=False,
        schema="platform",
    )
    op.create_check_constraint(
        "provider_steam",
        "external_identities",
        "provider = 'steam'",
        schema="platform",
    )
    op.create_check_constraint(
        "subject_steam_id64",
        "external_identities",
        "subject ~ '^[0-9]{17}$'",
        schema="platform",
    )
