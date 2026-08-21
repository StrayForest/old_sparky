"""Add globally exclusive player tournament commitments.

Revision ID: 20260720_0032
Revises: 20260714_0031
Create Date: 2026-07-20 17:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260720_0032"
down_revision = "20260714_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "player_tournament_commitments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("tournament_id", sa.String(length=36), nullable=False),
        sa.Column("assignment_run_id", sa.String(length=36), nullable=False),
        sa.Column("team_id", sa.String(length=20), nullable=False),
        sa.Column("team_name", sa.String(length=120), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "(released_at IS NULL AND release_reason IS NULL) OR "
            "(released_at IS NOT NULL AND release_reason IS NOT NULL)",
            name="ck_player_tournament_commitments_release_state_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["assignment_run_id"],
            ["platform.tournament_deadlock_assignment_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tournament_id"], ["platform.tournaments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_player_tournament_commitments"),
        schema="platform",
    )
    op.create_index(
        "ix_player_tournament_commitments_user_id",
        "player_tournament_commitments",
        ["user_id"],
        schema="platform",
    )
    op.create_index(
        "ix_player_tournament_commitments_tournament_id",
        "player_tournament_commitments",
        ["tournament_id"],
        schema="platform",
    )
    op.create_index(
        "ix_player_tournament_commitments_assignment_run_id",
        "player_tournament_commitments",
        ["assignment_run_id"],
        schema="platform",
    )

    op.execute(
        """
        WITH latest_locked_runs AS (
            SELECT DISTINCT ON (run.tournament_id)
                run.id AS assignment_run_id,
                run.tournament_id,
                run.locked_at,
                run.created_at AS run_created_at,
                run.result_snapshot::jsonb AS result_snapshot,
                tournament.status AS tournament_status
            FROM platform.tournament_deadlock_assignment_runs AS run
            JOIN platform.tournaments AS tournament ON tournament.id = run.tournament_id
            WHERE run.status = 'locked'
            ORDER BY
                run.tournament_id,
                run.locked_at DESC NULLS LAST,
                run.created_at DESC,
                run.id DESC
        ),
        team_rows AS (
            SELECT
                run.*,
                team.value AS team,
                team.value ->> 'team_id' AS team_id,
                COALESCE(NULLIF(team.value ->> 'team_name', ''), 'Team ' || (team.value ->> 'team_id')) AS team_name
            FROM latest_locked_runs AS run
            CROSS JOIN LATERAL jsonb_array_elements(COALESCE(run.result_snapshot -> 'teams', '[]'::jsonb)) AS team(value)
        ),
        roster_rows AS (
            SELECT assignment_run_id, tournament_id, locked_at, run_created_at,
                   tournament_status, team_id, team_name, team -> 'captain' ->> 'user_id' AS user_id
            FROM team_rows
            UNION ALL
            SELECT assignment_run_id, tournament_id, locked_at, run_created_at,
                   tournament_status, team_id, team_name, slot.value -> 'assigned_player' ->> 'user_id' AS user_id
            FROM team_rows
            CROSS JOIN LATERAL jsonb_array_elements(COALESCE(team -> 'starter_slots', '[]'::jsonb)) AS slot(value)
            UNION ALL
            SELECT assignment_run_id, tournament_id, locked_at, run_created_at,
                   tournament_status, team_id, team_name, team -> 'reserve_slot' -> 'assigned_player' ->> 'user_id' AS user_id
            FROM team_rows
            WHERE jsonb_typeof(team -> 'reserve_slot') = 'object'
        ),
        classified AS (
            SELECT DISTINCT
                roster.*,
                CASE
                    WHEN roster.tournament_status = 'completed' THEN 'tournament_completed'
                    WHEN roster.tournament_status = 'cancelled' THEN 'tournament_cancelled'
                    WHEN EXISTS (
                        SELECT 1
                        FROM platform.tournament_matches AS match
                        WHERE match.tournament_id = roster.tournament_id
                          AND match.status = 'completed'
                          AND roster.team_id IN (match.home_team_id, match.away_team_id)
                          AND match.winner_team_id IS NOT NULL
                          AND match.winner_team_id IN (match.home_team_id, match.away_team_id)
                          AND match.winner_team_id IS DISTINCT FROM roster.team_id
                    ) THEN 'team_eliminated'
                    ELSE NULL
                END AS base_release_reason
            FROM roster_rows AS roster
            WHERE NULLIF(roster.user_id, '') IS NOT NULL
              AND NULLIF(roster.team_id, '') IS NOT NULL
        ),
        ranked AS (
            SELECT
                classified.*,
                row_number() OVER (
                    PARTITION BY user_id
                    ORDER BY
                        CASE WHEN base_release_reason IS NULL THEN 0 ELSE 1 END,
                        CASE WHEN tournament_status = 'in_progress' THEN 0 ELSE 1 END,
                        locked_at ASC NULLS LAST,
                        tournament_id,
                        team_id
                ) AS user_commitment_rank
            FROM classified
        )
        INSERT INTO platform.player_tournament_commitments (
            id,
            user_id,
            tournament_id,
            assignment_run_id,
            team_id,
            team_name,
            activated_at,
            released_at,
            release_reason,
            created_at,
            updated_at
        )
        SELECT
            md5(assignment_run_id || ':' || user_id || ':' || team_id),
            user_id,
            tournament_id,
            assignment_run_id,
            team_id,
            team_name,
            COALESCE(locked_at, run_created_at, now()),
            CASE
                WHEN base_release_reason IS NOT NULL OR user_commitment_rank > 1 THEN now()
                ELSE NULL
            END,
            CASE
                WHEN base_release_reason IS NOT NULL THEN base_release_reason
                WHEN user_commitment_rank > 1 THEN 'migration_duplicate_commitment'
                ELSE NULL
            END,
            COALESCE(locked_at, run_created_at, now()),
            now()
        FROM ranked
        """
    )

    op.create_index(
        "uq_player_tournament_commitments_active_user",
        "player_tournament_commitments",
        ["user_id"],
        unique=True,
        schema="platform",
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        "ix_player_tournament_commitments_active_tournament_team",
        "player_tournament_commitments",
        ["tournament_id", "team_id"],
        schema="platform",
        postgresql_where=sa.text("released_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_player_tournament_commitments_active_tournament_team",
        table_name="player_tournament_commitments",
        schema="platform",
    )
    op.drop_index(
        "uq_player_tournament_commitments_active_user",
        table_name="player_tournament_commitments",
        schema="platform",
    )
    op.drop_index(
        "ix_player_tournament_commitments_assignment_run_id",
        table_name="player_tournament_commitments",
        schema="platform",
    )
    op.drop_index(
        "ix_player_tournament_commitments_tournament_id",
        table_name="player_tournament_commitments",
        schema="platform",
    )
    op.drop_index(
        "ix_player_tournament_commitments_user_id",
        table_name="player_tournament_commitments",
        schema="platform",
    )
    op.drop_table("player_tournament_commitments", schema="platform")
