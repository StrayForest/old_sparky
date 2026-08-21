"""Add production authentication lifecycle records.

Revision ID: 20260801_0037
Revises: 20260801_0036
Create Date: 2026-08-01 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260801_0037"
down_revision = "20260801_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.execute(
        "UPDATE platform.users "
        "SET email_verified_at = COALESCE(updated_at, created_at, now()) "
        "WHERE email_verified_at IS NULL"
    )

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(token_digest) = 64", name="token_digest_length"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["platform.users.id"],
            name="fk_password_reset_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_password_reset_tokens"),
        sa.UniqueConstraint("token_digest", name="uq_password_reset_tokens_token_digest"),
        schema="platform",
    )
    op.create_index(
        "ix_password_reset_tokens_user_id",
        "password_reset_tokens",
        ["user_id"],
        schema="platform",
    )
    op.create_index(
        "ix_password_reset_tokens_cleanup",
        "password_reset_tokens",
        ["expires_at", "consumed_at"],
        schema="platform",
    )

    op.create_table(
        "email_verification_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(token_digest) = 64", name="token_digest_length"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["platform.users.id"],
            name="fk_email_verification_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_verification_tokens"),
        sa.UniqueConstraint(
            "token_digest",
            name="uq_email_verification_tokens_token_digest",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_email_verification_tokens_user_id",
        "email_verification_tokens",
        ["user_id"],
        schema="platform",
    )
    op.create_index(
        "ix_email_verification_tokens_cleanup",
        "email_verification_tokens",
        ["expires_at", "consumed_at"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_verification_tokens_cleanup",
        table_name="email_verification_tokens",
        schema="platform",
    )
    op.drop_index(
        "ix_email_verification_tokens_user_id",
        table_name="email_verification_tokens",
        schema="platform",
    )
    op.drop_table("email_verification_tokens", schema="platform")

    op.drop_index(
        "ix_password_reset_tokens_cleanup",
        table_name="password_reset_tokens",
        schema="platform",
    )
    op.drop_index(
        "ix_password_reset_tokens_user_id",
        table_name="password_reset_tokens",
        schema="platform",
    )
    op.drop_table("password_reset_tokens", schema="platform")
    op.drop_column("users", "email_verified_at", schema="platform")
