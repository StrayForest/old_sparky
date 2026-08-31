"""Materialize current Deadlock tournament teams outside assignment JSON.

The assignment run remains the immutable solver/audit result. This revision
backfills the one current published-or-locked run for every existing
tournament and makes the normalized tables the live roster source.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260831_0049"
down_revision = "20260830_0048"
branch_labels = None
depends_on = None


def _deterministic_uuid_sql(expression: str) -> str:
    digest = f"md5({expression})"
    return (
        f"substr({digest}, 1, 8) || '-' || substr({digest}, 9, 4) || '-' || "
        f"substr({digest}, 13, 4) || '-' || substr({digest}, 17, 4) || '-' || "
        f"substr({digest}, 21, 12)"
    )


def upgrade() -> None:
    op.create_table(
        "tournament_teams",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tournament_id", sa.String(length=36), nullable=False),
        sa.Column("source_assignment_run_id", sa.String(length=36), nullable=False),
        sa.Column("team_key", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("captain_user_id", sa.String(length=36), nullable=True),
        sa.Column("starter_strength", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "starter_average_strength",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
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
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["platform.tournaments.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_assignment_run_id"],
            ["platform.tournament_deadlock_assignment_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["captain_user_id"],
            ["platform.users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tournament_teams"),
        sa.UniqueConstraint(
            "tournament_id",
            "team_key",
            name="uq_tournament_teams_tournament_team_key",
        ),
        sa.UniqueConstraint(
            "tournament_id",
            "id",
            name="uq_tournament_teams_tournament_id_id",
        ),
        sa.UniqueConstraint(
            "source_assignment_run_id",
            "team_key",
            name="uq_tournament_teams_source_run_team_key",
        ),
        sa.CheckConstraint(
            "length(btrim(team_key)) > 0",
            name="ck_tournament_teams_team_key_nonempty",
        ),
        sa.CheckConstraint(
            "length(btrim(name)) > 0",
            name="ck_tournament_teams_name_nonempty",
        ),
        sa.CheckConstraint(
            "starter_strength >= 0",
            name="ck_tournament_teams_starter_strength_nonnegative",
        ),
        sa.CheckConstraint(
            "starter_average_strength >= 0",
            name="ck_tournament_teams_starter_average_strength_nonnegative",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_tournament_teams_tournament_source_run",
        "tournament_teams",
        ["tournament_id", "source_assignment_run_id"],
        schema="platform",
    )
    op.create_index(
        "ix_tournament_teams_tournament_captain",
        "tournament_teams",
        ["tournament_id", "captain_user_id"],
        schema="platform",
    )

    op.create_table(
        "tournament_team_members",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tournament_id", sa.String(length=36), nullable=False),
        sa.Column("team_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("slot_number", sa.Integer(), nullable=False),
        sa.Column("roster_role", sa.String(length=20), nullable=False),
        sa.Column("assigned_role", sa.String(length=32), nullable=True),
        sa.Column("strength", sa.Float(), nullable=False, server_default="0"),
        sa.Column("rank", sa.String(length=32), nullable=True),
        sa.Column("subrank", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["tournament_id", "team_id"],
            [
                "platform.tournament_teams.tournament_id",
                "platform.tournament_teams.id",
            ],
            ondelete="CASCADE",
            name="fk_tournament_team_members_team_tournament",
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["platform.tournaments.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["platform.users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tournament_team_members"),
        sa.UniqueConstraint(
            "team_id",
            "slot_number",
            name="uq_tournament_team_members_team_slot",
        ),
        sa.UniqueConstraint(
            "tournament_id",
            "user_id",
            name="uq_tournament_team_members_tournament_user",
        ),
        sa.CheckConstraint(
            "roster_role IN ('captain', 'starter', 'substitute')",
            name="ck_tournament_team_members_roster_role_allowed",
        ),
        sa.CheckConstraint(
            "(roster_role = 'captain' AND slot_number = 0) OR "
            "(roster_role = 'starter' AND slot_number BETWEEN 1 AND 5) OR "
            "(roster_role = 'substitute' AND slot_number = 6)",
            name="ck_tournament_team_members_roster_role_slot_consistent",
        ),
        sa.CheckConstraint(
            "strength >= 0",
            name="ck_tournament_team_members_strength_nonnegative",
        ),
        sa.CheckConstraint(
            "subrank IS NULL OR subrank > 0",
            name="ck_tournament_team_members_subrank_positive",
        ),
        schema="platform",
    )
    op.create_index(
        "uq_tournament_team_members_team_captain",
        "tournament_team_members",
        ["team_id"],
        unique=True,
        postgresql_where=sa.text("roster_role = 'captain'"),
        schema="platform",
    )
    op.create_index(
        "ix_tournament_team_members_tournament_team_slot",
        "tournament_team_members",
        ["tournament_id", "team_id", "slot_number"],
        schema="platform",
    )

    team_id_sql = _deterministic_uuid_sql(
        "assignment_run_id || ':' || btrim(team ->> 'team_id')"
    )
    op.execute(
        f"""
        WITH authoritative_runs AS (
            SELECT DISTINCT ON (run.tournament_id)
                run.id AS assignment_run_id,
                run.tournament_id,
                run.result_snapshot::jsonb AS result_snapshot
            FROM platform.tournament_deadlock_assignment_runs AS run
            WHERE run.status IN ('published', 'locked')
            ORDER BY
                run.tournament_id,
                run.locked_at DESC NULLS LAST,
                run.published_at DESC NULLS LAST,
                run.created_at DESC,
                run.id DESC
        ),
        raw_teams AS (
            SELECT
                run.assignment_run_id,
                run.tournament_id,
                team.value AS team
            FROM authoritative_runs AS run
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(run.result_snapshot -> 'teams') = 'array'
                    THEN run.result_snapshot -> 'teams'
                    ELSE '[]'::jsonb
                END
            ) AS team(value)
        ),
        team_rows AS (
            SELECT
                {team_id_sql} AS id,
                assignment_run_id,
                tournament_id,
                btrim(team ->> 'team_id') AS team_key,
                COALESCE(
                    NULLIF(btrim(team ->> 'team_name'), ''),
                    'Team ' || btrim(team ->> 'team_id')
                ) AS name,
                NULLIF(btrim(team -> 'captain' ->> 'user_id'), '') AS captain_user_id,
                COALESCE(NULLIF(team ->> 'starter_strength', '')::double precision, 0.0)
                    AS starter_strength,
                COALESCE(
                    NULLIF(team ->> 'starter_average_strength', '')::double precision,
                    0.0
                ) AS starter_average_strength
            FROM raw_teams
            WHERE jsonb_typeof(team) = 'object'
              AND NULLIF(btrim(team ->> 'team_id'), '') IS NOT NULL
        )
        INSERT INTO platform.tournament_teams (
            id,
            tournament_id,
            source_assignment_run_id,
            team_key,
            name,
            captain_user_id,
            starter_strength,
            starter_average_strength
        )
        SELECT
            team_rows.id,
            team_rows.tournament_id,
            team_rows.assignment_run_id,
            team_rows.team_key,
            team_rows.name,
            captain.id,
            team_rows.starter_strength,
            team_rows.starter_average_strength
        FROM team_rows
        LEFT JOIN platform.users AS captain
            ON captain.id = team_rows.captain_user_id
        """
    )

    member_id_sql = _deterministic_uuid_sql(
        "assignment_run_id || ':' || team_key || ':' || roster_role || ':' || slot_number::text"
    )
    op.execute(
        f"""
        WITH authoritative_runs AS (
            SELECT DISTINCT ON (run.tournament_id)
                run.id AS assignment_run_id,
                run.tournament_id,
                run.result_snapshot::jsonb AS result_snapshot
            FROM platform.tournament_deadlock_assignment_runs AS run
            WHERE run.status IN ('published', 'locked')
            ORDER BY
                run.tournament_id,
                run.locked_at DESC NULLS LAST,
                run.published_at DESC NULLS LAST,
                run.created_at DESC,
                run.id DESC
        ),
        raw_teams AS (
            SELECT
                run.assignment_run_id,
                run.tournament_id,
                team.value AS team
            FROM authoritative_runs AS run
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(run.result_snapshot -> 'teams') = 'array'
                    THEN run.result_snapshot -> 'teams'
                    ELSE '[]'::jsonb
                END
            ) AS team(value)
        ),
        team_rows AS (
            SELECT
                assignment_run_id,
                tournament_id,
                btrim(team ->> 'team_id') AS team_key,
                team
            FROM raw_teams
            WHERE jsonb_typeof(team) = 'object'
              AND NULLIF(btrim(team ->> 'team_id'), '') IS NOT NULL
        ),
        roster_rows (
            assignment_run_id,
            tournament_id,
            team_key,
            user_id,
            slot_number,
            roster_role,
            assigned_role,
            strength,
            rank,
            subrank
        ) AS (
            SELECT
                assignment_run_id,
                tournament_id,
                team_key,
                NULLIF(btrim(team -> 'captain' ->> 'user_id'), ''),
                0,
                'captain',
                NULLIF(btrim(team -> 'captain' ->> 'assigned_role'), ''),
                COALESCE(NULLIF(team -> 'captain' ->> 'strength', '')::double precision, 0.0),
                NULLIF(btrim(team -> 'captain' ->> 'rank'), ''),
                NULLIF(team -> 'captain' ->> 'subrank', '')::integer
            FROM team_rows
            WHERE NULLIF(btrim(team -> 'captain' ->> 'user_id'), '') IS NOT NULL
            UNION ALL
            SELECT
                rows.assignment_run_id,
                rows.tournament_id,
                rows.team_key,
                NULLIF(btrim(slot.value -> 'assigned_player' ->> 'user_id'), ''),
                COALESCE(NULLIF(slot.value ->> 'slot_number', '')::integer, slot.position::integer),
                'starter',
                NULLIF(btrim(slot.value ->> 'assigned_role'), ''),
                COALESCE(NULLIF(slot.value -> 'assigned_player' ->> 'strength', '')::double precision, 0.0),
                NULLIF(btrim(slot.value -> 'assigned_player' ->> 'rank'), ''),
                NULLIF(slot.value -> 'assigned_player' ->> 'subrank', '')::integer
            FROM team_rows AS rows
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(rows.team -> 'starter_slots') = 'array'
                    THEN rows.team -> 'starter_slots'
                    ELSE '[]'::jsonb
                END
            ) WITH ORDINALITY AS slot(value, position)
            WHERE NULLIF(btrim(slot.value -> 'assigned_player' ->> 'user_id'), '') IS NOT NULL
            UNION ALL
            SELECT
                assignment_run_id,
                tournament_id,
                team_key,
                NULLIF(btrim(team -> 'reserve_slot' -> 'assigned_player' ->> 'user_id'), ''),
                6,
                'substitute',
                NULLIF(btrim(team -> 'reserve_slot' ->> 'assigned_role'), ''),
                COALESCE(NULLIF(team -> 'reserve_slot' -> 'assigned_player' ->> 'strength', '')::double precision, 0.0),
                NULLIF(btrim(team -> 'reserve_slot' -> 'assigned_player' ->> 'rank'), ''),
                NULLIF(team -> 'reserve_slot' -> 'assigned_player' ->> 'subrank', '')::integer
            FROM team_rows
            WHERE jsonb_typeof(team -> 'reserve_slot') = 'object'
              AND NULLIF(btrim(team -> 'reserve_slot' -> 'assigned_player' ->> 'user_id'), '') IS NOT NULL
        )
        INSERT INTO platform.tournament_team_members (
            id,
            tournament_id,
            team_id,
            user_id,
            slot_number,
            roster_role,
            assigned_role,
            strength,
            rank,
            subrank
        )
        SELECT
            {_deterministic_uuid_sql("assignment_run_id || ':' || team_key || ':' || roster_role || ':' || slot_number::text")},
            tournament_id,
            {_deterministic_uuid_sql("assignment_run_id || ':' || team_key")},
            user_id,
            slot_number,
            roster_role,
            assigned_role,
            strength,
            rank,
            subrank
        FROM roster_rows
        WHERE user_id IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM platform.users AS roster_user
              WHERE roster_user.id = roster_rows.user_id
          )
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tournament_team_members_tournament_team_slot",
        table_name="tournament_team_members",
        schema="platform",
    )
    op.drop_index(
        "uq_tournament_team_members_team_captain",
        table_name="tournament_team_members",
        schema="platform",
    )
    op.drop_table("tournament_team_members", schema="platform")
    op.drop_index(
        "ix_tournament_teams_tournament_captain",
        table_name="tournament_teams",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_teams_tournament_source_run",
        table_name="tournament_teams",
        schema="platform",
    )
    op.drop_table("tournament_teams", schema="platform")
