from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

from python_packages.platform_infra.models import (
    DeadlockDreamSlot,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentInvite,
    TournamentParticipantSlot,
)


def _check_constraints(table) -> dict[str, str]:
    return {
        str(constraint.name): str(constraint.sqltext)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }


class DeadlockPersistenceModelTests(unittest.TestCase):
    def test_workflow_state_and_value_constraints_are_declared(self) -> None:
        checks_by_table = {
            DeadlockDreamSlot.__table__.name: _check_constraints(DeadlockDreamSlot.__table__),
            Tournament.__table__.name: _check_constraints(Tournament.__table__),
            TournamentInvite.__table__.name: _check_constraints(TournamentInvite.__table__),
            TournamentDeadlockReadyRound.__table__.name: _check_constraints(
                TournamentDeadlockReadyRound.__table__
            ),
            TournamentDeadlockCaptainRound.__table__.name: _check_constraints(
                TournamentDeadlockCaptainRound.__table__
            ),
            TournamentDeadlockAssignmentRun.__table__.name: _check_constraints(
                TournamentDeadlockAssignmentRun.__table__
            ),
        }

        self.assertIn(
            "ck_deadlock_dream_slots_slot_number_in_range",
            checks_by_table["deadlock_dream_slots"],
        )
        self.assertIn("ck_tournaments_status_allowed", checks_by_table["tournaments"])
        self.assertIn("ck_tournament_invites_use_count_within_limit", checks_by_table["tournament_invites"])
        self.assertIn(
            "ck_tournament_deadlock_ready_rounds_status_allowed",
            checks_by_table["tournament_deadlock_ready_rounds"],
        )
        self.assertIn(
            "ck_tournament_deadlock_captain_rounds_status_allowed",
            checks_by_table["tournament_deadlock_captain_rounds"],
        )
        self.assertIn(
            "ck_tournament_deadlock_assignment_runs_status_allowed",
            checks_by_table["tournament_deadlock_assignment_runs"],
        )

    def test_workflow_singletons_have_database_final_guards(self) -> None:
        ready_indexes = {index.name for index in TournamentDeadlockReadyRound.__table__.indexes}
        captain_indexes = {index.name for index in TournamentDeadlockCaptainRound.__table__.indexes}
        assignment_indexes = {index.name for index in TournamentDeadlockAssignmentRun.__table__.indexes}
        captain_unique_columns = {
            tuple(column.name for column in constraint.columns)
            for constraint in TournamentDeadlockCaptainRound.__table__.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }

        self.assertIn("uq_tournament_deadlock_ready_rounds_active_tournament", ready_indexes)
        self.assertIn("uq_tournament_deadlock_captain_rounds_active_tournament", captain_indexes)
        self.assertIn(
            "uq_tournament_deadlock_assignment_runs_current_tournament",
            assignment_indexes,
        )
        self.assertIn(("source_ready_round_id",), captain_unique_columns)

    def test_migration_has_preflight_and_invalid_concurrent_index_recovery(self) -> None:
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "20260822_0040_deadlock_workflow_integrity.py"
        )
        spec = importlib.util.spec_from_file_location("deadlock_integrity_migration_0040", migration_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertEqual(module.revision, "20260822_0040")
        self.assertEqual(module.down_revision, "20260821_0039")
        self.assertTrue(module._DATA_INVARIANT_CHECKS)
        migration_source = migration_path.read_text(encoding="utf-8")
        self.assertIn("pg_index.indisvalid", migration_source)
        self.assertIn("SET visibility = 'invite_only'", migration_source)

        public_name_migration = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "20260714_0031_public_tournament_name_uniqueness.py"
        )
        self.assertIn(
            "_assert_no_normalized_public_name_duplicates",
            public_name_migration.read_text(encoding="utf-8"),
        )
        self.assertIn("pg_index.indisvalid", public_name_migration.read_text(encoding="utf-8"))

    def test_participant_capacity_slots_are_bounded_and_released_by_status_trigger(self) -> None:
        constraints = {
            str(constraint.name)
            for constraint in TournamentParticipantSlot.__table__.constraints
        }
        self.assertIn(
            "uq_tournament_participant_slots_tournament_slot",
            constraints,
        )
        self.assertIn(
            "uq_tournament_participant_slots_tournament_participant",
            constraints,
        )
        free_index = next(
            index
            for index in TournamentParticipantSlot.__table__.indexes
            if index.name == "ix_tournament_participant_slots_free"
        )
        self.assertEqual(str(free_index.dialect_options["postgresql"]["where"]), "participant_id IS NULL")

        migration_root = Path(__file__).resolve().parents[1] / "alembic" / "versions"
        capacity_migration = (migration_root / "20260824_0042_participant_capacity_slots.py").read_text(
            encoding="utf-8"
        )
        ready_vote_migration = (migration_root / "20260824_0043_ready_vote_open_round_guard.py").read_text(
            encoding="utf-8"
        )
        capacity_service = (
            Path(__file__).resolve().parents[1]
            / "apps"
            / "platform_api"
            / "app"
            / "services"
            / "tournament_participant_capacity.py"
        ).read_text(encoding="utf-8")
        self.assertIn("skip_locked=True", capacity_service)
        self.assertIn("trg_release_inactive_tournament_participant_slot", capacity_migration)
        self.assertIn("AFTER INSERT OR UPDATE OF max_participants", capacity_migration)
        self.assertIn("SLOT_MATERIALIZATION_LIMIT = 1024", capacity_migration)
        self.assertIn("LEAST(tournament.max_participants, {SLOT_MATERIALIZATION_LIMIT})", capacity_migration)
        self.assertIn("ON CONFLICT (tournament_id, slot_number) DO NOTHING", capacity_migration)
        self.assertIn("NEW.responded_at > ready_round_closed_at", ready_vote_migration)
        self.assertIn("closed_at", ready_vote_migration)
        self.assertIn("trg_tournament_deadlock_ready_votes_open_round", ready_vote_migration)


if __name__ == "__main__":
    unittest.main()
