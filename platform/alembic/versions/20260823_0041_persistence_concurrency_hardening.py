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
           OR (status = 'completed' AND NOT (
                (winner_side = 'home' AND home_score IS NOT NULL AND away_score IS NOT NULL
                 AND home_score > away_score)
                OR
                (winner_side = 'away' AND home_score IS NOT NULL AND away_score IS NOT NULL
                 AND away_score > home_score)
           ))
        """,
    ),
    (
        "invalid ready-vote count shard",
        """
        SELECT count(*) FROM platform.tournament_deadlock_ready_vote_count_shards
        WHERE choice NOT IN ('yes', 'no') OR shard NOT BETWEEN 0 AND 31 OR vote_count < 0
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
    (
        "captain round references a ready round from another tournament",
        """
        SELECT count(*)
        FROM platform.tournament_deadlock_captain_rounds captain_round
        JOIN platform.tournament_deadlock_ready_rounds ready_round
          ON ready_round.id = captain_round.source_ready_round_id
        WHERE captain_round.tournament_id <> ready_round.tournament_id
        """,
    ),
    (
        "assignment run references inconsistent workflow parents",
        """
        SELECT count(*)
        FROM platform.tournament_deadlock_assignment_runs assignment_run
        JOIN platform.tournament_deadlock_captain_rounds captain_round
          ON captain_round.id = assignment_run.source_captain_round_id
        JOIN platform.tournament_deadlock_ready_rounds ready_round
          ON ready_round.id = assignment_run.source_ready_round_id
        WHERE assignment_run.tournament_id <> captain_round.tournament_id
           OR assignment_run.tournament_id <> ready_round.tournament_id
           OR captain_round.source_ready_round_id <> assignment_run.source_ready_round_id
        """,
    ),
    (
        "player commitment references an assignment from another tournament",
        """
        SELECT count(*)
        FROM platform.player_tournament_commitments commitment
        JOIN platform.tournament_deadlock_assignment_runs assignment_run
          ON assignment_run.id = commitment.assignment_run_id
        WHERE commitment.tournament_id <> assignment_run.tournament_id
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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_api_mutation_idempotency_keys_request_fingerprint_length",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["platform.users.id"],
            name="fk_api_mutation_idempotency_keys_actor_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_mutation_idempotency_keys"),
        sa.UniqueConstraint(
            "actor_user_id", "scope", "key",
            name="uq_api_mutation_idempotency_keys_actor_scope_key",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_api_mutation_idempotency_keys_actor_user_id",
        "api_mutation_idempotency_keys", ["actor_user_id"],
        unique=False, schema="platform",
    )

    for table_name, constraint_name, condition in (
        ("tournament_participants", "entry_type_solo", "entry_type = 'solo'"),
        (
            "tournament_participants", "status_allowed",
            "status IN ('registered', 'confirmed', 'checked_in', 'withdrawn', 'disqualified')",
        ),
        ("tournament_matches", "round_number_positive", "round_number > 0"),
        ("tournament_matches", "sequence_number_positive", "sequence_number > 0"),
        (
            "tournament_matches", "status_allowed",
            "status IN ('scheduled', 'live', 'completed', 'cancelled')",
        ),
        (
            "tournament_matches", "winner_side_allowed",
            "winner_side IS NULL OR winner_side IN ('home', 'away')",
        ),
        (
            "tournament_matches", "home_score_nonnegative",
            "home_score IS NULL OR home_score >= 0",
        ),
        (
            "tournament_matches", "away_score_nonnegative",
            "away_score IS NULL OR away_score >= 0",
        ),
        (
            "tournament_matches", "completed_result_consistent",
            "status <> 'completed' OR ("
            "(winner_side = 'home' AND home_score IS NOT NULL AND away_score IS NOT NULL "
            "AND home_score > away_score) OR "
            "(winner_side = 'away' AND home_score IS NOT NULL AND away_score IS NOT NULL "
            "AND away_score > home_score))",
        ),
        (
            "tournament_deadlock_ready_vote_count_shards", "choice_allowed",
            "choice IN ('yes', 'no')",
        ),
        (
            "tournament_deadlock_ready_vote_count_shards", "shard_in_range",
            "shard BETWEEN 0 AND 31",
        ),
        (
            "tournament_deadlock_ready_vote_count_shards", "vote_count_nonnegative",
            "vote_count >= 0",
        ),
    ):
        op.create_check_constraint(
            constraint_name, table_name, condition, schema="platform"
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform.enforce_invite_access_tournament()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.invite_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM platform.tournament_invites invite
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
        FOR EACH ROW EXECUTE FUNCTION platform.enforce_invite_access_tournament()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform.enforce_captain_round_tournament()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM platform.tournament_deadlock_ready_rounds ready_round
                WHERE ready_round.id = NEW.source_ready_round_id
                  AND ready_round.tournament_id = NEW.tournament_id
            ) THEN
                RAISE EXCEPTION 'captain round tournament does not match source ready round'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_tournament_deadlock_captain_rounds_source_tournament';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tournament_deadlock_captain_rounds_source_tournament
        BEFORE INSERT OR UPDATE OF tournament_id, source_ready_round_id
        ON platform.tournament_deadlock_captain_rounds
        FOR EACH ROW EXECUTE FUNCTION platform.enforce_captain_round_tournament()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform.enforce_assignment_run_workflow_parents()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM platform.tournament_deadlock_captain_rounds captain_round
                JOIN platform.tournament_deadlock_ready_rounds ready_round
                  ON ready_round.id = captain_round.source_ready_round_id
                WHERE captain_round.id = NEW.source_captain_round_id
                  AND captain_round.tournament_id = NEW.tournament_id
                  AND ready_round.id = NEW.source_ready_round_id
                  AND ready_round.tournament_id = NEW.tournament_id
            ) THEN
                RAISE EXCEPTION 'assignment run workflow parents are inconsistent'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_tournament_deadlock_assignment_runs_workflow_parents';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tournament_deadlock_assignment_runs_workflow_parents
        BEFORE INSERT OR UPDATE OF tournament_id, source_captain_round_id, source_ready_round_id
        ON platform.tournament_deadlock_assignment_runs
        FOR EACH ROW EXECUTE FUNCTION platform.enforce_assignment_run_workflow_parents()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform.enforce_commitment_assignment_tournament()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM platform.tournament_deadlock_assignment_runs assignment_run
                WHERE assignment_run.id = NEW.assignment_run_id
                  AND assignment_run.tournament_id = NEW.tournament_id
            ) THEN
                RAISE EXCEPTION 'commitment tournament does not match assignment run'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_player_tournament_commitments_assignment_tournament';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_player_tournament_commitments_assignment_tournament
        BEFORE INSERT OR UPDATE OF tournament_id, assignment_run_id
        ON platform.player_tournament_commitments
        FOR EACH ROW EXECUTE FUNCTION platform.enforce_commitment_assignment_tournament()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform.enforce_ready_vote_active_participant()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            vote_tournament_id varchar(36);
            vote_round_status varchar(20);
        BEGIN
            SELECT ready_round.tournament_id, ready_round.status
            INTO vote_tournament_id, vote_round_status
            FROM platform.tournament_deadlock_ready_rounds ready_round
            WHERE ready_round.id = NEW.round_id;

            IF vote_tournament_id IS NULL THEN
                RETURN NEW;
            END IF;
            IF vote_round_status = 'active' AND NOT EXISTS (
                SELECT 1 FROM platform.tournament_participants participant
                WHERE participant.tournament_id = vote_tournament_id
                  AND participant.user_id = NEW.user_id
                  AND participant.status NOT IN ('withdrawn', 'disqualified')
            ) THEN
                RAISE EXCEPTION 'active ready vote requires active tournament participation'
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
        CREATE CONSTRAINT TRIGGER trg_tournament_deadlock_ready_votes_active_participant
        AFTER INSERT OR UPDATE
        ON platform.tournament_deadlock_ready_votes
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION platform.enforce_ready_vote_active_participant()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform.enforce_participant_active_ready_votes()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            participant_tournament_id varchar(36);
            participant_user_id varchar(36);
            participant_status varchar(20);
        BEGIN
            IF TG_OP = 'DELETE' THEN
                participant_tournament_id := OLD.tournament_id;
                participant_user_id := OLD.user_id;
                participant_status := 'withdrawn';
            ELSE
                participant_tournament_id := NEW.tournament_id;
                participant_user_id := NEW.user_id;
                participant_status := NEW.status;
            END IF;

            IF participant_status IN ('withdrawn', 'disqualified') AND EXISTS (
                SELECT 1
                FROM platform.tournament_deadlock_ready_votes vote
                JOIN platform.tournament_deadlock_ready_rounds ready_round
                  ON ready_round.id = vote.round_id
                WHERE ready_round.tournament_id = participant_tournament_id
                  AND ready_round.status = 'active'
                  AND vote.user_id = participant_user_id
            ) THEN
                RAISE EXCEPTION 'inactive participant cannot retain a vote in an active ready round'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_tournament_participants_active_ready_vote';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_tournament_participants_active_ready_vote
        AFTER UPDATE OR DELETE
        ON platform.tournament_participants
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION platform.enforce_participant_active_ready_votes()
        """
    )


def downgrade() -> None:
    for trigger_name, table_name in (
        ("trg_tournament_participants_active_ready_vote", "tournament_participants"),
        ("trg_tournament_deadlock_ready_votes_active_participant", "tournament_deadlock_ready_votes"),
        ("trg_player_tournament_commitments_assignment_tournament", "player_tournament_commitments"),
        ("trg_tournament_deadlock_assignment_runs_workflow_parents", "tournament_deadlock_assignment_runs"),
        ("trg_tournament_deadlock_captain_rounds_source_tournament", "tournament_deadlock_captain_rounds"),
        ("trg_tournament_invite_accesses_invite_tournament", "tournament_invite_accesses"),
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS {trigger_name} ON platform.{table_name}"
        )

    for function_name in (
        "enforce_participant_active_ready_votes",
        "enforce_ready_vote_active_participant",
        "enforce_commitment_assignment_tournament",
        "enforce_assignment_run_workflow_parents",
        "enforce_captain_round_tournament",
        "enforce_invite_access_tournament",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS platform.{function_name}()")

    for table_name, constraint_name in (
        ("tournament_deadlock_ready_vote_count_shards", "vote_count_nonnegative"),
        ("tournament_deadlock_ready_vote_count_shards", "shard_in_range"),
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
            table_name, type_="check", schema="platform",
        )

    op.drop_index(
        "ix_api_mutation_idempotency_keys_actor_user_id",
        table_name="api_mutation_idempotency_keys", schema="platform",
    )
    op.drop_table("api_mutation_idempotency_keys", schema="platform")
