"""Add durable sanitized media assets and immutable variants.

Revision ID: 20260801_0036
Revises: 20260731_0035
Create Date: 2026-08-01 10:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260801_0036"
down_revision = "20260731_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=True),
        sa.Column("tournament_id", sa.String(length=36), nullable=True),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("source_mime", sa.String(length=32), nullable=False),
        sa.Column("source_bytes", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_after", sa.DateTime(timezone=True), nullable=True),
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
            "purpose IN ('profile_avatar', 'profile_banner', 'tournament_banner')",
            name="purpose_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed', 'replaced', 'deleted')",
            name="status_allowed",
        ),
        sa.CheckConstraint(
            "((purpose IN ('profile_avatar', 'profile_banner') "
            "AND owner_user_id IS NOT NULL AND tournament_id IS NULL) OR "
            "(purpose = 'tournament_banner' "
            "AND owner_user_id IS NULL AND tournament_id IS NOT NULL))",
            name="ownership_matches_purpose",
        ),
        sa.CheckConstraint(
            "source_bytes > 0",
            name="source_bytes_positive",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="source_sha256_length",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="attempt_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["platform.users.id"],
            name="fk_media_assets_owner_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["platform.tournaments.id"],
            name="fk_media_assets_tournament_id_tournaments",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_media_assets"),
        sa.UniqueConstraint("version_id", name="uq_media_assets_version_id"),
        schema="platform",
    )
    op.create_index(
        "ix_media_assets_owner_user_id",
        "media_assets",
        ["owner_user_id"],
        schema="platform",
    )
    op.create_index(
        "ix_media_assets_tournament_id",
        "media_assets",
        ["tournament_id"],
        schema="platform",
    )
    op.create_index(
        "ix_media_assets_reconciliation",
        "media_assets",
        ["status", "next_retry_at", "updated_at"],
        schema="platform",
    )

    op.create_table(
        "media_variants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("variant_name", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=32), nullable=False, server_default="image/webp"),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "mime_type = 'image/webp'",
            name="mime_type_webp",
        ),
        sa.CheckConstraint(
            "width > 0 AND height > 0",
            name="dimensions_positive",
        ),
        sa.CheckConstraint(
            "byte_size > 0",
            name="byte_size_positive",
        ),
        sa.CheckConstraint(
            "length(sha256) = 64",
            name="sha256_length",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["platform.media_assets.id"],
            name="fk_media_variants_asset_id_media_assets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_media_variants"),
        sa.UniqueConstraint(
            "asset_id",
            "variant_name",
            name="uq_media_variants_asset_variant",
        ),
        sa.UniqueConstraint("object_key", name="uq_media_variants_object_key"),
        schema="platform",
    )
    op.add_column(
        "player_profiles",
        sa.Column("avatar_asset_id", sa.String(length=36), nullable=True),
        schema="platform",
    )
    op.add_column(
        "player_profiles",
        sa.Column("banner_asset_id", sa.String(length=36), nullable=True),
        schema="platform",
    )
    op.create_foreign_key(
        "fk_player_profiles_avatar_asset_id_media_assets",
        "player_profiles",
        "media_assets",
        ["avatar_asset_id"],
        ["id"],
        source_schema="platform",
        referent_schema="platform",
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_player_profiles_banner_asset_id_media_assets",
        "player_profiles",
        "media_assets",
        ["banner_asset_id"],
        ["id"],
        source_schema="platform",
        referent_schema="platform",
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_player_profiles_avatar_asset_id",
        "player_profiles",
        ["avatar_asset_id"],
        schema="platform",
    )
    op.create_index(
        "ix_player_profiles_banner_asset_id",
        "player_profiles",
        ["banner_asset_id"],
        schema="platform",
    )

    op.add_column(
        "tournaments",
        sa.Column("banner_asset_id", sa.String(length=36), nullable=True),
        schema="platform",
    )
    op.create_foreign_key(
        "fk_tournaments_banner_asset_id_media_assets",
        "tournaments",
        "media_assets",
        ["banner_asset_id"],
        ["id"],
        source_schema="platform",
        referent_schema="platform",
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_tournaments_banner_asset_id",
        "tournaments",
        ["banner_asset_id"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tournaments_banner_asset_id",
        table_name="tournaments",
        schema="platform",
    )
    op.drop_constraint(
        "fk_tournaments_banner_asset_id_media_assets",
        "tournaments",
        schema="platform",
        type_="foreignkey",
    )
    op.drop_column("tournaments", "banner_asset_id", schema="platform")

    op.drop_index(
        "ix_player_profiles_banner_asset_id",
        table_name="player_profiles",
        schema="platform",
    )
    op.drop_index(
        "ix_player_profiles_avatar_asset_id",
        table_name="player_profiles",
        schema="platform",
    )
    op.drop_constraint(
        "fk_player_profiles_banner_asset_id_media_assets",
        "player_profiles",
        schema="platform",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_player_profiles_avatar_asset_id_media_assets",
        "player_profiles",
        schema="platform",
        type_="foreignkey",
    )
    op.drop_column("player_profiles", "banner_asset_id", schema="platform")
    op.drop_column("player_profiles", "avatar_asset_id", schema="platform")

    op.drop_table("media_variants", schema="platform")

    op.drop_index(
        "ix_media_assets_reconciliation",
        table_name="media_assets",
        schema="platform",
    )
    op.drop_index(
        "ix_media_assets_tournament_id",
        table_name="media_assets",
        schema="platform",
    )
    op.drop_index(
        "ix_media_assets_owner_user_id",
        table_name="media_assets",
        schema="platform",
    )
    op.drop_table("media_assets", schema="platform")
