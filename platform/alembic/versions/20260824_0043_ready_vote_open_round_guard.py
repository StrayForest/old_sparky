"""Guard ready votes without serializing ordinary votes on tournaments.

Revision ID: 20260824_0043
Revises: 20260824_0042
Create Date: 2026-08-24
"""

from __future__ import annotations

from alembic import op


revision = "20260824_0043"
down_revision = "20260824_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform.enforce_ready_vote_open_round()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            ready_round_status varchar(20);
            ready_round_closed_at timestamptz;
        BEGIN
            SELECT status
                 , closed_at
            INTO ready_round_status
                , ready_round_closed_at
            FROM platform.tournament_deadlock_ready_rounds
            WHERE id = NEW.round_id;

            IF ready_round_status IS NOT NULL
               AND ready_round_status <> 'active'
               AND (
                   ready_round_closed_at IS NULL
                   OR NEW.responded_at > ready_round_closed_at
               ) THEN
                RAISE EXCEPTION 'ready vote requires an active ready round'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_tournament_deadlock_ready_votes_open_round';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_tournament_deadlock_ready_votes_open_round
        AFTER INSERT OR UPDATE
        ON platform.tournament_deadlock_ready_votes
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION platform.enforce_ready_vote_open_round()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_tournament_deadlock_ready_votes_open_round "
        "ON platform.tournament_deadlock_ready_votes"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS platform.enforce_ready_vote_open_round()"
    )
