"""Add durable tournament participant capacity slots.

Revision ID: 20260824_0042
Revises: 20260823_0041
Create Date: 2026-08-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260824_0042"
down_revision = "20260823_0041"
branch_labels = None
depends_on = None

# A tournament can legally advertise a very large capacity, but capacity
# tokens must not be materialized one row at a time for that upper bound. The
# service allocates sparse slots above this inventory window on demand.
SLOT_MATERIALIZATION_LIMIT = 1024


def upgrade() -> None:
    bind = op.get_bind()
    invalid_capacity = int(
        bind.scalar(
            sa.text(
                """
                SELECT count(*)
                FROM (
                    SELECT participant.tournament_id
                    FROM platform.tournament_participants participant
                    JOIN platform.tournaments tournament
                      ON tournament.id = participant.tournament_id
                    WHERE participant.status NOT IN ('withdrawn', 'disqualified')
                      AND tournament.max_participants IS NOT NULL
                    GROUP BY participant.tournament_id, tournament.max_participants
                    HAVING count(*) > tournament.max_participants
                ) over_capacity
                """
            )
        )
        or 0
    )
    if invalid_capacity:
        raise RuntimeError(
            "Cannot apply 20260824_0042: active participants exceed tournament capacity."
        )

    op.create_table(
        "tournament_participant_slots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tournament_id", sa.String(length=36), nullable=False),
        sa.Column("slot_number", sa.Integer(), nullable=False),
        sa.Column("participant_id", sa.String(length=36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["platform.tournaments.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["platform.tournament_participants.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tournament_id",
            "slot_number",
            name="uq_tournament_participant_slots_tournament_slot",
        ),
        sa.UniqueConstraint(
            "tournament_id",
            "participant_id",
            name="uq_tournament_participant_slots_tournament_participant",
        ),
        sa.CheckConstraint("slot_number > 0", name="slot_number_positive"),
        schema="platform",
    )
    op.create_index(
        "ix_tournament_participant_slots_tournament_id",
        "tournament_participant_slots",
        ["tournament_id"],
        schema="platform",
    )
    op.create_index(
        "ix_tournament_participant_slots_participant_id",
        "tournament_participant_slots",
        ["participant_id"],
        schema="platform",
    )
    op.create_index(
        "ix_tournament_participant_slots_free",
        "tournament_participant_slots",
        ["tournament_id"],
        unique=False,
        postgresql_where=sa.text("participant_id IS NULL"),
        schema="platform",
    )

    op.execute(
        f"""
        WITH ranked AS (
            SELECT
                participant.id AS participant_id,
                participant.tournament_id,
                participant.created_at AS claimed_at,
                row_number() OVER (
                    PARTITION BY participant.tournament_id
                    ORDER BY participant.created_at, participant.id
                ) AS slot_number
            FROM platform.tournament_participants participant
            WHERE participant.status NOT IN ('withdrawn', 'disqualified')
        ), slot_rows AS (
            SELECT
                ranked.tournament_id,
                ranked.slot_number::integer AS slot_number,
                ranked.participant_id,
                ranked.claimed_at
            FROM ranked
            JOIN platform.tournaments tournament
              ON tournament.id = ranked.tournament_id
             AND tournament.max_participants IS NOT NULL
            UNION ALL
            SELECT
                tournament.id,
                slots.slot_number,
                NULL,
                NULL
            FROM platform.tournaments tournament
            CROSS JOIN LATERAL generate_series(
                1,
                LEAST(tournament.max_participants, {SLOT_MATERIALIZATION_LIMIT})
            ) AS slots(slot_number)
            WHERE tournament.max_participants IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM ranked
                  WHERE ranked.tournament_id = tournament.id
                    AND ranked.slot_number = slots.slot_number
              )
        )
        INSERT INTO platform.tournament_participant_slots
            (id, tournament_id, slot_number, participant_id, claimed_at)
        SELECT
            md5(slot_rows.tournament_id || ':' || slot_rows.slot_number::text),
            slot_rows.tournament_id,
            slot_rows.slot_number,
            slot_rows.participant_id,
            slot_rows.claimed_at
        FROM slot_rows
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform.initialize_tournament_participant_slots()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.max_participants IS NULL THEN
                DELETE FROM platform.tournament_participant_slots
                WHERE tournament_id = NEW.id
                  AND participant_id IS NULL;
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE'
               AND OLD.max_participants IS NOT NULL
               AND NEW.max_participants < OLD.max_participants THEN
                IF EXISTS (
                    SELECT 1
                    FROM platform.tournament_participant_slots
                    WHERE tournament_id = NEW.id
                      AND slot_number > NEW.max_participants
                      AND participant_id IS NOT NULL
                ) THEN
                    RAISE EXCEPTION 'Cannot reduce tournament capacity below an active participant slot';
                END IF;
                DELETE FROM platform.tournament_participant_slots
                WHERE tournament_id = NEW.id
                  AND slot_number > NEW.max_participants
                  AND participant_id IS NULL;
            END IF;
            INSERT INTO platform.tournament_participant_slots
                (id, tournament_id, slot_number)
            SELECT
                md5(NEW.id || ':' || slots.slot_number::text),
                NEW.id,
                slots.slot_number
            FROM generate_series(
                1,
                LEAST(NEW.max_participants, {SLOT_MATERIALIZATION_LIMIT})
            ) AS slots(slot_number)
            ON CONFLICT (tournament_id, slot_number) DO NOTHING;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_initialize_tournament_participant_slots
        AFTER INSERT OR UPDATE OF max_participants ON platform.tournaments
        FOR EACH ROW
        EXECUTE FUNCTION platform.initialize_tournament_participant_slots()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform.release_inactive_tournament_participant_slot()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status IN ('withdrawn', 'disqualified')
               AND OLD.status NOT IN ('withdrawn', 'disqualified') THEN
                UPDATE platform.tournament_participant_slots
                SET participant_id = NULL,
                    claimed_at = NULL,
                    updated_at = now()
                WHERE participant_id = NEW.id;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_release_inactive_tournament_participant_slot
        AFTER UPDATE OF status ON platform.tournament_participants
        FOR EACH ROW
        EXECUTE FUNCTION platform.release_inactive_tournament_participant_slot()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_release_inactive_tournament_participant_slot "
        "ON platform.tournament_participants"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_initialize_tournament_participant_slots "
        "ON platform.tournaments"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS platform.initialize_tournament_participant_slots()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS platform.release_inactive_tournament_participant_slot()"
    )
    op.drop_index(
        "ix_tournament_participant_slots_free",
        table_name="tournament_participant_slots",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_participant_slots_participant_id",
        table_name="tournament_participant_slots",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_participant_slots_tournament_id",
        table_name="tournament_participant_slots",
        schema="platform",
    )
    op.drop_table("tournament_participant_slots", schema="platform")
