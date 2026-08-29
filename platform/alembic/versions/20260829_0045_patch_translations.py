"""Persist patch translation state outside the Redis cache.

Revision ID: 20260829_0045
Revises: 20260829_0044
Create Date: 2026-08-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260829_0045"
down_revision = "20260829_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "patch_translations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("patch_id", sa.String(length=32), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=False),
        sa.Column("translation_version", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "translated_segments",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "processing_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("translated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'skipped', 'superseded')",
            name="ck_patch_translations_status_allowed",
        ),
        sa.CheckConstraint(
            "length(source_hash) = 64",
            name="ck_patch_translations_source_hash_length",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_patch_translations_attempts_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_patch_translations"),
        sa.UniqueConstraint(
            "patch_id",
            "source_hash",
            "locale",
            "translation_version",
            "model",
            name="uq_patch_translations_identity",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_patch_translations_patch_id_status",
        "patch_translations",
        ["patch_id", "status"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_patch_translations_status",
        "patch_translations",
        ["status"],
        unique=False,
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_patch_translations_status",
        table_name="patch_translations",
        schema="platform",
    )
    op.drop_index(
        "ix_patch_translations_patch_id_status",
        table_name="patch_translations",
        schema="platform",
    )
    op.drop_table("patch_translations", schema="platform")
