from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.platform_external_load import (
    ExternalLoadError,
    LogicalRequestResult,
    RequestResult,
    VirtualUser,
    _route_for_read,
    _ready_vote_action,
    load_manifest,
    run_load,
    spread_offsets,
    summarize_logical_results,
    summarize_results,
)


def manifest_payload() -> dict[str, object]:
    return {
        "schema": 1,
        "purpose": "external_ready_vote",
        "origin": "https://old-sparky.com",
        "session_cookie_name": "deadlock_platform_session",
        "csrf_cookie_name": "deadlock_platform_session_csrf",
        "marker": "preprod26082900000000ab",
        "tournaments": [
            {"id": "tournament-1", "slug": "qa-tournament", "user_count": 2}
        ],
        "users": [
            {
                "user_id": "user-00000001",
                "tournament_slug": "qa-tournament",
                "session_token": "s" * 64,
                "csrf_token": "c" * 64,
            },
            {
                "user_id": "user-00000002",
                "tournament_slug": "qa-tournament",
                "session_token": "t" * 64,
                "csrf_token": "d" * 64,
            },
        ],
    }


def load_manifest_from_payload(
    payload: dict[str, object],
) -> tuple[dict[str, object], list[VirtualUser]]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "manifest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_manifest(path)


class ExternalLoadTests(unittest.TestCase):
    def test_read_mix_uses_the_current_tournament_page_request(self) -> None:
        route = _route_for_read(0, "qa-tournament")

        self.assertEqual(
            route,
            "/tournaments/qa-tournament/workspace?participants_limit=0"
            "&participants_offset=0&workspace_view=detail&include_current_user=false",
        )

    def test_spread_offsets_are_bounded_and_deterministic(self) -> None:
        self.assertEqual(spread_offsets(5, 10), [0.0, 2.0, 4.0, 6.0, 8.0])
        self.assertEqual(spread_offsets(2, 0), [0.0, 0.0])
        self.assertEqual(spread_offsets(0, 10), [])

    def test_manifest_validates_identity_and_tournament_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest_payload()), encoding="utf-8")
            payload, users = load_manifest(path)

        self.assertEqual(payload["marker"], "preprod26082900000000ab")
        self.assertEqual(len(users), 2)
        self.assertEqual(users[0].tournament_slug, "qa-tournament")

    def test_manifest_accepts_production_host_cookie_names(self) -> None:
        payload = manifest_payload()
        payload["session_cookie_name"] = "__Host-deadlock_platform_session"
        payload["csrf_cookie_name"] = "__Host-deadlock_platform_session_csrf"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            load_manifest(path)

    def test_manifest_rejects_duplicate_session_material(self) -> None:
        payload = manifest_payload()
        users = payload["users"]
        assert isinstance(users, list)
        users[1] = dict(users[1])
        users[1]["session_token"] = users[0]["session_token"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ExternalLoadError):
                load_manifest(path)

    def test_summary_contains_metrics_but_not_session_material(self) -> None:
        secret = "session-secret-that-must-not-be-serialized"
        result = RequestResult(
            phase="write_external_vote",
            method="POST",
            path="/tournaments/qa/deadlock/ready-check/vote",
            status=200,
            elapsed_ms=100.0,
            ok=True,
            response_bytes=120,
            response_json={"changed": True, "internal": secret},
        )
        summary = summarize_results([result])
        serialized = json.dumps(summary)
        self.assertEqual(summary["scope"], "full_population")
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["errors"], 0)
        self.assertNotIn(secret, serialized)

    def test_ready_vote_retries_only_explicit_overload_and_reports_logical_latency(self) -> None:
        _, users = load_manifest_from_payload(manifest_payload())
        responses = [
            RequestResult(
                phase="write_external_vote",
                method="POST",
                path="/tournaments/qa/deadlock/ready-check/vote",
                status=503,
                elapsed_ms=12.0,
                ok=False,
                response_bytes=88,
                response_json={
                    "code": "READY_VOTE_OVERLOADED",
                    "retryable": True,
                    "retry_after_ms": 250,
                },
            ),
            RequestResult(
                phase="write_external_vote",
                method="POST",
                path="/tournaments/qa/deadlock/ready-check/vote",
                status=200,
                elapsed_ms=18.0,
                ok=True,
                response_bytes=120,
                response_json={"changed": True},
            ),
        ]

        with (
            patch("tools.platform_external_load._ready_vote_request", side_effect=responses),
            patch("tools.platform_external_load.time.sleep") as sleep,
            patch("tools.platform_external_load.random.uniform", return_value=200.0),
        ):
            logical = _ready_vote_action(
                "https://old-sparky.com",
                users[0],
                "write_external_vote",
                1.0,
                session_cookie_name="session",
                csrf_cookie_name="csrf",
            )

        self.assertEqual(len(logical.attempts), 2)
        self.assertEqual(logical.retry_count, 1)
        sleep.assert_called_once_with(0.25)
        summary = summarize_logical_results([logical])
        self.assertEqual(summary["actions"], 1)
        self.assertEqual(summary["final_successes"], 1)
        self.assertEqual(summary["total_retries"], 1)
        self.assertEqual(summary["changed_counts"], {"True": 1})

    def test_logical_summary_keeps_final_failure_rate_separate_from_raw_overloads(self) -> None:
        overloaded = RequestResult(
            phase="write_external_vote",
            method="POST",
            path="/tournaments/qa/deadlock/ready-check/vote",
            status=503,
            elapsed_ms=4.0,
            ok=False,
            response_bytes=80,
            response_json={
                "code": "READY_VOTE_OVERLOADED",
                "retryable": True,
                "retry_after_ms": 250,
            },
        )
        logical = LogicalRequestResult([overloaded], elapsed_ms=4.0)

        raw = summarize_results([overloaded])
        logical_summary = summarize_logical_results([logical])
        self.assertEqual(raw["temporary_overload_responses"], 1)
        self.assertEqual(logical_summary["final_failures"], 1)
        self.assertEqual(logical_summary["final_failure_rate_percent"], 100.0)

    def test_ready_vote_load_report_separates_primary_http_and_logical_layers(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_action(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
        ) -> LogicalRequestResult:
            result = RequestResult(
                phase=phase,
                method="POST",
                path=f"/tournaments/{user.tournament_slug}/deadlock/ready-check/vote",
                status=200,
                elapsed_ms=12.0,
                ok=True,
                response_bytes=120,
                response_json={"changed": True},
            )
            return LogicalRequestResult([result], elapsed_ms=12.0)

        def fake_state_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=8.0,
                ok=True,
                response_bytes=150,
                response_json={"active_round": {"ready_count": 2}},
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._ready_vote_action", side_effect=fake_action),
            patch("tools.platform_external_load._request", side_effect=fake_state_request),
        ):
            report = run_load(
                payload,
                users,
                mode="ready-vote",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=600,
                p99_budget_ms=1000,
            )

        self.assertTrue(report["acceptance"]["passed"])
        self.assertEqual(report["raw_http"]["requests"], 2)
        self.assertEqual(report["logical"]["actions"], 2)
        self.assertEqual(report["logical"]["final_failures"], 0)
        self.assertGreater(report["logical"]["successful_goodput_actions_per_second"], 0)

    def test_stress_allows_shed_duplicates_but_requires_successful_noops(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_action(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
        ) -> LogicalRequestResult:
            duplicate = phase.endswith("duplicate")
            result = RequestResult(
                phase=phase,
                method="POST",
                path=f"/tournaments/{user.tournament_slug}/deadlock/ready-check/vote",
                status=503 if duplicate else 200,
                elapsed_ms=12.0,
                ok=not duplicate,
                response_bytes=120,
                response_json=(
                    {"code": "READY_VOTE_OVERLOADED", "retryable": True}
                    if duplicate
                    else {"changed": True}
                ),
                error_kind="http_error" if duplicate else None,
            )
            return LogicalRequestResult(
                [result], elapsed_ms=12.0, user_id=user.user_id
            )

        def fake_state_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=8.0,
                ok=True,
                response_bytes=150,
                response_json={"active_round": {"ready_count": 2}},
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._ready_vote_action", side_effect=fake_action),
            patch("tools.platform_external_load._request", side_effect=fake_state_request),
        ):
            report = run_load(
                payload,
                users,
                mode="ready-vote",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=1,
                manual_refresh_count=0,
                p95_budget_ms=600,
                p99_budget_ms=1000,
                scenario_kind="stress",
                acceptance_contract={
                    "kind": "stress",
                    "accepted_request_latency": {
                        "p50_ms": 1500,
                        "p90_ms": 3000,
                        "p95_ms": 5000,
                        "p99_ms": 8000,
                    },
                    "max_shed_percent": 99.9,
                    "max_retry_amplification_percent": 200,
                },
            )

        self.assertTrue(report["acceptance"]["contract_ok"])
        self.assertFalse(report["acceptance"]["passed"])
        self.assertEqual(report["phases"]["duplicate"]["logical"]["final_failures"], 1)

    def test_read_mix_manual_refresh_uses_conditional_workspace_request(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)
        calls: list[dict[str, object]] = []

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            calls.append(
                {
                    "phase": phase,
                    "path": path,
                    "expected_statuses": expected_statuses,
                    "extra_headers": extra_headers,
                }
            )
            is_refresh = extra_headers is not None
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=304 if is_refresh else 200,
                elapsed_ms=10.0,
                ok=(304 if is_refresh else 200) in expected_statuses,
                response_bytes=0,
                response_etag='"workspace-etag"',
            )

        with patch("tools.platform_external_load._trace", side_effect=fake_trace):
            with patch("tools.platform_external_load._request", side_effect=fake_request):
                report = run_load(
                    payload,
                    users,
                    mode="read-mix",
                    spread_seconds=0,
                    concurrency=1,
                    timeout=1,
                    duplicate_count=0,
                    manual_refresh_count=1,
                    p95_budget_ms=1000,
                    p99_budget_ms=2000,
                )

        self.assertTrue(report["acceptance"]["passed"])
        self.assertEqual(report["scope"], "full_population")
        self.assertEqual(report["phases"]["read_mix"]["requests"], 2)
        self.assertEqual(report["phases"]["manual_refresh"]["requests"], 1)
        self.assertEqual(report["phases"]["manual_refresh"]["status_counts"], {"304": 1})
        refresh_call = next(call for call in calls if call["phase"] == "manual_workspace_refresh")
        self.assertEqual(refresh_call["expected_statuses"], frozenset({200, 304}))
        self.assertEqual(refresh_call["extra_headers"], {"If-None-Match": '"workspace-etag"'})

    def test_read_mix_concurrency_ramp_reports_each_full_population_stage(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)
        calls: list[str] = []

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            calls.append(phase)
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=10.0,
                ok=True,
                response_bytes=150,
                response_etag='"workspace-etag"' if user.user_id == "user-00000001" else None,
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._request", side_effect=fake_request),
        ):
            report = run_load(
                payload,
                users,
                mode="read-mix",
                spread_seconds=0,
                concurrency=2,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=1000,
                p99_budget_ms=2000,
                concurrency_stages=[1, 2],
            )

        self.assertTrue(report["acceptance"]["passed"])
        self.assertEqual(report["phases"]["read_mix"]["requests"], 4)
        self.assertEqual(
            report["phases"]["capacity_ramp"]["concurrency_stages"],
            [1, 2],
        )
        self.assertEqual(
            [report["phases"]["capacity_ramp"]["stages"][str(stage)]["requests"] for stage in (1, 2)],
            [2, 2],
        )
        self.assertTrue(all(phase.startswith("scale_external_read_mix_c") for phase in calls))

    def test_ready_vote_rate_plan_reports_offered_rate_per_phase(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_action(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
            retry_policy: dict[str, object] | None = None,
        ) -> LogicalRequestResult:
            result = RequestResult(
                phase=phase,
                method="POST",
                path=f"/tournaments/{user.tournament_slug}/deadlock/ready-check/vote",
                status=200,
                elapsed_ms=10.0,
                ok=True,
                response_bytes=120,
                response_json={"changed": True},
            )
            return LogicalRequestResult([result], elapsed_ms=10.0, user_id=user.user_id)

        def fake_state_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=8.0,
                ok=True,
                response_bytes=150,
                response_json={"active_round": {"ready_count": 2}},
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._ready_vote_action", side_effect=fake_action),
            patch("tools.platform_external_load._request", side_effect=fake_state_request),
        ):
            report = run_load(
                payload,
                users,
                mode="ready-vote",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=600,
                p99_budget_ms=1000,
                phase_plan=[
                    {"name": "rate-1", "target_logical_actions_per_second": 1, "duration_seconds": 1, "logical_actions": 1},
                    {"name": "rate-1b", "target_logical_actions_per_second": 1, "duration_seconds": 1, "logical_actions": 1},
                ],
                scenario_kind="capacity",
                acceptance_contract={
                    "kind": "capacity",
                    "slo": {
                        "accepted_request_latency": {"p50_ms": 250, "p90_ms": 400, "p95_ms": 600, "p99_ms": 1000},
                        "logical_latency": {"p95_ms": 600, "p99_ms": 1000},
                        "logical_final_failure_percent": 0.5,
                        "max_shed_percent": 0,
                        "max_retry_amplification_percent": 0,
                    },
                    "capacity": {"target_logical_actions_per_second": [1, 1], "steady_duration_seconds": 1},
                },
            )

        phases = report["phases"]["ramp"]["phases"]
        self.assertEqual(set(phases), {"rate-1", "rate-1b"})
        self.assertEqual(phases["rate-1"]["logical"]["target_logical_actions_per_second"], 1)
        self.assertGreater(phases["rate-1"]["raw_http"]["attempts_per_second"], 0)


if __name__ == "__main__":
    unittest.main()
