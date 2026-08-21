"""Drop ready-vote counter trigger.

Revision ID: 20260704_0029
Revises: 20260704_0028
Create Date: 2026-07-04 19:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260704_0029"
down_revision = "20260704_0028"
branch_labels = None
depends_on = None


TRIGGER_FUNCTION = "sync_tournament_deadlock_ready_vote_counts"
TRIGGER_NAME = "trg_tournament_deadlock_ready_vote_counts"


def upgrade() -> None:
    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {TRIGGER_NAME}
        ON platform.tournament_deadlock_ready_votes
        """
    )
    op.execute(f"DROP FUNCTION IF EXISTS platform.{TRIGGER_FUNCTION}()")
    op.drop_column(
        "tournament_deadlock_ready_rounds",
        "declined_count",
        schema="platform",
    )
    op.drop_column(
        "tournament_deadlock_ready_rounds",
        "ready_count",
        schema="platform",
    )


def downgrade() -> None:
    op.add_column(
        "tournament_deadlock_ready_rounds",
        sa.Column("ready_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        schema="platform",
    )
    op.add_column(
        "tournament_deadlock_ready_rounds",
        sa.Column("declined_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        schema="platform",
    )
    op.execute(
        """
        UPDATE platform.tournament_deadlock_ready_rounds AS rounds
        SET
            ready_count = COALESCE(votes.ready_count, 0),
            declined_count = COALESCE(votes.declined_count, 0)
        FROM (
            SELECT
                round_id,
                COUNT(*) FILTER (WHERE choice = 'yes')::integer AS ready_count,
                COUNT(*) FILTER (WHERE choice = 'no')::integer AS declined_count
            FROM platform.tournament_deadlock_ready_votes
            GROUP BY round_id
        ) AS votes
        WHERE rounds.id = votes.round_id
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION platform.{TRIGGER_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                UPDATE platform.tournament_deadlock_ready_rounds
                SET
                    ready_count = ready_count + CASE WHEN NEW.choice = 'yes' THEN 1 ELSE 0 END,
                    declined_count = declined_count + CASE WHEN NEW.choice = 'no' THEN 1 ELSE 0 END,
                    updated_at = now()
                WHERE id = NEW.round_id;
                RETURN NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                IF OLD.choice IS DISTINCT FROM NEW.choice THEN
                    UPDATE platform.tournament_deadlock_ready_rounds
                    SET
                        ready_count = GREATEST(
                            0,
                            ready_count
                            - CASE WHEN OLD.choice = 'yes' THEN 1 ELSE 0 END
                            + CASE WHEN NEW.choice = 'yes' THEN 1 ELSE 0 END
                        ),
                        declined_count = GREATEST(
                            0,
                            declined_count
                            - CASE WHEN OLD.choice = 'no' THEN 1 ELSE 0 END
                            + CASE WHEN NEW.choice = 'no' THEN 1 ELSE 0 END
                        ),
                        updated_at = now()
                    WHERE id = NEW.round_id;
                END IF;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                UPDATE platform.tournament_deadlock_ready_rounds
                SET
                    ready_count = GREATEST(
                        0,
                        ready_count - CASE WHEN OLD.choice = 'yes' THEN 1 ELSE 0 END
                    ),
                    declined_count = GREATEST(
                        0,
                        declined_count - CASE WHEN OLD.choice = 'no' THEN 1 ELSE 0 END
                    ),
                    updated_at = now()
                WHERE id = OLD.round_id;
                RETURN OLD;
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {TRIGGER_NAME}
        AFTER INSERT OR UPDATE OF choice OR DELETE
        ON platform.tournament_deadlock_ready_votes
        FOR EACH ROW
        EXECUTE FUNCTION platform.{TRIGGER_FUNCTION}()
        """
    )
