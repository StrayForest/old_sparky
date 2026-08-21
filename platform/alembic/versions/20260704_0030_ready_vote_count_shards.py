"""Add sharded ready-vote counters.

Revision ID: 20260704_0030
Revises: 20260704_0029
Create Date: 2026-07-04 20:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260704_0030"
down_revision = "20260704_0029"
branch_labels = None
depends_on = None


SHARD_COUNT = 32
TRIGGER_FUNCTION = "sync_tournament_deadlock_ready_vote_count_shards"
TRIGGER_NAME = "trg_tournament_deadlock_ready_vote_count_shards"


def upgrade() -> None:
    op.create_table(
        "tournament_deadlock_ready_vote_count_shards",
        sa.Column("round_id", sa.Integer(), nullable=False),
        sa.Column("choice", sa.String(length=10), nullable=False),
        sa.Column("shard", sa.Integer(), nullable=False),
        sa.Column("vote_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["platform.tournament_deadlock_ready_rounds.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("round_id", "choice", "shard"),
        schema="platform",
    )
    op.execute(
        f"""
        INSERT INTO platform.tournament_deadlock_ready_vote_count_shards
            (round_id, choice, shard, vote_count)
        SELECT
            round_id,
            choice,
            mod(abs(hashtext(user_id)::bigint), {SHARD_COUNT})::integer AS shard,
            count(*)::integer AS vote_count
        FROM platform.tournament_deadlock_ready_votes
        GROUP BY
            round_id,
            choice,
            mod(abs(hashtext(user_id)::bigint), {SHARD_COUNT})::integer
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION platform.{TRIGGER_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            old_shard integer;
            new_shard integer;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                new_shard := mod(abs(hashtext(NEW.user_id)::bigint), {SHARD_COUNT})::integer;
                INSERT INTO platform.tournament_deadlock_ready_vote_count_shards
                    (round_id, choice, shard, vote_count)
                VALUES
                    (NEW.round_id, NEW.choice, new_shard, 1)
                ON CONFLICT (round_id, choice, shard)
                DO UPDATE SET
                    vote_count = platform.tournament_deadlock_ready_vote_count_shards.vote_count + 1,
                    updated_at = now();
                RETURN NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                IF OLD.round_id IS DISTINCT FROM NEW.round_id
                    OR OLD.user_id IS DISTINCT FROM NEW.user_id
                    OR OLD.choice IS DISTINCT FROM NEW.choice THEN
                    old_shard := mod(abs(hashtext(OLD.user_id)::bigint), {SHARD_COUNT})::integer;
                    UPDATE platform.tournament_deadlock_ready_vote_count_shards
                    SET
                        vote_count = GREATEST(0, vote_count - 1),
                        updated_at = now()
                    WHERE round_id = OLD.round_id
                        AND choice = OLD.choice
                        AND shard = old_shard;

                    new_shard := mod(abs(hashtext(NEW.user_id)::bigint), {SHARD_COUNT})::integer;
                    INSERT INTO platform.tournament_deadlock_ready_vote_count_shards
                        (round_id, choice, shard, vote_count)
                    VALUES
                        (NEW.round_id, NEW.choice, new_shard, 1)
                    ON CONFLICT (round_id, choice, shard)
                    DO UPDATE SET
                        vote_count = platform.tournament_deadlock_ready_vote_count_shards.vote_count + 1,
                        updated_at = now();
                END IF;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                old_shard := mod(abs(hashtext(OLD.user_id)::bigint), {SHARD_COUNT})::integer;
                UPDATE platform.tournament_deadlock_ready_vote_count_shards
                SET
                    vote_count = GREATEST(0, vote_count - 1),
                    updated_at = now()
                WHERE round_id = OLD.round_id
                    AND choice = OLD.choice
                    AND shard = old_shard;
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
        AFTER INSERT OR UPDATE OF round_id, user_id, choice OR DELETE
        ON platform.tournament_deadlock_ready_votes
        FOR EACH ROW
        EXECUTE FUNCTION platform.{TRIGGER_FUNCTION}()
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {TRIGGER_NAME}
        ON platform.tournament_deadlock_ready_votes
        """
    )
    op.execute(f"DROP FUNCTION IF EXISTS platform.{TRIGGER_FUNCTION}()")
    op.drop_table(
        "tournament_deadlock_ready_vote_count_shards",
        schema="platform",
    )
