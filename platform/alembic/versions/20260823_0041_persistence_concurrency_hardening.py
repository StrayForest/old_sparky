"""Harden persistence domains, relations and mutation idempotency.

Revision ID: 20260823_0041
Revises: 20260822_0040
Create Date: 2026-08-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260823_0041"
down_revision = "20260822_0040"
branch_labels = None
depends_on = None


_DATA_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "invalid tournament participant state",
        """
        SELECT count(*) FROM platform.tournament_participants
        WHERE entry_type <> 'solo'
           OR status NOT IN ('registered', 'confirmed', 'checked_in', 'withdrawn', 'disqualified')
        """,
    ),
    (
        "invalid tournament match state",
        """
        SELECT count(*) FROM platform.tournament_matches
        WHERE round_number <= 0
           OR sequence_number <= 0
           OR status NOT IN ('scheduled', 'live', 'completed', 'cancelled')
           OR (winner_side IS NOT NULL AND winner_side NOT IN ('home', 'away'))
           OR (home_score IS NOT NULL AND home_score < 0)
           OR (away_score IS NOT NULL AND away_score < 0)
           OR (status = 'completed' AND (
                home_score IS NULL OR away_score IS NULL OR home_score = away_score OR winner_side IS NULL
           ))
        """,
    ),
    (
        "invalid ready-vote count shard",
        """
        SELECT count(*) FROM platform.tournament_deadlock_ready_vote_count_shards
        WHERE choice NOT IN ('yes', 'no') OR shard < 0 OR vote_count < 0
        """,
    ),
    (
        "invite access references an invite from another tournament",
        """
        SELECT count(*)
        FROM platform.tournament_invite_accesses access
        JOIN platform.tournament_invites invite ON invite.id = access.invite_id
        WHERE access.invite_id IS NOT NULL
          AND invite.tournament_id <> access.tournament_id
        """,
    ),
)


def _assert_data_invariants() -> None:
    bind = op.get_bind()
    for label, statement in _DATA_CHECKS:
        invalid_count = int(bind.scalar(sa.text(statement)) or 0)
        if invalid_count:
            raise RuntimeError(
                "Cannot apply 20260823_0041: found "
                f"{invalid_count} row(s) with {label}. Repair the data before retrying."
            )


def upgrade() -> None:
    _assert_data_invariants()

    op.create_table(
        "api_mutation_idempotency_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=200), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_api_mutation_idempotency_keys_request_fingerprint_length",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["platform.users.id"],
            name="fk_api_mutation_idempotency_keys_actor_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id", name="pk_api_mutation_idempotency_keys"
        ),
        sa.UniqueConstraint(
            "actor_user_id",
            "scope",
            "key",
            name="uq_api_mutation_idempotency_keys_actor_scope_key",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_api_mutation_idempotency_keys_actor_user_id",
        "api_mutation_idempotency_keys",
        ["actor_user_id"],
        unique=False,
        schema="platform",
    )

    for table_name, constraint_name, condition in (
        (
            "tournament_participants",
            "entry_type_solo",
            "entry_type = 'solo'",
        ),
        (
            "tournament_participants",
            "status_allowed",
            "status IN ('registered', 'confirmed', 'checked_in', 'withdrawn', 'disqualified')",
        ),
        ("tournament_matches", "round_number_positive", "round_number > 0"),
        ("tournament_matches", "sequence_number_positive", "sequence_number > 0"),
        (
            "tournament_matches",
            "status_allowed",
            "status IN ('scheduled', 'live', 'completed', 'cancelled')",
        ),
        (
            "tournament_matches",
            "winner_side_allowed",
            "winner_side IS NULL OR winner_side IN ('home', 'away')",
        ),
        (
            "tournament_matches",
            "home_score_nonnegative",
            "home_score IS NULL OR home_score >= 0",
        ),
        (
            "tournament_matches",
            "away_score_nonnegative",
            "away_score IS NULL OR away_score >= 0",
        ),
        (
            "tournament_matches",
            "completed_result_consistent",
            "status <> 'completed' OR (home_score IS NOT NULL AND away_score IS NOT NULL "
            "AND home_score <> away_score AND winner_side IS NOT NULL)",
        ),
        (
            "tournament_deadlock_ready_vote_count_shards",
            "choice_allowed",
            "choice IN ('yes', 'no')",
        ),
        (
            "tournament_deadlock_ready_vote_count_shards",
            "shard_nonnegative",
            "shard >= 0",
        ),
        (
            "tournament_deadlock_ready_vote_count_shards",
            "vote_count_nonnegative",
            "vote_count >= 0",
        ),
    ):
        op.create_check_constraint(
            constraint_name, table_name, condition, schema="platform"
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform.enforce_invite_access_tournament()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.invite_id IS NOT NULL AND NOT EXISTS (
                SELECT 1
                FROM platform.tournament_invites invite
                WHERE invite.id = NEW.invite_id
                  AND invite.tournament_id = NEW.tournament_id
            ) THEN
                RAISE EXCEPTION 'invite access tournament does not match invite tournament'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_tournament_invite_accesses_invite_tournament';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tournament_invite_accesses_invite_tournament
        BEFORE INSERT OR UPDATE OF tournament_id, invite_id
        ON platform.tournament_invite_accesses
        FOR EACH ROW
        EXECUTE FUNCTION platform.enforce_invite_access_tournament()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform.enforce_ready_vote_active_participant()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            vote_tournament_id varchar(36);
        BEGIN
            SELECT ready_round.tournament_id
            INTO vote_tournament_id
            FROM platform.tournament_deadlock_ready_rounds ready_round
            WHERE ready_round.id = NEW.round_id;

            IF vote_tournament_id IS NULL OR NOT EXISTS (
                SELECT 1
                FROM platform.tournament_participants participant
                WHERE participant.tournament_id = vote_tournament_id
                  AND participant.user_id = NEW.user_id
                  AND participant.status NOT IN ('withdrawn', 'disqualified')
            ) THEN
                RAISE EXCEPTION 'ready vote requires active tournament participation'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_tournament_deadlock_ready_votes_active_participant';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tournament_deadlock_ready_votes_active_participant
        BEFORE INSERT OR UPDATE OF round_id, user_id
        ON platform.tournament_deadlock_ready_votes
        FOR EACH ROW
        EXECUTE FUNCTION platform.enforce_ready_vote_active_participant()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_tournament_deadlock_ready_votes_active_participant "
        "ON platform.tournament_deadlock_ready_votes"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS platform.enforce_ready_vote_active_participant()"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_tournament_invite_accesses_invite_tournament "
        "ON platform.tournament_invite_accesses"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS platform.enforce_invite_access_tournament()"
    )

    for table_name, constraint_name in (
        ("tournament_deadlock_ready_vote_count_shards", "vote_count_nonnegative"),
        ("tournament_deadlock_ready_vote_count_shards", "shard_nonnegative"),
        ("tournament_deadlock_ready_vote_count_shards", "choice_allowed"),
        ("tournament_matches", "completed_result_consistent"),
        ("tournament_matches", "away_score_nonnegative"),
        ("tournament_matches", "home_score_nonnegative"),
        ("tournament_matches", "winner_side_allowed"),
        ("tournament_matches", "status_allowed"),
        ("tournament_matches", "sequence_number_positive"),
        ("tournament_matches", "round_number_positive"),
        ("tournament_participants", "status_allowed"),
        ("tournament_participants", "entry_type_solo"),
    ):
        op.drop_constraint(
            f"ck_{table_name}_{constraint_name}",
            table_name,
            type_="check",
            schema="platform",
        )

    op.drop_index(
        "ix_api_mutation_idempotency_keys_actor_user_id",
        table_name="api_mutation_idempotency_keys",
        schema="platform",
    )
    op.drop_table("api_mutation_idempotency_keys", schema="platform")
