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
