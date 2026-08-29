"""Increase ready-vote counter shards for concurrent vote bursts.

Revision ID: 20260829_0044
Revises: 20260824_0043
Create Date: 2026-08-29
"""

from __future__ import annotations

from alembic import op


revision = "20260829_0044"
down_revision = "20260824_0043"
branch_labels = None
depends_on = None

TABLE_NAME = "platform.tournament_deadlock_ready_vote_count_shards"
VOTE_TABLE_NAME = "platform.tournament_deadlock_ready_votes"
TRIGGER_FUNCTION = "sync_tournament_deadlock_ready_vote_count_shards"
SHARD_CONSTRAINT = "ck_tournament_deadlock_ready_vote_count_shards_shard_in_range"


def _rebuild_counter_rows(shard_count: int) -> None:
    op.execute(f"DELETE FROM {TABLE_NAME}")
    op.execute(
        f"""
        INSERT INTO {TABLE_NAME}
            (round_id, choice, shard, vote_count)
        SELECT
            round_id,
            choice,
            mod(abs(hashtext(user_id)::bigint), {shard_count})::integer AS shard,
            count(*)::integer AS vote_count
        FROM {VOTE_TABLE_NAME}
        GROUP BY
            round_id,
            choice,
            mod(abs(hashtext(user_id)::bigint), {shard_count})::integer
        """
    )


def _replace_counter_trigger_function(shard_count: int) -> None:
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
                new_shard := mod(abs(hashtext(NEW.user_id)::bigint), {shard_count})::integer;
                INSERT INTO {TABLE_NAME}
                    (round_id, choice, shard, vote_count)
                VALUES
                    (NEW.round_id, NEW.choice, new_shard, 1)
                ON CONFLICT (round_id, choice, shard)
                DO UPDATE SET
                    vote_count = {TABLE_NAME}.vote_count + 1,
                    updated_at = now();
                RETURN NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                IF OLD.round_id IS DISTINCT FROM NEW.round_id
                    OR OLD.user_id IS DISTINCT FROM NEW.user_id
                    OR OLD.choice IS DISTINCT FROM NEW.choice THEN
                    old_shard := mod(abs(hashtext(OLD.user_id)::bigint), {shard_count})::integer;
                    UPDATE {TABLE_NAME}
                    SET
                        vote_count = GREATEST(0, vote_count - 1),
                        updated_at = now()
                    WHERE round_id = OLD.round_id
                        AND choice = OLD.choice
                        AND shard = old_shard;

                    new_shard := mod(abs(hashtext(NEW.user_id)::bigint), {shard_count})::integer;
                    INSERT INTO {TABLE_NAME}
                        (round_id, choice, shard, vote_count)
                    VALUES
                        (NEW.round_id, NEW.choice, new_shard, 1)
                    ON CONFLICT (round_id, choice, shard)
                    DO UPDATE SET
                        vote_count = {TABLE_NAME}.vote_count + 1,
                        updated_at = now();
                END IF;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                old_shard := mod(abs(hashtext(OLD.user_id)::bigint), {shard_count})::integer;
                UPDATE {TABLE_NAME}
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


def _set_shard_count(shard_count: int) -> None:
    upper_bound = shard_count - 1
    op.execute(f"LOCK TABLE {TABLE_NAME} IN ACCESS EXCLUSIVE MODE")
    op.drop_constraint(
        SHARD_CONSTRAINT,
        "tournament_deadlock_ready_vote_count_shards",
        type_="check",
        schema="platform",
    )
    _rebuild_counter_rows(shard_count)
    op.create_check_constraint(
        SHARD_CONSTRAINT,
        "tournament_deadlock_ready_vote_count_shards",
        f"shard BETWEEN 0 AND {upper_bound}",
        schema="platform",
    )
    _replace_counter_trigger_function(shard_count)


def upgrade() -> None:
    # Existing rows were hashed into 32 buckets. Rebuilding while the counter
    # table is locked is required so a later choice change decrements the same
    # bucket that the original insert incremented.
    _set_shard_count(128)


def downgrade() -> None:
    _set_shard_count(32)
