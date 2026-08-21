from __future__ import annotations

import unittest

from python_packages.platform_infra.models import (
    PlayerTournamentCommitment,
    Tournament,
    TournamentInvite,
    User,
    UserSession,
)


class PlatformModelIndexTests(unittest.TestCase):
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
