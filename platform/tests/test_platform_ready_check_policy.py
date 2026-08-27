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

    def test_distributed_simultaneous_schedule_matches_global_proportional_quotas(self) -> None:
        demands = (
            ReadyCheckDemand("a", self.starts_at, 7_000),
            ReadyCheckDemand("b", self.starts_at, 5_000),
        )
        plan = ready_check_preparation_plan(demands, already_connected=2_000)
        allocations = proportional_ready_check_capacity(
            demands,
            capacity=10_000 - plan.already_connected,
        )

        scheduled = {tournament_id: 0 for tournament_id in allocations}
        polling = {tournament_id: 0 for tournament_id in allocations}
        for demand in demands:
            for user_index in range(demand.eligible_count):
                _, _, mode = ready_check_user_admission(
                    plan,
                    demand=demand,
                    user_id=f"{demand.tournament_id}-user-{user_index}",
                    sse_quota=allocations[demand.tournament_id],
                    now=plan.preparation_starts_at,
                )
                if mode == "scheduled_sse":
                    scheduled[demand.tournament_id] += 1
                elif mode == "polling":
                    polling[demand.tournament_id] += 1

        self.assertEqual(allocations, {"a": 4_667, "b": 3_333})
        # User IDs are independently hashed, so the distributed client-side
        # schedule samples each quota rather than enumerating a global user
        # list. Validate proportional fairness with a bounded sampling error;
        # Redis remains the final 10,000-seat admission guard at T.
        self.assertAlmostEqual(scheduled["a"] / sum(scheduled.values()), 7 / 12, delta=0.02)
        self.assertAlmostEqual(scheduled["b"] / sum(scheduled.values()), 5 / 12, delta=0.02)
        self.assertLessEqual(sum(scheduled.values()), 10_000 - plan.already_connected)
        self.assertEqual(
            polling,
            {
                "a": 7_000 - scheduled["a"],
                "b": 5_000 - scheduled["b"],
            },
        )

    def test_total_demand_below_capacity_is_not_renormalized(self) -> None:
        self.assertEqual(
            proportional_ready_check_capacity(
                [
                    ReadyCheckDemand("a", self.starts_at, 700),
                    ReadyCheckDemand("b", self.starts_at, 300),
                ],
                capacity=10_000,
            ),
            {"a": 700, "b": 300},
        )

    def test_late_arrivals_use_authoritative_state_polling_after_ready_check_start(self) -> None:
        demand = ReadyCheckDemand("a", self.starts_at, 7_000)
        plan = ready_check_preparation_plan((demand,))
        open_at, priority, mode = ready_check_user_admission(
            plan,
            demand=demand,
            user_id="late-arrival",
            sse_quota=0,
            now=self.starts_at + timedelta(seconds=1),
        )

        self.assertEqual(open_at, self.starts_at)
        self.assertEqual(priority, READY_CHECK_POLLING_ADMISSION_PRIORITY)
        self.assertEqual(mode, "polling")

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
        self.assertEqual(late_open_at, self.starts_at)
        self.assertEqual(late_priority, READY_CHECK_POLLING_ADMISSION_PRIORITY)
        self.assertEqual(late_mode, "polling")

    def test_invalid_rate_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ready_check_preparation_plan(
                [ReadyCheckDemand("a", self.starts_at, 1)],
                safe_open_rate_per_second=0,
            )
