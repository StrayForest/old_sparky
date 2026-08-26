from __future__ import annotations

import unittest

from sqlalchemy.dialects import postgresql

from apps.platform_api.app.api.routes import tournaments as tournament_routes


class PlatformTournamentQueryShapeTests(unittest.TestCase):
    def test_single_tournament_counts_are_correlated_to_the_outer_tournament(self) -> None:
        statement = tournament_routes.tournament_with_counts_stmt(single_tournament=True)
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        self.assertIn(
            "platform.tournament_participants.tournament_id = platform.tournaments.id",
            sql,
        )
        self.assertIn(
            "platform.tournament_deadlock_assignment_runs.tournament_id = platform.tournaments.id",
            sql,
        )
        self.assertNotIn("GROUP BY platform.tournament_participants.tournament_id", sql)

    def test_collection_counts_keep_the_grouped_page_shape(self) -> None:
        statement = tournament_routes.tournament_with_counts_stmt()
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        self.assertIn("GROUP BY platform.tournament_participants.tournament_id", sql)


if __name__ == "__main__":
    unittest.main()
