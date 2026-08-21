"""Refresh Deadlock rank names without changing rank strength.

Revision ID: 20260731_0035
Revises: 20260729_0034
Create Date: 2026-07-31 05:30:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260731_0035"
down_revision = "20260729_0034"
branch_labels = None
depends_on = None


UPGRADE_CASE = """
CASE rank_name
    WHEN 'Alchemist' THEN 'Acolyte'
    WHEN 'Arcanist' THEN 'Sentinel'
    WHEN 'Ritualist' THEN 'Mystic'
    WHEN 'Emissary' THEN 'Ritualist'
    WHEN 'Archon' THEN 'Emissary'
    ELSE rank_name
END
"""

DOWNGRADE_CASE = """
CASE rank_name
    WHEN 'Acolyte' THEN 'Alchemist'
    WHEN 'Sentinel' THEN 'Arcanist'
    WHEN 'Mystic' THEN 'Ritualist'
    WHEN 'Ritualist' THEN 'Emissary'
    WHEN 'Emissary' THEN 'Archon'
    ELSE rank_name
END
"""


def _migrate_profile_ranks(case_expression: str, source_names: tuple[str, ...]) -> None:
    quoted_names = ", ".join(f"'{name}'" for name in source_names)
    op.execute(
        f"""
        UPDATE platform.deadlock_profiles
        SET rank = {case_expression.replace('rank_name', 'rank')}
        WHERE rank IN ({quoted_names})
        """
    )


def _migrate_tournament_ranks(case_expression: str, source_names: tuple[str, ...]) -> None:
    quoted_names = ", ".join(f"'{name}'" for name in source_names)
    op.execute(
        f"""
        UPDATE platform.tournaments AS tournament
        SET allowed_ranks = (
            SELECT COALESCE(
                json_agg({case_expression} ORDER BY rank_position),
                '[]'::json
            )
            FROM json_array_elements_text(tournament.allowed_ranks)
                WITH ORDINALITY AS ranks(rank_name, rank_position)
        )
        WHERE EXISTS (
            SELECT 1
            FROM json_array_elements_text(tournament.allowed_ranks) AS ranks(rank_name)
            WHERE rank_name IN ({quoted_names})
        )
        """
    )


def upgrade() -> None:
    source_names = ("Alchemist", "Arcanist", "Ritualist", "Emissary", "Archon")
    _migrate_profile_ranks(UPGRADE_CASE, source_names)
    _migrate_tournament_ranks(UPGRADE_CASE, source_names)


def downgrade() -> None:
    source_names = ("Acolyte", "Sentinel", "Mystic", "Ritualist", "Emissary")
    _migrate_profile_ranks(DOWNGRADE_CASE, source_names)
    _migrate_tournament_ranks(DOWNGRADE_CASE, source_names)
