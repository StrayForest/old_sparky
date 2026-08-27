from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from python_packages.platform_infra.ready_check_policy import (
    READY_CHECK_LATE_ADMISSION_PRIORITY,
    READY_CHECK_POLLING_ADMISSION_PRIORITY,
    READY_CHECK_SCHEDULED_ADMISSION_PRIORITY,
    ReadyCheckDemand,
    proportional_ready_check_capacity,
    ready_check_preparation_plan,
    ready_check_user_admission,
)


class PlatformReadyCheckPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.starts_at = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)

    def test_preparation_window_scales_with_demand_and_safe_open_rate(self) -> None:
        small = ready_check_preparation_plan(
            [ReadyCheckDemand("small", self.starts_at, 60)]
        )
        large = ready_check_preparation_plan(
            [ReadyCheckDemand("large", self.starts_at, 5_000)]
        )

        self.assertEqual(small.expected_demand, 60)
        self.assertEqual(small.preparation_starts_at, self.starts_at - timedelta(seconds=18))
        self.assertEqual(large.preparation_starts_at, self.starts_at - timedelta(seconds=215))

    def test_simultaneous_demand_is_allocated_fairly(self) -> None:
        allocations = proportional_ready_check_capacity(
            [
                ReadyCheckDemand("a", self.starts_at, 7_000),
                ReadyCheckDemand("b", self.starts_at + timedelta(minutes=1), 5_000),
            ],
            capacity=10_000,
        )

        self.assertEqual(allocations, {"a": 5_833, "b": 4_167})

    def test_outside_quota_uses_polling_until_ready_check_start(self) -> None:
        demand = ReadyCheckDemand("a", self.starts_at, 100)
        plan = ready_check_preparation_plan([demand], max_preparation_seconds=120)
        open_at, priority, mode = ready_check_user_admission(
            plan,
            demand=demand,
            user_id="user-with-stable-slot",
            sse_quota=0,
            now=self.starts_at - timedelta(seconds=1),
        )

        self.assertEqual(open_at, self.starts_at)
        self.assertEqual(priority, READY_CHECK_POLLING_ADMISSION_PRIORITY)
        self.assertEqual(mode, "polling")

    def test_scheduled_user_becomes_late_without_waiting_for_old_queue(self) -> None:
        demand = ReadyCheckDemand("a", self.starts_at, 100)
        plan = ready_check_preparation_plan([demand])
        open_at, priority, mode = ready_check_user_admission(
            plan,
            demand=demand,
            user_id="late-user",
            sse_quota=100,
            now=self.starts_at - timedelta(seconds=1),
        )
        self.assertGreaterEqual(open_at, plan.preparation_starts_at)
        self.assertLess(open_at, self.starts_at)
        self.assertIn(priority, {READY_CHECK_SCHEDULED_ADMISSION_PRIORITY, READY_CHECK_LATE_ADMISSION_PRIORITY})
        self.assertIn(mode, {"scheduled_sse", "late_sse"})

        late_open_at, late_priority, late_mode = ready_check_user_admission(
            plan,
            demand=demand,
            user_id="late-user",
            sse_quota=100,
            now=self.starts_at + timedelta(seconds=1),
        )
        self.assertEqual(late_open_at, self.starts_at + timedelta(seconds=1))
        self.assertEqual(late_priority, READY_CHECK_LATE_ADMISSION_PRIORITY)
        self.assertEqual(late_mode, "late_sse")

    def test_invalid_rate_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ready_check_preparation_plan(
                [ReadyCheckDemand("a", self.starts_at, 1)],
                safe_open_rate_per_second=0,
            )
