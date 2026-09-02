from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from python_packages.platform_infra import performance


class RequestPerformanceMiddlewareTests(unittest.TestCase):
    def settings(self, *, log_mutations: bool = True) -> SimpleNamespace:
        return SimpleNamespace(
            platform_perf_log_mutations=log_mutations,
            platform_perf_slow_request_ms=1000,
            platform_perf_slow_db_ms=500,
            platform_perf_sql_count_threshold=25,
        )

    def metrics(self, *, method: str) -> performance.RequestPerformanceMetrics:
        return performance.RequestPerformanceMetrics(
            request_id="request",
            method=method,
            path="/api/v1/tournaments/demo/join",
            started_at=perf_counter(),
            sql_query_count=3,
            sql_time_seconds=0.008,
            response_bytes=320,
        )

    def test_mutations_are_logged_below_slow_threshold(self) -> None:
        middleware = performance.RequestPerformanceMiddleware(app=None)
        with (
            patch.object(performance, "get_settings", return_value=self.settings()),
            patch.object(performance.logger, "info") as log_info,
        ):
            middleware._log_if_slow(
                {"route": SimpleNamespace(path="/tournaments/{slug}/join")},
                self.metrics(method="POST"),
                201,
            )

        log_info.assert_called_once()
        self.assertEqual(log_info.call_args.args[-5], 320)
        self.assertEqual(log_info.call_args.args[-4], "-")
        self.assertEqual(log_info.call_args.args[-3], 0.0)
        self.assertEqual(log_info.call_args.args[-2:], ("-", "-"))

    def test_fast_reads_are_not_logged_by_mutation_rule(self) -> None:
        middleware = performance.RequestPerformanceMiddleware(app=None)
        with (
            patch.object(performance, "get_settings", return_value=self.settings()),
            patch.object(performance.logger, "info") as log_info,
        ):
            middleware._log_if_slow(
                {"route": SimpleNamespace(path="/tournaments/{slug}")},
                self.metrics(method="GET"),
                200,
            )

        log_info.assert_not_called()

    def test_fast_failed_requests_are_always_logged_as_warnings(self) -> None:
        middleware = performance.RequestPerformanceMiddleware(app=None)
        with (
            patch.object(performance, "get_settings", return_value=self.settings(log_mutations=False)),
            patch.object(performance.logger, "info") as log_info,
            patch.object(performance.logger, "warning") as log_warning,
        ):
            middleware._log_if_slow(
                {"route": SimpleNamespace(path="/tournaments/{slug}")},
                self.metrics(method="GET"),
                503,
            )

        log_info.assert_not_called()
        log_warning.assert_called_once()

    def test_ready_vote_pool_wait_does_not_log_every_successful_vote(self) -> None:
        middleware = performance.RequestPerformanceMiddleware(app=None)
        metrics = self.metrics(method="POST")
        metrics.path = "/api/v1/tournaments/demo/deadlock/ready-check/vote"
        metrics.pool_checkout_wait_seconds = 0.2
        with (
            patch.object(performance, "get_settings", return_value=self.settings()),
            patch.object(performance.logger, "info") as log_info,
        ):
            middleware._log_if_slow(
                {"route": SimpleNamespace(path="/{slug}/deadlock/ready-check/vote")},
                metrics,
                200,
            )

        log_info.assert_not_called()

    def test_ready_vote_checkout_records_count_and_duration_separately(self) -> None:
        token = performance.start_request_metrics(
            "POST",
            "/api/v1/tournaments/demo/deadlock/ready-check/vote",
        )
        try:
            performance.record_pool_checkout_wait(0.004)
            performance.record_ready_vote_checkout(0.006)
            metrics = performance.current_request_metrics()
            self.assertIsNotNone(metrics)
            assert metrics is not None
            self.assertEqual(metrics.ready_vote_checkout_count, 1)
            self.assertEqual(metrics.ready_vote_checkout_ms, 6.0)
            self.assertEqual(metrics.pool_checkout_wait_seconds, 0.004)
        finally:
            performance.reset_request_metrics(token)

    def test_pool_hold_records_separately_from_checkout_wait(self) -> None:
        token = performance.start_request_metrics(
            "GET",
            "/api/v1/users/me",
        )
        try:
            performance.record_pool_checkout_wait(0.012)
            performance.record_pool_connection_hold(0.456)
            metrics = performance.current_request_metrics()
            self.assertIsNotNone(metrics)
            assert metrics is not None
            self.assertEqual(metrics.pool_checkout_wait_seconds, 0.012)
            self.assertEqual(metrics.pool_connection_hold_seconds, 0.456)
            self.assertEqual(metrics.pool_connection_hold_count, 1)
        finally:
            performance.reset_request_metrics(token)

    def test_pool_hold_fields_are_emitted_for_slow_reads(self) -> None:
        middleware = performance.RequestPerformanceMiddleware(app=None)
        metrics = self.metrics(method="GET")
        metrics.path = "/api/v1/users/me"
        metrics.pool_checkout_wait_seconds = 0.2
        metrics.pool_connection_hold_seconds = 0.9
        metrics.pool_connection_hold_count = 1
        with (
            patch.object(performance, "get_settings", return_value=self.settings()),
            patch.object(performance.logger, "info") as log_info,
        ):
            middleware._log_if_slow(
                {"route": SimpleNamespace(path="/users/me")},
                metrics,
                200,
            )
            rendered = log_info.call_args.args[0] % log_info.call_args.args[1:]
        self.assertIn("pool_checkout_wait_ms=200.00", rendered)
        self.assertIn("pool_connection_hold_ms=900.00", rendered)
        self.assertIn("pool_connection_hold_count=1", rendered)

    def test_workspace_stages_are_recorded_on_the_existing_request_metrics(self) -> None:
        token = performance.start_request_metrics(
            "GET",
            "/api/v1/tournaments/demo/workspace",
        )
        try:
            performance.record_workspace_stage("workspace_auth", 0.004)
            performance.record_workspace_stage("workspace_auth", 0.001)
            performance.record_workspace_stage("workspace_etag", 0.002)
            performance.record_workspace_stage("ready_vote_auth", 0.5)
            metrics = performance.current_request_metrics()
            self.assertIsNotNone(metrics)
            assert metrics is not None
            self.assertEqual(metrics.workspace_stage_seconds["workspace_auth"], 0.005)
            self.assertEqual(metrics.workspace_stage_seconds["workspace_etag"], 0.002)
            self.assertNotIn("ready_vote_auth", metrics.workspace_stage_seconds)
        finally:
            performance.reset_request_metrics(token)

    def test_workspace_log_exposes_conditional_preflight_stage(self) -> None:
        middleware = performance.RequestPerformanceMiddleware(app=None)
        metrics = self.metrics(method="GET")
        metrics.path = "/api/v1/tournaments/demo/workspace"
        metrics.workspace_stage_seconds["workspace_conditional_preflight"] = 0.012
        metrics.sql_time_seconds = 1.0
        with (
            patch.object(performance, "get_settings", return_value=self.settings()),
            patch.object(performance.logger, "info") as log_info,
        ):
            middleware._log_if_slow(
                {"route": SimpleNamespace(path="/{slug}/workspace")},
                metrics,
                304,
            )

        rendered = log_info.call_args.args[0] % log_info.call_args.args[1:]
        self.assertIn("workspace_conditional_preflight_ms=12.00", rendered)
        self.assertIn("workspace_tournament_base_ms=0.00", rendered)

    def test_ready_vote_log_format_exposes_checkout_metrics(self) -> None:
        middleware = performance.RequestPerformanceMiddleware(app=None)
        metrics = self.metrics(method="POST")
        metrics.path = "/api/v1/tournaments/demo/deadlock/ready-check/vote"
        metrics.ready_vote_checkout_count = 1
        metrics.ready_vote_checkout_ms = 6.0
        metrics.ready_vote_admission_inflight = 3
        metrics.ready_vote_admission_limit = 4
        metrics.ready_vote_admission_wait_ms = 25.5
        metrics.ready_vote_admitted_total = 12
        metrics.ready_vote_shed_total = 2
        metrics.ready_vote_controller_state = "pressure"
        metrics.ready_vote_controller_limit_changes = 3
        metrics.ready_vote_cpu_pressure = 81.5
        metrics.ready_vote_pool_wait_ms = 4.0
        metrics.ready_vote_cpu_monitor_sample_ms = 0.12
        metrics.ready_vote_cpu_monitor_samples = 20
        metrics.ready_vote_admission_shed = True
        # Force the slow path to verify the emitted field names and values
        # without relying on process-global log state.
        metrics.sql_time_seconds = 1.0
        with (
            patch.object(performance, "get_settings", return_value=self.settings()),
            patch.object(performance.logger, "info") as log_info,
        ):
            middleware._log_if_slow(
                {"route": SimpleNamespace(path="/{slug}/deadlock/ready-check/vote")},
                metrics,
                200,
            )
            log_info.assert_called_once()
            rendered = log_info.call_args.args[0] % log_info.call_args.args[1:]
        self.assertIn("ready_vote_checkout_count=1", rendered)
        self.assertIn("ready_vote_checkout_ms=6.00", rendered)
        self.assertIn("ready_vote_admission_inflight=3", rendered)
        self.assertIn("ready_vote_admission_limit=4", rendered)
        self.assertIn("ready_vote_admission_wait_ms=25.50", rendered)
        self.assertIn("ready_vote_controller_state=pressure", rendered)
        self.assertIn("ready_vote_cpu_pressure=81.50", rendered)
        self.assertIn("ready_vote_cpu_monitor_sample_ms=0.12", rendered)
        self.assertIn("ready_vote_cpu_monitor_samples=20", rendered)
        self.assertIn("ready_vote_admission_shed=True", rendered)
        self.assertNotIn("ready_vote_counter_ms", rendered)

    def test_ready_vote_error_log_includes_zero_checkout_metrics(self) -> None:
        middleware = performance.RequestPerformanceMiddleware(app=None)
        metrics = self.metrics(method="POST")
        metrics.path = "/api/v1/tournaments/demo/deadlock/ready-check/vote"
        with (
            patch.object(performance, "get_settings", return_value=self.settings()),
            patch.object(performance.logger, "warning") as log_warning,
        ):
            middleware._log_if_slow(
                {"route": SimpleNamespace(path="/{slug}/deadlock/ready-check/vote")},
                metrics,
                503,
            )

        log_warning.assert_called_once()
        rendered = log_warning.call_args.args[0] % log_warning.call_args.args[1:]
        self.assertIn("ready_vote_checkout_count=0", rendered)
        self.assertIn("ready_vote_checkout_ms=0.00", rendered)

    def test_qa_phase_header_is_bounded_and_namespaced(self) -> None:
        self.assertEqual(
            performance.qa_phase_from_scope(
                {"headers": [(b"x-platform-qa-phase", b"write_ready_burst_5s")]}
            ),
            "write_ready_burst_5s",
        )
        self.assertIsNone(
            performance.qa_phase_from_scope(
                {"headers": [(b"x-platform-qa-phase", b"arbitrary-user-value")]}
            )
        )


if __name__ == "__main__":
    unittest.main()
