from __future__ import annotations

from contextlib import nullcontext
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

from python_packages.platform_infra.models import (
    PlayerTournamentCommitment,
    Tournament,
    TournamentInvite,
    User,
    UserSession,
)


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20260830_0048_ready_vote_auth_covering_index.py"
)


def _load_ready_vote_migration():
    spec = importlib.util.spec_from_file_location(
        "platform_migration_20260830_0048_for_tests",
        MIGRATION_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load migration from {MIGRATION_PATH}")
    migration = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = migration
    spec.loader.exec_module(migration)
    return migration


READY_VOTE_MIGRATION = _load_ready_vote_migration()


def _catalog_state(
    *,
    is_valid: bool = True,
    is_ready: bool = True,
    indexdef: str | None = None,
    predicate: str | None = None,
    include_columns: tuple[str, ...] | None = None,
):
    migration = READY_VOTE_MIGRATION
    return migration.IndexCatalogState(
        is_valid=is_valid,
        is_ready=is_ready,
        indexdef=(
            migration.EXPECTED_INDEXDEF
            if indexdef is None
            else indexdef
        ),
        predicate=(
            migration.EXPECTED_PREDICATE
            if predicate is None
            else predicate
        ),
        include_columns=(
            migration.EXPECTED_INCLUDE_COLUMNS
            if include_columns is None
            else include_columns
        ),
    )


def _fake_alembic_op():
    fake_op = mock.Mock()
    fake_op.get_context.return_value.autocommit_block.side_effect = (
        lambda: nullcontext()
    )
    return fake_op


class PlatformModelIndexTests(unittest.TestCase):
    def test_ready_vote_auth_index_covers_session_projection_and_is_retryable(self) -> None:
        index = next(
            index
            for index in UserSession.__table__.indexes
            if index.name == "ix_sessions_ready_vote_auth"
        )

        self.assertFalse(index.unique)
        self.assertEqual([column.name for column in index.columns], ["token_digest"])
        self.assertEqual(
            tuple(index.dialect_options["postgresql"]["include"]),
            ("user_id", "expires_at"),
        )
        self.assertEqual(
            str(index.dialect_options["postgresql"]["where"]),
            "invalidated_at IS NULL",
        )

        migration_source = MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertIn('revision = "20260830_0048"', migration_source)
        self.assertIn('down_revision = "20260829_0047"', migration_source)
        self.assertIn("CREATE INDEX CONCURRENTLY IF NOT EXISTS", migration_source)
        self.assertIn("pg_index.indisvalid", migration_source)
        self.assertIn("DROP INDEX CONCURRENTLY", migration_source)

    def test_upgrade_rejects_valid_same_name_with_unexpected_definition_or_predicate(self) -> None:
        migration = READY_VOTE_MIGRATION
        cases = (
            (
                "definition",
                {
                    "indexdef": (
                        "CREATE INDEX ix_sessions_ready_vote_auth "
                        "ON platform.sessions (user_id)"
                    )
                },
            ),
            (
                "predicate",
                {"predicate": "(invalidated_at IS NOT NULL)"},
            ),
        )

        for label, overrides in cases:
            with self.subTest(mismatch=label):
                fake_op = _fake_alembic_op()
                state = _catalog_state(**overrides)
                with (
                    mock.patch.object(
                        migration, "_read_index_catalog", return_value=state
                    ),
                    mock.patch.object(migration, "op", fake_op),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "valid but unexpected catalog definition",
                    ):
                        migration.upgrade()
                fake_op.execute.assert_not_called()

    def test_upgrade_drops_and_recreates_invalid_interrupted_index(self) -> None:
        migration = READY_VOTE_MIGRATION
        fake_op = _fake_alembic_op()
        invalid_state = _catalog_state(is_valid=False, is_ready=False)
        valid_state = _catalog_state()

        with (
            mock.patch.object(
                migration,
                "_read_index_catalog",
                side_effect=[invalid_state, valid_state],
            ),
            mock.patch.object(migration, "op", fake_op),
        ):
            migration.upgrade()

        statements = [call.args[0] for call in fake_op.execute.call_args_list]
        self.assertEqual(
            statements,
            [
                f"DROP INDEX CONCURRENTLY IF EXISTS platform.{migration.INDEX_NAME}",
                (
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                    f"{migration.INDEX_NAME} ON platform.sessions (token_digest) "
                    "INCLUDE (user_id, expires_at) WHERE invalidated_at IS NULL"
                ),
            ],
        )

    def test_interrupted_create_cleans_up_invalid_catalog_entry(self) -> None:
        migration = READY_VOTE_MIGRATION
        fake_op = _fake_alembic_op()
        invalid_state = _catalog_state(is_valid=False, is_ready=False)
        executed_statements = []

        def execute(statement):
            statement = str(statement)
            executed_statements.append(statement)
            if statement.startswith("CREATE INDEX CONCURRENTLY"):
                raise RuntimeError("simulated interrupted build")

        fake_op.execute.side_effect = execute
        with (
            mock.patch.object(
                migration, "_read_index_catalog", return_value=invalid_state
            ) as read_catalog,
            mock.patch.object(migration, "op", fake_op),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated interrupted build"):
                migration._create_index_concurrently()

        self.assertEqual(read_catalog.call_count, 1)
        self.assertEqual(
            executed_statements,
            [
                (
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                    f"{migration.INDEX_NAME} ON platform.sessions (token_digest) "
                    "INCLUDE (user_id, expires_at) WHERE invalidated_at IS NULL"
                ),
                f"DROP INDEX CONCURRENTLY IF EXISTS platform.{migration.INDEX_NAME}",
            ],
        )

    def test_player_commitment_has_database_enforced_single_active_roster(self) -> None:
        index = next(
            index
            for index in PlayerTournamentCommitment.__table__.indexes
            if index.name == "uq_player_tournament_commitments_active_user"
        )
        self.assertTrue(index.unique)
        self.assertEqual([column.name for column in index.columns], ["user_id"])
        self.assertEqual(
            str(index.dialect_options["postgresql"]["where"]),
            "released_at IS NULL",
        )

    def test_unique_lookup_columns_do_not_request_duplicate_non_unique_indexes(self) -> None:
        columns = (
            User.__table__.c.email,
            UserSession.__table__.c.token_digest,
            Tournament.__table__.c.slug,
            TournamentInvite.__table__.c.code,
        )

        for column in columns:
            with self.subTest(column=column.name):
                self.assertTrue(column.unique)
                self.assertIsNot(column.index, True)
