"""Add the singleton site appearance palette.

Revision ID: 20261112_0055
Revises: 20261010_0054
"""

from alembic import op
import sqlalchemy as sa


revision = "20261112_0055"
down_revision = "20261010_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "appearance_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("colors", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("id = 1", name="appearance_settings_singleton"),
        sa.CheckConstraint(
            "revision >= 1", name="appearance_settings_revision_positive"
        ),
        schema="platform",
    )
    op.execute(
        """
        INSERT INTO platform.appearance_settings (id, colors, revision)
        VALUES (
          1,
          '{"background":"#050813","accent":"#8B5CF6","foreground":"#F7F8FC","secondary":"#2DD4BF","warning":"#FB923C","danger":"#FB7185","success":"#4ADE80"}'::jsonb,
          1
        )
        """
    )


def downgrade() -> None:
    op.drop_table("appearance_settings", schema="platform")
