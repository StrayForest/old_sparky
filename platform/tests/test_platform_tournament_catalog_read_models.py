from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from sqlalchemy.dialects import postgresql

from apps.platform_api.app.services import tournament_catalog_read_models as catalog_models


class TournamentCatalogReadModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_upsert_statement_contains_authoritative_bounded_aggregates(self) -> None:
        db_session = Mock()
        db_session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=1))

        changed = await catalog_models._upsert_projection(
            db_session,
            tournament_id="tournament-1",
        )

        self.assertEqual(changed, 1)
        statement = db_session.execute.await_args.args[0]
        sql = " ".join(
            str(statement.compile(dialect=postgresql.dialect())).split()
        )
        self.assertIn("INSERT INTO platform.tournament_list_read_models", sql)
        self.assertIn("FROM platform.tournaments", sql)
        self.assertIn("JOIN platform.users", sql)
        self.assertIn("SELECT count(platform.tournament_participants.id)", sql)
        self.assertIn("EXISTS (SELECT platform.tournament_deadlock_assignment_runs.id", sql)
        self.assertIn("ON CONFLICT (id) DO UPDATE", sql)
        self.assertIn("excluded.updated_at >=", sql)

    async def test_missing_authoritative_tournament_removes_projection(self) -> None:
        db_session = Mock()
        db_session.scalar = AsyncMock(return_value=None)
        db_session.execute = AsyncMock()
        db_session.commit = AsyncMock()

        changed = await catalog_models.refresh_tournament_list_read_model(
            "tournament-1",
            db_session=db_session,
        )

        self.assertFalse(changed)
        db_session.execute.assert_awaited_once()
        db_session.commit.assert_not_awaited()
        sql = " ".join(
            str(db_session.execute.await_args.args[0].compile(dialect=postgresql.dialect())).split()
        )
        self.assertIn("DELETE FROM platform.tournament_list_read_models", sql)

    async def test_projection_source_can_be_scoped_to_one_organizer(self) -> None:
        statement = catalog_models._projection_source(organizer_user_id="user-1")
        sql = " ".join(
            str(statement.compile(dialect=postgresql.dialect())).split()
        )
        self.assertIn("platform.tournaments.organizer_user_id =", sql)
        self.assertIn("platform.player_profiles", sql)


if __name__ == "__main__":
    unittest.main()
