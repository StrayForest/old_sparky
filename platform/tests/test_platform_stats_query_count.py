from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from apps.platform_api.app.api.routes.stats import stats_overview
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class PlatformStatsQueryCountTests(PlatformIsolatedAsyncioTestCase):
    async def test_stats_overview_uses_two_database_round_trips(self) -> None:
        metric_result = Mock()
        metric_result.mappings.return_value.one.return_value = {
            "total_tournaments": 10,
            "completed_tournaments": 3,
            "active_upcoming_tournaments": 4,
            "registered_participants": 8,
            "completed_matches": 6,
            "deadlock_profiles_total": 7,
            "registered_participants_with_deadlock_profile": 6,
        }
        rank_result = Mock()
        rank_result.all.return_value = [
            ("Initiate", 2),
            ("Phantom", 5),
        ]
        db_session = Mock()
        db_session.execute = AsyncMock(side_effect=[metric_result, rank_result])

        response = await stats_overview(db_session=db_session)

        self.assertEqual(db_session.execute.await_count, 2)
        self.assertEqual(response.total_tournaments, 10)
        self.assertEqual(response.completed_tournaments, 3)
        self.assertEqual(response.active_upcoming_tournaments, 4)
        self.assertEqual(response.registered_participants, 8)
        self.assertEqual(response.completed_matches, 6)
        self.assertEqual(response.deadlock_profiles_total, 7)
        self.assertEqual(response.registered_participants_with_deadlock_profile, 6)
        self.assertEqual(response.deadlock_profile_coverage_percent, 75.0)
        self.assertEqual(
            {item.rank: item.count for item in response.deadlock_rank_distribution},
            {"Initiate": 2, "Phantom": 5},
        )
