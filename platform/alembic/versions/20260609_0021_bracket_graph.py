"""Add stable bracket graph identifiers and optimistic revisioning."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260609_0021"
down_revision = "20260608_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tournaments",
        sa.Column(
            "bracket_revision",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        schema="platform",
    )
    for column_name in ("home_team_id", "away_team_id", "winner_team_id"):
        op.add_column(
            "tournament_matches",
            sa.Column(column_name, sa.String(length=20), nullable=True),
            schema="platform",
        )
    for column_name in ("home_source_match_id", "away_source_match_id"):
        op.add_column(
            "tournament_matches",
            sa.Column(column_name, sa.String(length=36), nullable=True),
            schema="platform",
        )
        op.create_foreign_key(
            f"fk_tournament_matches_{column_name}",
            "tournament_matches",
            "tournament_matches",
            [column_name],
            ["id"],
            source_schema="platform",
            referent_schema="platform",
            ondelete="SET NULL",
        )
        op.create_index(
            f"ix_platform_tournament_matches_{column_name}",
            "tournament_matches",
            [column_name],
            schema="platform",
        )

    op.execute(
        """
        UPDATE platform.tournament_matches
        SET home_team_id = nullif(trim(substring(home_label FROM 6)), '')
        WHERE home_label LIKE 'Team %'
        """
    )
    op.execute(
        """
        UPDATE platform.tournament_matches
        SET away_team_id = nullif(trim(substring(away_label FROM 6)), '')
        WHERE away_label LIKE 'Team %'
        """
    )
    op.execute(
        """
        UPDATE platform.tournament_matches
        SET winner_team_id = CASE winner_side
            WHEN 'home' THEN home_team_id
            WHEN 'away' THEN away_team_id
            ELSE NULL
        END
        """
    )


def downgrade() -> None:
    for column_name in ("away_source_match_id", "home_source_match_id"):
        op.drop_index(
            f"ix_platform_tournament_matches_{column_name}",
            table_name="tournament_matches",
            schema="platform",
        )
        op.drop_constraint(
            f"fk_tournament_matches_{column_name}",
            "tournament_matches",
            schema="platform",
            type_="foreignkey",
        )
        op.drop_column("tournament_matches", column_name, schema="platform")
    for column_name in ("winner_team_id", "away_team_id", "home_team_id"):
        op.drop_column("tournament_matches", column_name, schema="platform")
    op.drop_column("tournaments", "bracket_revision", schema="platform")
