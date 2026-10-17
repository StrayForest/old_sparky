from __future__ import annotations

from copy import deepcopy
import cProfile
import io
import json
import marshal
import os
from pathlib import Path
import re
import signal
import subprocess
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.platform_external_load import (
    ExternalLoadError,
    RequestResult,
    VirtualUser,
    _annotate_timing,
    build_ready_vote_transport_pair_plan,
    planned_http_attempts,
    run_load,
    summarize_results,
)
from tools.platform_http_transport import TransportResponse
from tools.platform_external_load_observer import (
    ObserverProgressReceipt,
    OBSERVER_PROGRESS_NAME,
    _signal_api_workers_detailed,
    _signal_delivery_summary,
    api_worker_identities,
    cpu_profile_summary,
    profile_artifact_snapshot,
    signal_api_workers,
)
from tools.platform_load import (
    LoadProfileError,
    ensure_dispatchable,
    evaluate_report,
    get_profile,
    load_profiles,
    profile_contract,
    run_profile_worker,
    validate_profile,
    _run_external_load_or_report,
    main as platform_load_main,
)
from tools import platform_tmpfs_allocation_audit as tmpfs_audit


class PerformanceProfileContractTests(unittest.TestCase):
    @staticmethod
    def _bound_report(profile: dict[str, object]) -> dict[str, object]:
        contract = profile_contract(profile)
        planned_work = contract["planned_work"]
        expected_logical_actions = int(planned_work["logical_actions"])
        expected_primary_actions = int(planned_work["primary_logical_actions"])
        expected_state_reads = int(planned_work["state_read_requests"])
        expected_requests = expected_logical_actions + expected_state_reads
        max_http_attempts = int(profile["portfolio"]["request_budget"]["max_http_attempts"])
        contract["offered_logical_actions"] = expected_logical_actions
        contract["primary_http_attempts"] = expected_primary_actions
        contract["http_attempts"] = expected_requests
        contract["total_http_attempts"] = expected_requests
        contract["runtime_http_budget"] = {
            "planned_worst_case": planned_work["http_attempts"],
            "max_http_attempts": max_http_attempts,
            "actual_http_attempts": expected_requests,
            "within_budget": expected_requests <= max_http_attempts,
        }
        return {
            "schema": 1,
            "measurement_schema": 2,
            "timing_schema": 1,
            "profile_id": profile["profile_id"],
            "profile_version": profile["profile_version"],
            "profile_digest": contract["profile_digest"],
            "mode": profile["mode"],
            "environment": profile["portfolio"]["environment"],
            "source_git_sha": "a" * 40,
            "external_run_id": "123",
            "fixture_marker": "preprod26082900000000ab",
            "scope": "full_population",
            "load_contract": contract,
            "acceptance": {"contract_ok": False, "passed": False},
            "authoritative": True,
            "dispatchable": True,
            "phases": {"primary": {}, "duplicate": {}, "state": {}},
            "overall": {
                "scope": "full_population",
                "requests": expected_requests,
                "errors": 0,
                "successful_responses": expected_requests,
                "final_failure_rate_percent": 0,
                "status_counts": {"200": expected_requests},
            },
            "logical": {
                "scope": "logical_user_actions",
                "actions": expected_logical_actions,
                "final_successes": expected_logical_actions,
                "final_failures": 0,
                "final_failure_rate_percent": 0,
                "total_retries": 0,
                "retry_amplification_percent": 0,
                "final_status_counts": {"200": expected_logical_actions},
            },
            "raw_http": {
                "scope": "full_population",
                "requests": expected_logical_actions,
                "errors": 0,
                "successful_responses": expected_logical_actions,
                "final_failure_rate_percent": 0,
                "temporary_overload_rate_percent": 0,
                "retry_attempts": 0,
                "total_retries": 0,
                "retry_amplification_percent": 0,
                "unexpected_statuses": 0,
                "status_counts": {"200": expected_logical_actions},
                "state_read_requests": expected_state_reads,
                "total_requests_including_state": expected_requests,
            },
        }

    def test_active_registry_exposes_bounded_portfolio_metadata(self) -> None:
        profiles = load_profiles()

        self.assertEqual(len(profiles), 12)
        self.assertEqual(
            {profile["portfolio"]["status"] for profile in profiles.values()},
            {"active"},
        )
        for profile in profiles.values():
            portfolio = profile["portfolio"]
            self.assertIn(portfolio["class"], {"default", "diagnostic"})
            self.assertIn(
                portfolio["status"],
                {"active", "deprecated", "replacement-needed"},
            )
            self.assertGreaterEqual(
                portfolio["request_budget"]["max_http_attempts"],
                portfolio["request_budget"]["max_logical_actions"],
            )
            self.assertGreater(portfolio["cost_budget"]["max_runner_minutes"], 0)
            if profile["mode"] == "tournament-lifecycle":
                self.assertFalse(profile["execution"]["require_exact_observer_binding"])
            else:
                self.assertTrue(profile["execution"]["require_exact_observer_binding"])

        transport_pair = profiles["ready-vote-transport-pair-v1"]
        self.assertEqual(transport_pair["category"], "stress")
        self.assertEqual(transport_pair["acceptance"]["kind"], "stress")
        self.assertEqual(transport_pair["mode"], "ready-vote")
        self.assertEqual(
            transport_pair["portfolio"]["class"],
            "diagnostic",
        )
        self.assertEqual(transport_pair["portfolio"]["cadence"], "on-demand")
        self.assertEqual(
            transport_pair["fixture"],
            {
                "tournament_count": 20,
                "users_per_tournament": 500,
                "setup_concurrency": 20,
                "max_total_users": 10000,
            },
        )
        self.assertEqual(
            [
                (
                    phase["name"],
                    phase["target_logical_actions_per_second"],
                    phase["duration_seconds"],
                    phase["logical_actions"],
                )
                for phase in transport_pair["traffic"]["phases"]
            ],
            [
                ("close-a", 70, 30, 2100),
                ("keepalive-b", 70, 30, 2100),
                ("keepalive-c", 70, 30, 2100),
                ("close-d", 70, 30, 2100),
            ],
        )
        self.assertNotIn("client_transport", transport_pair)

    def test_runtime_budget_includes_retries_duplicates_and_state_reads(self) -> None:
        profiles = load_profiles()
        capacity = profiles["ready-vote-capacity-ramp-v2"]
        saturation = profiles["ready-vote-saturation-ramp-v4"]
        transport_pair = profiles["ready-vote-transport-pair-v1"]

        self.assertEqual(profile_contract(capacity)["planned_work"]["http_attempts"], 31_540)
        self.assertEqual(capacity["portfolio"]["request_budget"]["max_http_attempts"], 31_540)
        self.assertEqual(profile_contract(saturation)["planned_work"]["http_attempts"], 45_940)
        self.assertEqual(saturation["portfolio"]["request_budget"]["max_http_attempts"], 50_000)
        self.assertEqual(
            profile_contract(transport_pair)["planned_work"]["logical_actions"],
            8_400,
        )
        self.assertEqual(
            profile_contract(transport_pair)["planned_work"]["http_attempts"],
            25_220,
        )
        self.assertEqual(
            transport_pair["portfolio"]["request_budget"],
            {
                "max_logical_actions": 8_400,
                "max_http_attempts": 25_220,
                "max_duration_seconds": 300,
            },
        )
        self.assertEqual(transport_pair["portfolio"]["cost_budget"]["max_runner_minutes"], 30)
        pair_manifest = {
            "tournaments": [
                {"slug": f"pair-tournament-{index:02d}", "user_count": 500}
                for index in range(20)
            ]
        }
        pair_users = [
            VirtualUser(
                user_id=f"pair-user-{tournament_index:02d}-{user_index:03d}",
                tournament_slug=f"pair-tournament-{tournament_index:02d}",
                session_token=f"session-{tournament_index:02d}-{user_index:03d}",
                csrf_token=f"csrf-{tournament_index:02d}-{user_index:03d}",
            )
            for tournament_index in range(20)
            for user_index in range(500)
        ]
        pair_plan = build_ready_vote_transport_pair_plan(
            "ready-vote-transport-pair-v1",
            pair_manifest,
            pair_users,
            transport_pair["traffic"]["phases"],
        )
        expected_transports = {
            "close-a": "http1-close",
            "keepalive-b": "http1-keepalive",
            "keepalive-c": "http1-keepalive",
            "close-d": "http1-close",
        }
        used_pair_user_ids: set[str] = set()
        used_pair_tournament_slugs: set[str] = set()
        for phase_name, expected_transport in expected_transports.items():
            phase = pair_plan[phase_name]
            self.assertEqual(phase["transport"], expected_transport)
            self.assertEqual(len(phase["tournament_slugs"]), 5)
            self.assertEqual(len(phase["users"]), 2100)
            self.assertFalse(used_pair_user_ids & {user.user_id for user in phase["users"]})
            self.assertFalse(used_pair_tournament_slugs & set(phase["tournament_slugs"]))
            used_pair_user_ids.update(user.user_id for user in phase["users"])
            used_pair_tournament_slugs.update(phase["tournament_slugs"])
            for slug in phase["tournament_slugs"]:
                self.assertEqual(
                    sum(user.tournament_slug == slug for user in phase["users"]),
                    420,
                )
        self.assertEqual(len(used_pair_user_ids), 8400)
        self.assertEqual(len(used_pair_tournament_slugs), 20)
        self.assertEqual(len(pair_users) - len(used_pair_user_ids), 1600)

        overlapping_pair_users = list(pair_users)
        overlapping_pair_users[2500] = VirtualUser(
            user_id=overlapping_pair_users[0].user_id,
            tournament_slug=overlapping_pair_users[2500].tournament_slug,
            session_token=overlapping_pair_users[2500].session_token,
            csrf_token=overlapping_pair_users[2500].csrf_token,
        )
        with self.assertRaisesRegex(ExternalLoadError, "actor fixture is invalid"):
            build_ready_vote_transport_pair_plan(
                "ready-vote-transport-pair-v1",
                pair_manifest,
                overlapping_pair_users,
                transport_pair["traffic"]["phases"],
            )

        wrong_pair_phases = deepcopy(transport_pair["traffic"]["phases"])
        wrong_pair_phases[1]["name"] = "close-b"
        with self.assertRaisesRegex(ExternalLoadError, "phase contract"):
            build_ready_vote_transport_pair_plan(
                "ready-vote-transport-pair-v1",
                pair_manifest,
                pair_users,
                wrong_pair_phases,
            )

        class RecordingTransportClient:
            instances: list["RecordingTransportClient"] = []

            def __init__(
                self,
                _origin: str,
                *,
                timeout: float,
                max_response_bytes: int,
                reuse_connection: bool,
            ) -> None:
                self.timeout = timeout
                self.max_response_bytes = max_response_bytes
                self.reuse_connection = reuse_connection
                self.requests: list[dict[str, object]] = []
                self.closed = False
                self.last_timing: dict[str, object] = {}
                self.__class__.instances.append(self)

            def request(
                self,
                method: str,
                path: str,
                *,
                headers: dict[str, str],
                body: bytes | None = None,
                timeout: float | None = None,
                deadline: float | None = None,
            ) -> TransportResponse:
                del timeout, deadline
                reused = self.reuse_connection and bool(self.requests)
                self.requests.append(
                    {
                        "method": method,
                        "path": path,
                        "headers": dict(headers),
                        "body": body,
                        "reused": reused,
                    }
                )
                timing: dict[str, object] = {
                    "transport": (
                        "http1-keepalive"
                        if self.reuse_connection
                        else "http1-close"
                    ),
                    "connection_reused": reused,
                    "request_write_ms": 0.1,
                    "edge_wait_ms": 0.2,
                    "ttfb_ms": 0.3,
                    "body_receive_ms": 0.1,
                    "total_ms": 0.4,
                    "http_version": "1.1",
                }
                self.last_timing = timing
                return TransportResponse(
                    status=200,
                    reason="OK",
                    headers={"connection": "keep-alive"},
                    body=b'{"changed":true}',
                    http_version="1.1",
                    connection_reused=reused,
                    timing=timing,
                )

            def close(self) -> None:
                self.closed = True

        phase_observations: list[dict[str, object]] = []

        def fast_rate_phase(
            origin: str,
            phase_users: list[VirtualUser],
            *,
            phase: str,
            duration_seconds: float,
            request_builder,
            result_consumer,
            **_kwargs: object,
        ) -> tuple[list[object], float]:
            phase_observations.append(
                {"phase": phase, "users": len(phase_users), "duration": duration_seconds}
            )
            for index, user in enumerate(phase_users):
                scheduled_at = time.monotonic()
                result = request_builder(origin, user, phase, 30)
                result = _annotate_timing(
                    result,
                    scheduled_at=scheduled_at,
                    enqueued_at=scheduled_at,
                    fallback_started_at=scheduled_at,
                    fallback_finished_at=scheduled_at + 0.001,
                )
                result.submission_index = index
                for attempt in result.attempts:
                    attempt.submission_index = index
                result_consumer(result)
            return [], duration_seconds

        def empty_duplicate_phase(*_args: object, **_kwargs: object) -> list[object]:
            return []

        from tools.platform_external_load import _request as production_request

        def request_with_state_fixture(
            origin: str,
            user: VirtualUser,
            **kwargs: object,
        ) -> RequestResult:
            if kwargs.get("method") == "GET":
                return RequestResult(
                    phase=str(kwargs["phase"]),
                    method="GET",
                    path=str(kwargs["path"]),
                    status=200,
                    elapsed_ms=0.1,
                    ok=True,
                    response_bytes=1,
                    response_json={"active_round": {"ready_count": 420}},
                    time_to_first_byte_ms=0.1,
                )
            return production_request(origin, user, **kwargs)

        runner_manifest = {
            "origin": "https://load-test.invalid",
            "session_cookie_name": "deadlock_platform_session",
            "csrf_cookie_name": "deadlock_platform_session_csrf",
            "marker": "preprod26082900000000ab",
            "tournaments": [
                {
                    "id": f"pair-id-{index:02d}",
                    "slug": f"pair-tournament-{index:02d}",
                    "user_count": 500,
                }
                for index in range(20)
            ],
        }
        RecordingTransportClient.instances.clear()
        with (
            patch("tools.platform_external_load.HTTP11KeepAliveClient", RecordingTransportClient),
            patch("tools.platform_external_load.run_rate_phase", fast_rate_phase),
            patch("tools.platform_external_load.run_phase", empty_duplicate_phase),
            patch("tools.platform_external_load._request", side_effect=request_with_state_fixture),
            patch(
                "tools.platform_external_load._trace",
                return_value={"status": "200", "ip": "192.0.2.10", "colo": "TEST"},
            ),
        ):
            pair_report = run_load(
                runner_manifest,
                pair_users,
                mode="ready-vote",
                spread_seconds=0,
                concurrency=128,
                timeout=30,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=5000,
                p99_budget_ms=8000,
                retry_policy=transport_pair["traffic"]["retry"],
                phase_plan=transport_pair["traffic"]["phases"],
                scenario_kind="stress",
                expected_profile_id="ready-vote-transport-pair-v1",
                expected_profile_version=1,
                expected_profile_digest=profile_contract(transport_pair)["profile_digest"],
                expected_primary_action_count=8400,
                expected_total_logical_action_count=8400,
                expected_state_read_count=20,
                max_http_attempts=25220,
            )

        self.assertEqual(
            phase_observations,
            [
                {"phase": "write_external_vote_close-a", "users": 2100, "duration": 30.0},
                {"phase": "write_external_vote_keepalive-b", "users": 2100, "duration": 30.0},
                {"phase": "write_external_vote_keepalive-c", "users": 2100, "duration": 30.0},
                {"phase": "write_external_vote_close-d", "users": 2100, "duration": 30.0},
            ],
        )
        self.assertEqual(len(RecordingTransportClient.instances), 4)
        phase_transport_counts: dict[str, dict[str, int]] = {}
        for client in RecordingTransportClient.instances:
            self.assertTrue(client.closed)
            self.assertEqual(len(client.requests), 2100)
            self.assertEqual(
                {request["method"] for request in client.requests},
                {"POST"},
            )
            self.assertEqual(
                {request["body"] for request in client.requests},
                {b'{"choice":"yes"}'},
            )
            headers = [request["headers"] for request in client.requests]
            self.assertEqual(
                {header["X-CSRF-Token"] for header in headers},
                {header["Cookie"].rsplit("=", 1)[1] for header in headers},
            )
            self.assertTrue(
                all(
                    header["Cookie"].startswith("deadlock_platform_session=session-")
                    for header in headers
                )
            )
            phase_names = {header["X-Platform-QA-Phase"] for header in headers}
            self.assertEqual(len(phase_names), 1)
            phase_name = next(iter(phase_names))
            phase_transport_counts[phase_name] = {
                "calls": len(client.requests),
                "reused": sum(request["reused"] is True for request in client.requests),
                "transport_reuse": int(client.reuse_connection),
            }
        self.assertEqual(
            phase_transport_counts,
            {
                "write_external_vote_close-a": {
                    "calls": 2100,
                    "reused": 0,
                    "transport_reuse": 0,
                },
                "write_external_vote_keepalive-b": {
                    "calls": 2100,
                    "reused": 2099,
                    "transport_reuse": 1,
                },
                "write_external_vote_keepalive-c": {
                    "calls": 2100,
                    "reused": 2099,
                    "transport_reuse": 1,
                },
                "write_external_vote_close-d": {
                    "calls": 2100,
                    "reused": 0,
                    "transport_reuse": 0,
                },
            },
        )
        self.assertEqual(pair_report["phases"]["primary"]["logical"]["actions"], 8400)
        self.assertEqual(pair_report["phases"]["state"]["requests"], 20)
        self.assertEqual(
            planned_http_attempts(
                mode="ready-vote",
                user_count=20_000,
                tournament_count=40,
                duplicate_count=0,
                manual_refresh_count=0,
                phase_plan=capacity["traffic"]["phases"],
                concurrency_stages=None,
                retry_policy=capacity["traffic"]["retry"],
            ),
            31_540,
        )

        with self.assertRaisesRegex(ExternalLoadError, "planned HTTP attempts"):
            run_load(
                {"tournaments": [{"id": "tournament-1"}]},
                [object()] * 10,
                mode="ready-vote",
                spread_seconds=0,
                concurrency=4,
                timeout=1,
                duplicate_count=2,
                manual_refresh_count=0,
                p95_budget_ms=100,
                p99_budget_ms=200,
                retry_policy={"max_retries": 2},
                max_http_attempts=1,
            )

    def test_dispatchability_matches_active_registry_and_workflow_choices(self) -> None:
        profiles = load_profiles()
        for profile in profiles.values():
            with self.subTest(profile=profile["profile_id"]):
                self.assertEqual(profile["portfolio"]["status"], "active")
                self.assertNotEqual(profile["mode"], "tournament-lifecycle")
                ensure_dispatchable(profile)

        retained_root = Path(__file__).resolve().parents[1] / "performance" / "profiles" / "retained-v1"
        for profile_id in (
            "ready-vote-saturation-ramp-v1",
            "ready-vote-saturation-ramp-v2",
            "ready-vote-saturation-ramp-v3",
        ):
            self.assertNotIn(profile_id, profiles)
            self.assertTrue((retained_root / f"{profile_id}.json").is_file())
        for profile_id in (
            "tournament-lifecycle-slo-v1",
            "tournament-lifecycle-scale-v1",
            "tournament-lifecycle-capacity-v1",
        ):
            self.assertNotIn(profile_id, profiles)
            self.assertFalse((Path(__file__).resolve().parents[1] / "performance" / "profiles" / f"{profile_id}.json").exists())

        workflow = (
            Path(__file__).resolve().parents[2]
            / ".github"
            / "workflows"
            / "platform-production-external-load.yml"
        ).read_text(encoding="utf-8")
        options_match = re.search(
            r"profile_id:\n(?P<options>.*?)(?:\n\npermissions:)",
            workflow,
            re.DOTALL,
        )
        self.assertIsNotNone(options_match)
        assert options_match is not None
        options = set(
            re.findall(
                r"^\s+-\s+([a-z0-9-]+-v[0-9]+)\s*$",
                options_match.group("options"),
                re.MULTILINE,
            )
        )
        self.assertEqual(options, set(profiles))

    def test_portfolio_semantic_matrix_rejects_unowned_combinations(self) -> None:
        profile = get_profile("ready-vote-slo-v2")
        cases = (
            {"class": "default", "status": "deprecated", "cadence": "release"},
            {"class": "default", "status": "active", "cadence": "on-demand"},
            {"class": "diagnostic", "status": "deprecated", "cadence": "on-demand"},
        )
        for replacement in cases:
            invalid = deepcopy(profile)
            invalid["portfolio"].update(replacement)
            with self.subTest(replacement=replacement), self.assertRaises(LoadProfileError):
                validate_profile(invalid)

        invalid = deepcopy(profile)
        invalid["portfolio"]["environment"] = "qa-preprod"
        with self.assertRaises(LoadProfileError):
            validate_profile(invalid)

    def test_external_error_is_a_structured_failed_report(self) -> None:
        profile = get_profile("ready-vote-slo-v2")
        contract = profile_contract(profile)

        def failed_runner(**_kwargs: object) -> None:
            raise ExternalLoadError("fixture contract failed")

        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "failed.json"
            result = _run_external_load_or_report(
                failed_runner,
                profile=profile,
                contract=contract,
                report_path=report_path,
                error_type=ExternalLoadError,
            )
            payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertIsNone(result)
        self.assertFalse(payload["acceptance"]["passed"])
        self.assertEqual(payload["acceptance"]["decision"], "LOAD RUN FAILED")

    def test_evaluate_report_requires_current_profile_contract_and_run_binding(self) -> None:
        profile = get_profile("ready-vote-slo-v2")
        mutations = {
            "stale_profile": lambda report: report.update(
                {"profile_id": "ready-vote-slo-v1"}
            ),
            "wrong_version": lambda report: report.update({"profile_version": 1}),
            "wrong_digest": lambda report: report.update({"profile_digest": "0" * 64}),
            "wrong_source": lambda report: report.update({"source_git_sha": "b" * 40}),
            "wrong_mode": lambda report: report.update({"mode": "read-mix"}),
            "wrong_environment": lambda report: report.update(
                {"environment": "qa-preprod"}
            ),
            "wrong_contract_schema": lambda report: report["load_contract"].update(
                {"schema": 1}
            ),
            "wrong_run": lambda report: report.update({"external_run_id": "999"}),
            "wrong_raw_population": lambda report: report["raw_http"].update(
                {"requests": 0}
            ),
            "wrong_overall_scope_shape": lambda report: report["overall"].update(
                {"actions": report["overall"]["requests"]}
            ),
            "non_authoritative": lambda report: report.update(
                {"authoritative": False, "dispatchable": False}
            ),
            "legacy_contract_override": lambda report: (
                report.update({"contract": {"ok": True}}),
                report["acceptance"].update({"contract_ok": False}),
            ),
        }

        with patch.dict(
            os.environ,
            {"SOURCE_GIT_SHA": "a" * 40, "GITHUB_RUN_ID": "123"},
            clear=False,
        ):
            for name, mutate in mutations.items():
                with self.subTest(binding=name), tempfile.TemporaryDirectory() as directory:
                    report_path = Path(directory) / "report.json"
                    report = self._bound_report(profile)
                    mutate(report)
                    report_path.write_text(
                        json.dumps(report),
                        encoding="utf-8",
                    )

                    result = evaluate_report(profile, report_path, None)
                    evaluated = json.loads(report_path.read_text(encoding="utf-8"))

                self.assertEqual(result, 1)
                self.assertFalse(evaluated["report_binding"]["complete"])
                self.assertEqual(
                    evaluated["acceptance"]["decision"],
                    "LOAD REPORT BINDING FAIL",
                )

    def test_evaluate_report_accepts_only_a_fully_bound_current_contract(self) -> None:
        profile = get_profile("ready-vote-slo-v2")
        with (
            patch.dict(
                os.environ,
                {"SOURCE_GIT_SHA": "a" * 40, "GITHUB_RUN_ID": "123"},
                clear=False,
            ),
            tempfile.TemporaryDirectory() as directory,
        ):
            report_path = Path(directory) / "report.json"
            report_path.write_text(
                json.dumps(self._bound_report(profile)),
                encoding="utf-8",
            )
            result = evaluate_report(profile, report_path, None)
            evaluated = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertTrue(evaluated["report_binding"]["complete"])
        self.assertNotEqual(
            evaluated["acceptance"]["decision"],
            "LOAD REPORT BINDING FAIL",
        )

    def test_evaluate_report_rejects_runtime_budget_field_mismatch(self) -> None:
        profile = get_profile("ready-vote-slo-v2")
        report = self._bound_report(profile)
        report["load_contract"]["runtime_http_budget"]["actual_http_attempts"] -= 1

        with (
            patch.dict(
                os.environ,
                {"SOURCE_GIT_SHA": "a" * 40, "GITHUB_RUN_ID": "123"},
                clear=False,
            ),
            tempfile.TemporaryDirectory() as directory,
        ):
            report_path = Path(directory) / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            result = evaluate_report(profile, report_path, None)
            evaluated = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertFalse(evaluated["report_binding"]["complete"])
        self.assertFalse(evaluated["report_binding"]["checks"]["runtime_budget"])
        self.assertEqual(
            evaluated["acceptance"]["decision"],
            "LOAD REPORT BINDING FAIL",
        )

    def test_profile_cli_rejects_duplicate_json_and_nonfinite_budgets(self) -> None:
        profile = get_profile("ready-vote-slo-v2")
        for field, value in (
            ("spread_seconds", -1),
            ("spread_seconds", float("nan")),
            ("spread_seconds", float("inf")),
            ("max_shed_percent", -1),
            ("max_shed_percent", float("nan")),
        ):
            invalid = deepcopy(profile)
            if field == "max_shed_percent":
                invalid["acceptance"][field] = value
            else:
                invalid["traffic"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(LoadProfileError):
                validate_profile(invalid)

        with tempfile.TemporaryDirectory() as directory:
            duplicate_path = Path(directory) / "duplicate.json"
            encoded = json.dumps(profile, indent=2)
            encoded = encoded.replace('"schema": 2,', '"schema": 2,\n  "schema": 2,', 1)
            duplicate_path.write_text(encoded, encoding="utf-8")
            with patch("tools.platform_load.PROFILE_ROOT", Path(directory)):
                with self.assertRaises(LoadProfileError):
                    platform_load_main(["validate"])

            duplicate_report_path = Path(directory) / "duplicate-report.json"
            duplicate_report_path.write_text(
                '{"schema": 1, "schema": 1}',
                encoding="utf-8",
            )
            with self.assertRaises(LoadProfileError):
                platform_load_main(
                    [
                        "evaluate",
                        "--profile",
                        "ready-vote-slo-v2",
                        "--report",
                        str(duplicate_report_path),
                    ]
                )

    def test_canonical_run_forwards_exact_observer_requirement_to_runner(self) -> None:
        # Keep this contract-forwarding test bounded while preserving the
        # selected profile's exact-plan preflight: the one-user fake manifest
        # must be the plan the runner receives.
        profile = deepcopy(get_profile("read-mix-human-v2"))
        profile["fixture"]["users_per_tournament"] = 1
        profile["fixture"]["max_total_users"] = 1
        profile["traffic"]["spread_seconds"] = 0
        manifest = {
            "origin": "https://old-sparky.com",
            "session_cookie_name": "deadlock_platform_session",
            "csrf_cookie_name": "deadlock_platform_session_csrf",
            "marker": "preprod26082900000000ab",
            "tournaments": [
                {"id": "tournament-1", "slug": "qa-tournament", "user_count": 1}
            ],
        }
        users = [
            VirtualUser(
                user_id="user-00000001",
                tournament_slug="qa-tournament",
                session_token="s" * 64,
                csrf_token="c" * 64,
            )
        ]

        def fake_request(origin: str, user: VirtualUser, **kwargs: object) -> RequestResult:
            del origin, user
            return RequestResult(
                phase=str(kwargs["phase"]),
                method=str(kwargs["method"]),
                path=str(kwargs["path"]),
                status=200,
                elapsed_ms=10.0,
                ok=True,
                response_bytes=1,
                response_etag='"workspace-etag"',
            )

        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "canonical-report.json"
            with (
                patch("tools.platform_external_load.load_manifest", return_value=(manifest, users)),
                patch(
                    "tools.platform_external_load._trace",
                    return_value={"status": "200", "ip": "192.0.2.10", "colo": "TEST"},
                ),
                patch("tools.platform_external_load._request", side_effect=fake_request),
                patch("tools.platform_load._source_git_sha", return_value="a" * 40),
                patch.dict(
                    os.environ,
                    {"SOURCE_GIT_SHA": "a" * 40, "GITHUB_RUN_ID": "26082900000000"},
                ),
            ):
                result = run_profile_worker(
                    profile,
                    manifest_path=Path(directory) / "manifest.json",
                    report_path=report_path,
                )
            payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertFalse(payload["acceptance"]["passed"])
        self.assertTrue(payload["acceptance"]["pending_origin_evidence"])
        self.assertFalse(payload["acceptance"]["observer_binding"]["complete"])
        self.assertTrue(payload["acceptance"]["timing_evidence"]["complete"])
        # The human read profile has one ordinary-concurrency population, not
        # an authored capacity ramp.  Its canonical report must not be made
        # permanently ineligible by a synthetic fallback stage requirement.
        self.assertTrue(payload["acceptance"]["checks"]["capacity_ramp_evidence"])

        for environment in (
            {"GITHUB_RUN_ID": "123"},
            {"SOURCE_GIT_SHA": "a" * 40},
        ):
            with tempfile.TemporaryDirectory() as directory:
                report_path = Path(directory) / "missing-binding-report.json"
                with patch.dict(os.environ, environment, clear=True):
                    missing_result = run_profile_worker(
                        profile,
                        manifest_path=Path(directory) / "manifest.json",
                        report_path=report_path,
                    )
                missing_payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(missing_result, 1)
            self.assertEqual(
                missing_payload["acceptance"]["decision"],
                "LOAD REPORT BINDING FAIL",
            )
            self.assertEqual(missing_payload["acceptance"]["error_class"], "other")

        # Exercise the complete canonical path with the smallest possible
        # fixture for each supported producer shape.  The observer is bound
        # to the exact marker/run emitted by the producer; only evaluation can
        # turn the runner's pending-origin report into PASS.
        def evaluate_hermetic_profile(profile_id: str) -> dict[str, object]:
            scenario = deepcopy(get_profile(profile_id))
            scenario["fixture"]["tournament_count"] = 1
            scenario["fixture"]["users_per_tournament"] = 1
            scenario["fixture"]["max_total_users"] = 1
            scenario["traffic"]["spread_seconds"] = 0
            if scenario["mode"] == "ready-vote":
                scenario["traffic"]["duplicate_count"] = 0
            if (
                scenario["mode"] == "read-mix"
                and scenario["traffic"].get("concurrency_stages")
            ):
                # Keep the one-user hermetic fixture within the selected
                # profile's exact primary population; the production profile
                # refresh cohort is intentionally much larger.
                scenario["traffic"]["manual_refresh_count"] = 0
            marker = manifest["marker"]
            external_run_id = "26082900000000"

            def bound_request(
                origin: str,
                user: VirtualUser,
                **kwargs: object,
            ) -> RequestResult:
                del origin, user
                phase = str(kwargs.get("phase") or "")
                response_json = (
                    {"active_round": {"ready_count": 1}}
                    if phase == "read_external_vote_state"
                    else {"changed": True}
                    if scenario["mode"] == "ready-vote"
                    else None
                )
                return RequestResult(
                    phase=phase,
                    method=str(kwargs.get("method") or "GET"),
                    path=str(kwargs.get("path") or "/"),
                    status=200,
                    elapsed_ms=10.0,
                    ok=True,
                    response_bytes=128,
                    response_etag='"workspace-etag"',
                    response_json=response_json,
                )

            observer = {
                "schema": 1,
                "stop_file_seen": True,
                "timed_out": False,
                "binding": {
                    "complete": True,
                    "fixture_marker": marker,
                    "external_run_id": external_run_id,
                },
                "system": {
                    "cpu_per_core": {"cpu0": {"max_percent": 0}},
                    "postgres_backend_connections": {"max": 0},
                    "postgres_waits": {
                        "max_waiting_backends": 0,
                        "max_lock_waiters": 0,
                    },
                },
                "server_request_perf_logs": {
                    "pool_checkout_wait_ms": {"p95_ms": 0, "p99_ms": 0}
                },
            }
            with tempfile.TemporaryDirectory() as directory:
                report_path = Path(directory) / "hermetic-report.json"
                observer_path = Path(directory) / "observer.json"
                observer_path.write_text(json.dumps(observer), encoding="utf-8")
                with (
                    patch(
                        "tools.platform_external_load.load_manifest",
                        return_value=(manifest, users),
                    ),
                    patch(
                        "tools.platform_external_load._trace",
                        return_value={
                            "status": "200",
                            "ip": "192.0.2.10",
                            "colo": "TEST",
                        },
                    ),
                    patch(
                        "tools.platform_external_load._request",
                        side_effect=bound_request,
                    ),
                    patch.dict(
                        os.environ,
                        {
                            "SOURCE_GIT_SHA": "a" * 40,
                            "GITHUB_RUN_ID": external_run_id,
                        },
                    ),
                ):
                    run_result = run_profile_worker(
                        scenario,
                        manifest_path=Path(directory) / "manifest.json",
                        report_path=report_path,
                    )
                    pending = json.loads(report_path.read_text(encoding="utf-8"))
                    evaluate_result = evaluate_report(
                        scenario,
                        report_path,
                        observer_path,
                    )
                    evaluated = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(run_result, 1)
            self.assertFalse(pending["acceptance"]["passed"])
            self.assertEqual(evaluate_result, 0)
            self.assertTrue(evaluated["acceptance"]["passed"])
            self.assertTrue(evaluated["report_binding"]["complete"])
            return evaluated

        for profile_id, expected_phases in (
            ("ready-vote-slo-v2", []),
            ("read-mix-human-v2", ["read_mix"]),
            ("read-mix-concurrency-ramp-v1", ["read_mix"]),
            ("authenticated-page-load-v1", ["authenticated_page_load"]),
        ):
            with self.subTest(hermetic_profile=profile_id):
                evaluated = evaluate_hermetic_profile(profile_id)
                self.assertEqual(
                    evaluated["acceptance"]["phase_plan_evidence"]["expected_phases"],
                    expected_phases,
                )
                if profile_id == "read-mix-concurrency-ramp-v1":
                    self.assertEqual(
                        evaluated["acceptance"]["phase_plan_evidence"]["actual_phases"],
                        ["capacity_ramp", "read_mix"],
                    )
                    self.assertTrue(
                        evaluated["acceptance"]["checks"]["capacity_ramp_evidence"]
                    )

    def test_partial_submission_is_not_presented_as_complete_work(self) -> None:
        result = RequestResult(
            phase="partial",
            method="GET",
            path="/health",
            status=200,
            elapsed_ms=10.0,
            ok=True,
            response_bytes=1,
            started_at_monotonic=1.0,
            finished_at_monotonic=1.01,
            scheduled_at_monotonic=1.0,
            enqueued_at_monotonic=1.0,
        )
        summary = summarize_results([result], expected_count=2, submitted_count=1)
        self.assertTrue(summary["timing"]["partial"])
        self.assertEqual(summary["timing"]["dropped_work"], 1)

    def test_observer_profiles_are_pid_bound_and_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            bound = output_dir / "ready-vote-cprofile-123-456.pstats"
            unbound = output_dir / "ready-vote-cprofile-999-456.pstats"
            bound.write_bytes(b"profile")
            unbound.write_bytes(b"profile")

            class FakeStats:
                stats: dict[object, object] = {}

                def __init__(self, *_args: object) -> None:
                    pass

            identities = {123: {"pid": 123, "start_time_ticks": 456}}
            with patch("tools.platform_external_load_observer.Stats", FakeStats):
                summary = cpu_profile_summary(output_dir, armed_identities=identities)

            self.assertEqual(len(summary["profiles"]), 1)
            self.assertEqual(summary["retention"]["ignored_unbound_profiles"], 1)
            self.assertTrue(bound.exists())
            self.assertTrue(unbound.exists())

    def test_observer_profiles_require_changed_exact_generation_and_parse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            profile = output_dir / "ready-vote-cprofile-123-456.pstats"
            wrong_generation = output_dir / "ready-vote-cprofile-123-999.pstats"
            partial = output_dir / "ready-vote-cprofile-124-457.pstats"
            corrupt = output_dir / "ready-vote-cprofile-125-458.pstats"
            invalid_marshal = output_dir / "ready-vote-cprofile-126-459.pstats"
            wrong_shape = output_dir / "ready-vote-cprofile-127-460.pstats"
            producer = cProfile.Profile()
            producer.enable()
            sum(range(1000))
            producer.disable()
            valid_profile = output_dir / "valid.pstats"
            producer.dump_stats(str(valid_profile))
            profile.write_bytes(valid_profile.read_bytes())
            wrong_generation.write_bytes(b"wrong")
            invalid_fixtures = {
                partial: profile.read_bytes()[: max(1, profile.stat().st_size // 2)],
                # A real marshal payload with the wrong top-level shape makes
                # pstats access `.items` on ellipsis and raise AttributeError.
                corrupt: marshal.dumps(Ellipsis),
                invalid_marshal: b"not-marshal",
                wrong_shape: marshal.dumps([]),
            }
            for path, contents in invalid_fixtures.items():
                path.write_bytes(contents)
            baseline = profile_artifact_snapshot(output_dir)

            identities = {123: {"pid": 123, "start_time_ticks": 456}}
            stale = cpu_profile_summary(
                output_dir,
                armed_identities=identities,
                baseline_artifacts=baseline,
            )
            self.assertEqual(len(stale["profiles"]), 0)
            self.assertEqual(stale["retention"]["ignored_stale_profiles"], 1)

            profile_mtime = int(baseline[profile.name][3]) + 1
            partial_mtime = int(baseline[partial.name][3]) + 1
            os.utime(profile, ns=(profile_mtime, profile_mtime))
            os.utime(partial, ns=(partial_mtime, partial_mtime))
            corrupt_mtime = int(baseline[corrupt.name][3]) + 1
            os.utime(corrupt, ns=(corrupt_mtime, corrupt_mtime))
            for path in (invalid_marshal, wrong_shape):
                mtime = int(baseline[path.name][3]) + 1
                os.utime(path, ns=(mtime, mtime))
            fresh = cpu_profile_summary(
                output_dir,
                armed_identities={
                    123: {"pid": 123, "start_time_ticks": 456},
                    124: {"pid": 124, "start_time_ticks": 457},
                    125: {"pid": 125, "start_time_ticks": 458},
                    126: {"pid": 126, "start_time_ticks": 459},
                    127: {"pid": 127, "start_time_ticks": 460},
                },
                baseline_artifacts=baseline,
            )

            self.assertEqual(len(fresh["profiles"]), 1)
            self.assertEqual(fresh["retention"]["invalid_profiles"], 4)
            self.assertTrue(profile.exists())
            self.assertTrue(wrong_generation.exists())

    def test_observer_signals_only_direct_workers_and_never_master(self) -> None:
        command = (
            "/opt/venv/bin/python -m gunicorn "
            "apps.platform_api.app.main:app --workers 2"
        )
        processes = [
            {
                "pid": 100,
                "ppid": 1,
                "uid": 994,
                "comm": "python",
                "cmdline": command,
                "start_time_ticks": 10,
            },
            {
                "pid": 101,
                "ppid": 100,
                "uid": 994,
                "comm": "python",
                "cmdline": command,
                "start_time_ticks": 11,
            },
            {
                "pid": 102,
                "ppid": 100,
                "uid": 994,
                "comm": "python",
                "cmdline": command,
                "start_time_ticks": 12,
            },
            # An API-shaped foreign process with no direct worker children is
            # not a master candidate and must not receive profiling signals.
            {
                "pid": 200,
                "ppid": 1,
                "uid": 994,
                "comm": "python",
                "cmdline": command,
                "start_time_ticks": 20,
            },
            # A foreign UID cannot be selected even when it claims the API
            # command line and the active master's parent relationship.
            {
                "pid": 103,
                "ppid": 100,
                "uid": 995,
                "comm": "python",
                "cmdline": command,
                "start_time_ticks": 13,
            },
        ]

        records = {process["pid"]: process for process in processes}

        def process_reader(pid: int) -> dict[str, object] | None:
            return records.get(pid)  # type: ignore[arg-type]

        pidfds: list[int] = []
        sent: list[tuple[int, signal.Signals]] = []

        def pidfd_open(pid: int, _flags: int) -> int:
            pidfds.append(pid)
            return pid + 1000

        def pidfd_send_signal(pidfd: int, signum: signal.Signals) -> None:
            sent.append((pidfd, signum))

        with patch("tools.platform_external_load_observer.os.pidfd_open", pidfd_open), patch(
            "tools.platform_external_load_observer.signal.pidfd_send_signal",
            pidfd_send_signal,
        ), patch("tools.platform_external_load_observer.os.kill") as kill:
            signalled = signal_api_workers(
                signal.SIGUSR1,
                processes=processes,
                expected_uid=994,
                process_reader=process_reader,
            )

        self.assertEqual(signalled, [101, 102])
        self.assertEqual(pidfds, [101, 102])
        self.assertEqual(sent, [(1101, signal.SIGUSR1), (1102, signal.SIGUSR1)])
        kill.assert_not_called()

    def test_observer_rejects_signal_when_pidfd_is_unavailable(self) -> None:
        command = "/opt/venv/bin/python -m gunicorn apps.platform_api.app.main:app --workers 1"
        processes = [
            {"pid": 100, "ppid": 1, "uid": 994, "cmdline": command, "start_time_ticks": 10},
            {"pid": 101, "ppid": 100, "uid": 994, "cmdline": command, "start_time_ticks": 11},
        ]
        records = {process["pid"]: process for process in processes}

        with patch("tools.platform_external_load_observer.os.pidfd_open", None), patch(
            "tools.platform_external_load_observer.signal.pidfd_send_signal", None
        ), patch("tools.platform_external_load_observer.os.kill") as kill:
            signalled, reasons, availability_reason = _signal_api_workers_detailed(
                signal.SIGUSR1,
                processes=processes,
                expected_uid=994,
                process_reader=lambda pid: records.get(pid),  # type: ignore[arg-type]
            )

        self.assertEqual(signalled, [])
        self.assertEqual(reasons, {"pidfd_unavailable": 1})
        self.assertIsNone(availability_reason)
        self.assertEqual(len(signalled) + sum(reasons.values()), 1)
        kill.assert_not_called()

        with patch(
            "tools.platform_external_load_observer.expected_api_uid",
            return_value=None,
        ):
            signalled, reasons, availability_reason = _signal_api_workers_detailed(
                signal.SIGUSR1,
                processes=processes,
                expected_uid=None,
                process_reader=lambda pid: records.get(pid),  # type: ignore[arg-type]
            )
        self.assertEqual(signalled, [])
        self.assertEqual(reasons, {})
        self.assertEqual(availability_reason, "uid_unavailable")

        with patch(
            "tools.platform_external_load_observer.expected_api_uid",
            return_value=None,
        ):
            signalled, reasons, availability_reason = _signal_api_workers_detailed(
                signal.SIGUSR2,
                processes=processes,
                expected_uid=None,
                armed_identities={
                    101: {"pid": 101, "uid": 994, "ppid": 100, "start_time_ticks": 11},
                    102: {"pid": 102, "uid": 994, "ppid": 100, "start_time_ticks": 12},
                },
                process_reader=lambda pid: records.get(pid),  # type: ignore[arg-type]
            )
        self.assertEqual(signalled, [])
        self.assertEqual(reasons, {"uid_unavailable": 2})
        self.assertIsNone(availability_reason)

        initial_arm = _signal_delivery_summary(
            [],
            {},
            requested=2,
            availability_reason="uid_unavailable",
        )
        self.assertEqual(initial_arm["requested_count"], 2)
        self.assertEqual(initial_arm["delivered_count"], 0)
        self.assertEqual(initial_arm["rejected_count"], 2)
        self.assertEqual(initial_arm["rejection_reasons"], {"uid_unavailable": 2})
        self.assertNotIn("availability_reason", initial_arm)

        flush_after_prior_arm = _signal_delivery_summary(
            [],
            {},
            requested=2,
            availability_reason="uid_unavailable",
        )
        self.assertEqual(flush_after_prior_arm["requested_count"], 2)
        self.assertEqual(flush_after_prior_arm["rejected_count"], 2)
        self.assertEqual(flush_after_prior_arm["rejection_reasons"], {"uid_unavailable": 2})
        self.assertNotIn("availability_reason", flush_after_prior_arm)

        flush_after_no_candidates = _signal_delivery_summary(
            [],
            {},
            requested=0,
            availability_reason="uid_unavailable",
        )
        self.assertEqual(flush_after_no_candidates["requested_count"], 0)
        self.assertEqual(flush_after_no_candidates["rejected_count"], 0)
        self.assertEqual(flush_after_no_candidates["availability_reason"], "uid_unavailable")

        overcounted = _signal_delivery_summary(
            [101],
            {"worker_missing": 3},
            requested=2,
        )
        self.assertEqual(overcounted["delivered_count"] + overcounted["rejected_count"], 2)
        self.assertEqual(overcounted["rejection_reasons"], {"worker_missing": 1})

    def test_observer_flush_rejects_stale_or_reused_worker_identity(self) -> None:
        command = (
            "/opt/venv/bin/python -m gunicorn "
            "apps.platform_api.app.main:app --workers 2"
        )
        armed_processes = [
            {
                "pid": 100,
                "ppid": 1,
                "uid": 994,
                "comm": "python",
                "cmdline": command,
                "start_time_ticks": 10,
            },
            {
                "pid": 101,
                "ppid": 100,
                "uid": 994,
                "comm": "python",
                "cmdline": command,
                "start_time_ticks": 11,
            },
            {
                "pid": 102,
                "ppid": 100,
                "uid": 994,
                "comm": "python",
                "cmdline": command,
                "start_time_ticks": 12,
            },
        ]
        armed = api_worker_identities(armed_processes, expected_uid=994)
        reused_processes = [dict(process) for process in armed_processes]
        reused_processes[1]["start_time_ticks"] = 999

        records = {process["pid"]: process for process in reused_processes}

        def process_reader(pid: int) -> dict[str, object] | None:
            return records.get(pid)  # type: ignore[arg-type]

        with patch("tools.platform_external_load_observer.os.pidfd_open", side_effect=lambda pid, _flags: pid + 1000), patch(
            "tools.platform_external_load_observer.signal.pidfd_send_signal"
        ) as pidfd_send, patch("tools.platform_external_load_observer.os.kill") as kill:
            signalled, reasons, availability_reason = _signal_api_workers_detailed(
                signal.SIGUSR2,
                processes=reused_processes,
                expected_uid=994,
                armed_identities=armed,
                process_reader=process_reader,
            )

        self.assertEqual(signalled, [102])
        self.assertEqual(reasons, {"identity_mismatch": 1})
        self.assertIsNone(availability_reason)
        self.assertEqual(len(signalled) + sum(reasons.values()), len(armed))
        pidfd_send.assert_called_once_with(1102, signal.SIGUSR2)
        kill.assert_not_called()

        missing_processes = [process for process in reused_processes if process["pid"] != 101]
        missing_records = {process["pid"]: process for process in missing_processes}
        with patch(
            "tools.platform_external_load_observer.os.pidfd_open",
            side_effect=lambda pid, _flags: pid + 1000,
        ), patch("tools.platform_external_load_observer.signal.pidfd_send_signal") as pidfd_send:
            signalled, reasons, availability_reason = _signal_api_workers_detailed(
                signal.SIGUSR2,
                processes=missing_processes,
                expected_uid=994,
                armed_identities=armed,
                process_reader=lambda pid: missing_records.get(pid),  # type: ignore[arg-type]
            )
        self.assertEqual(signalled, [102])
        self.assertEqual(reasons, {"worker_missing": 1})
        self.assertIsNone(availability_reason)
        self.assertEqual(len(signalled) + sum(reasons.values()), len(armed))
        pidfd_send.assert_called_once_with(1102, signal.SIGUSR2)

    def test_supervisor_binds_observer_and_workflow_uses_current_profiles(self) -> None:
        root = Path(__file__).resolve().parents[1]
        supervisor = (root / "tools" / "platform_production_external_fixture_qa.sh").read_text(
            encoding="utf-8"
        )
        observer = (root / "tools" / "platform_external_load_observer.py").read_text(
            encoding="utf-8"
        )
        workflow = (root.parent / ".github" / "workflows" / "platform-production-external-load.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("re.fullmatch(r\"preprod[0-9]{12}[0-9a-f]{4}\", marker)", supervisor)
        self.assertIn('--fixture-marker "$fixture_marker"', supervisor)
        self.assertIn('--external-run-id "$run_id"', supervisor)
        self.assertIn('--run-attempt "$run_attempt"', supervisor)
        self.assertIn('--progress-path "$external_vote_progress"', supervisor)
        self.assertIn('record_progress "observer_reaped"', supervisor)
        self.assertIn('record_progress "supervisor_exit"', supervisor)
        self.assertIn('record_progress "fixture_setup_requested"', supervisor)
        self.assertIn('external_vote_timing="$external_vote_root/fixture-timing.json"', supervisor)
        timing_projection = supervisor.index("--project-fixture-timing \"$external_vote_timing\"")
        summary_projection = supervisor.index('"$summary_path" "$safe_matrix_summary" "$qa_status"')
        summary_export = supervisor.index('"$safe_matrix_summary" "$export_dir/matrix-summary.json"')
        self.assertLess(timing_projection, summary_projection)
        self.assertLess(summary_projection, summary_export)
        self.assertNotIn('record_progress "fixture_preflight"', supervisor)
        self.assertNotIn('record_progress "fixture_creation"', supervisor)
        self.assertIn('"authoritative": False', observer)
        self.assertIn('"final_credit": False', observer)
        self.assertLess(supervisor.index('--fixture-marker "$fixture_marker"'), supervisor.index(': > "$external_vote_ready"'))
        self.assertIn("pidfd_send_signal", observer)
        self.assertNotIn("os.kill(pid, signum)", observer)
        self.assertIn("platform_load.py validate", workflow)
        self.assertIn('--profile "$PROFILE_ID" --dispatchable', workflow)

        marker_helper = re.search(
            r"(?ms)^completion_marker_was_present\(\) \{\n.*?^\}",
            supervisor,
        )
        self.assertIsNotNone(marker_helper)
        helper = marker_helper.group(0)  # type: ignore[union-attr]
        state_capture = supervisor.index(
            'if completion_marker_was_present "$external_vote_complete"; then'
        )
        forced_marker = supervisor.index(': > "$external_vote_complete"', state_capture)
        observer_reaped = supervisor.index(
            'record_progress "observer_reaped"', forced_marker
        )
        self.assertLess(state_capture, forced_marker)
        self.assertLess(forced_marker, observer_reaped)

        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "complete"

            def marker_state() -> str:
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        helper
                        + '\ncompletion_marker_was_present "$1" && printf true || printf false',
                        "marker-state-test",
                        str(marker),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                return result.stdout

            self.assertEqual(marker_state(), "false")
            marker.write_text("", encoding="ascii")
            self.assertEqual(marker_state(), "true")
            marker.unlink()
            target = Path(directory) / "target"
            target.write_text("", encoding="ascii")
            marker.symlink_to(target.name)
            self.assertEqual(marker_state(), "false")

    def test_observer_progress_receipt_is_private_bound_monotonic_and_non_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / OBSERVER_PROGRESS_NAME
            receipt = ObserverProgressReceipt(
                path,
                source_sha="a" * 40,
                run_id="123456",
                run_attempt="2",
            )
            receipt.emit("fixture_setup_requested", stop_marker_seen=False)
            first = json.loads(path.read_text(encoding="ascii"))
            self.assertEqual(first["sequence"], 1)
            self.assertEqual(first["elapsed_ms"], 0)
            self.assertEqual(first["source_sha"], "a" * 40)
            self.assertEqual(first["run_id"], "123456")
            self.assertEqual(first["run_attempt"], "2")
            self.assertEqual(first["profile_id"], "external-vote")
            self.assertFalse(first["authoritative"])
            self.assertFalse(first["dispatchable"])
            self.assertFalse(first["final_credit"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            receipt.emit("observer_startup", stop_marker_seen=False)
            second = json.loads(path.read_text(encoding="ascii"))
            self.assertEqual(second["sequence"], 2)
            self.assertGreaterEqual(second["elapsed_ms"], first["elapsed_ms"])
            self.assertEqual(second["started_monotonic_ms"], first["started_monotonic_ms"])

            with self.assertRaises(ValueError):
                receipt.emit("fixture_setup_requested", stop_marker_seen=False)
            receipt.emit("stop_marker_seen", stop_marker_seen=True)
            receipt.emit("sampler_stop", stop_marker_seen=True)
            with self.assertRaises(ValueError):
                receipt.emit(
                    "observer_reaped",
                    stop_marker_seen=True,
                    observer_exit_code=2,
                )
            receipt.emit(
                "observer_reaped",
                stop_marker_seen=True,
                observer_exit_code=2,
                observer_command_reaped=True,
                observer_output_closed=True,
            )
            final = json.loads(path.read_text(encoding="ascii"))
            self.assertEqual(final["phase"], "observer_reaped")
            self.assertEqual(final["observer_exit_code"], 2)
            self.assertTrue(final["observer_command_reaped"])
            self.assertTrue(final["observer_output_closed"])
            self.assertFalse(final["final_credit"])

            other_binding = ObserverProgressReceipt(
                path,
                source_sha="b" * 40,
                run_id="123456",
                run_attempt="2",
            )
            with self.assertRaises(ValueError):
                other_binding.emit("export_summary", stop_marker_seen=True)

            path.unlink()
            target = Path(directory) / "external-target"
            target.write_text("not a progress receipt", encoding="ascii")
            path.symlink_to(target.name)
            with self.assertRaises(OSError):
                receipt.emit("export_summary", stop_marker_seen=True)

    def test_external_workflow_requires_and_binds_observer_before_evaluation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root.parent / ".github" / "workflows" / "platform-production-external-load.yml").read_text(
            encoding="utf-8"
        )

        def job(name: str) -> str:
            match = re.search(
                rf"^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
                workflow,
                re.MULTILINE | re.DOTALL,
            )
            self.assertIsNotNone(match, name)
            assert match is not None
            return match.group("body")

        candidate_jobs = {
            name: job(name)
            for name in ("validate-external-inputs", "load-client", "evaluate-load")
        }
        for name, body in candidate_jobs.items():
            self.assertNotRegex(body, r"\bPROD_SSH_(?:HOST|USER|KEY)\b", name)
            self.assertNotIn("secrets.", body, name)
        checkout = candidate_jobs["load-client"]
        self.assertRegex(
            checkout,
            re.compile(r"^[ \t]+persist-credentials:[ \t]*false[ \t]*$", re.MULTILINE),
        )

        expected_secret_scopes = {
            "Validate explicit external production load": set(),
            "Prepare external fixture with ephemeral SSH": {
                "PROD_SSH_HOST",
                "PROD_SSH_USER",
                "PROD_SSH_KEY",
            },
            "Signal fixture completion and collect origin evidence": {
                "PROD_SSH_HOST",
                "PROD_SSH_USER",
                "PROD_SSH_KEY",
            },
            "Run checked-out external HTTP load client": set(),
            "Evaluate checked-out load report": set(),
            "Exact cleanup of external fixture": {
                "PROD_SSH_HOST",
                "PROD_SSH_USER",
                "PROD_SSH_KEY",
            },
        }
        for name, expected in expected_secret_scopes.items():
            body = next(
                job(job_name)
                for job_name in ("validate-external-inputs", "fixture-setup", "load-client", "fixture-finalize", "evaluate-load")
                if name in job(job_name)
            )
            actual = set(re.findall(r"\bPROD_SSH_(?:HOST|USER|KEY)\b", body))
            self.assertEqual(actual, expected, name)

        finalizer = job("fixture-finalize")
        evaluator = job("evaluate-load")
        self.assertIn("if: ${{ always()", finalizer)
        self.assertIn("if: ${{ always()", evaluator)
        self.assertIn("observer", finalizer)
        self.assertIn("server-observability.json", finalizer)
        self.assertIn("server-observability.json", evaluator)
        self.assertIn('needs.fixture-finalize.outputs.observer_ready', evaluator)
        self.assertIn('needs.fixture-finalize.outputs.cleanup_status', evaluator)
        self.assertLess(
            workflow.index("Download fixed origin evidence"),
            workflow.index("platform/tools/platform_load.py evaluate"),
        )

    def test_retained_historical_class_cannot_become_runnable(self) -> None:
        profile = deepcopy(next(iter(load_profiles().values())))
        profile["portfolio"]["class"] = "historical"

        with self.assertRaises(LoadProfileError):
            validate_profile(profile)


class TmpfsAllocationAuditTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, int, int, list[int], int]:
        shm = root / "synthetic-shm"
        shm.mkdir(mode=0o700)
        (shm / "tree").mkdir(mode=0o700)
        (shm / "tree" / "data.bin").write_bytes(b"metadata-only fixture")
        os.link(shm / "tree" / "data.bin", shm / "tree" / "hardlink.bin")
        os.symlink("data.bin", shm / "tree" / "link")
        fdinfo_chain, fdinfo_fd = tmpfs_audit._open_own_fdinfo()
        shm_fd = os.open(shm, tmpfs_audit.DIR_FLAGS)
        mount_id = tmpfs_audit._fd_mount_id(shm_fd, fdinfo_fd)
        return shm, shm_fd, fdinfo_fd, fdinfo_chain, mount_id

    def test_meminfo_parser_is_closed_and_converts_only_the_two_counters(self) -> None:
        sample = tmpfs_audit.read_memory_snapshot(
            b"MemTotal: 2048 kB\nMemAvailable: 1000 kB\nShmem: 250 kB\n"
        )
        self.assertEqual(sample["mem_available_bytes"], 1_024_000)
        self.assertEqual(sample["shmem_bytes"], 256_000)
        self.assertNotIn("MemTotal", sample)
        for raw in (
            b"MemAvailable: 1 kB\nMemAvailable: 2 kB\nShmem: 1 kB\n",
            b"MemAvailable: 1 MB\nShmem: 1 kB\n",
            b"MemAvailable: 1 kB\n",
            b"MemAvailable: 1 kB\nShmem: 1 kB\n" + b"x" * tmpfs_audit.MAX_MEMINFO_BYTES,
        ):
            with self.subTest(raw=raw[:40]), self.assertRaises(tmpfs_audit.AuditError):
                tmpfs_audit.read_memory_snapshot(raw)

    def test_two_pass_scan_deduplicates_hardlinks_and_does_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tmpfs-audit-test-") as temporary:
            base = Path(temporary)
            _, shm_fd, fdinfo_fd, fdinfo_chain, mount_id = self._fixture(base)
            out = base / "out"
            out.mkdir(mode=0o700)
            out_fd = os.open(out, tmpfs_audit.DIR_FLAGS)
            inventory_fd = tmpfs_audit._create_output_file(out_fd, "inventory.jsonl")
            db = tmpfs_audit._private_sqlite(out_fd, tmpfs_audit.Limits())
            try:
                deadline = time.monotonic() + 10
                first = tmpfs_audit._scan_pass(shm_fd, mount_id, fdinfo_fd, db, inventory_fd, 1, deadline)
                os.fsync(inventory_fd)
                second = tmpfs_audit._scan_pass(shm_fd, mount_id, fdinfo_fd, db, None, 2, deadline)
                self.assertEqual((first["status"], second["status"]), ("COMPLETE", "COMPLETE"))
                self.assertEqual(first["entries"], 4)
                os.fsync(inventory_fd)
                read_fd = os.open("inventory.jsonl", tmpfs_audit.READ_FLAGS, dir_fd=out_fd)
                raw = bytearray()
                try:
                    while True:
                        chunk = os.read(read_fd, 64 * 1024)
                        if not chunk:
                            break
                        raw.extend(chunk)
                finally:
                    os.close(read_fd)
                parsed = [json.loads(line) for line in bytes(raw).splitlines()]
                self.assertEqual(sum(row["type"] == stat.S_IFLNK for row in parsed), 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM inodes").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM links_pass2 WHERE occurrences=nlink").fetchone()[0], 1)
            finally:
                db.close()
                os.close(inventory_fd)
                os.close(out_fd)
                os.close(shm_fd)
                for fd in reversed(fdinfo_chain):
                    os.close(fd)

    def test_entry_cap_and_between_pass_drift_are_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tmpfs-audit-cap-") as temporary:
            base = Path(temporary)
            _, shm_fd, fdinfo_fd, fdinfo_chain, mount_id = self._fixture(base)
            out = base / "out"
            out.mkdir(mode=0o700)
            out_fd = os.open(out, tmpfs_audit.DIR_FLAGS)
            inventory_fd = tmpfs_audit._create_output_file(out_fd, "inventory.jsonl")
            db = tmpfs_audit._private_sqlite(out_fd, tmpfs_audit.Limits())
            try:
                capped = tmpfs_audit._scan_pass(
                    shm_fd, mount_id, fdinfo_fd, db, inventory_fd, 1,
                    time.monotonic() + 10, tmpfs_audit.Limits(entries=1),
                )
                self.assertEqual(capped["status"], "UNKNOWN")
                self.assertIn("entry_limit", {item["category"] for item in capped["errors"]})
            finally:
                db.close()
                os.close(inventory_fd)
                os.close(out_fd)
                os.close(shm_fd)
                for fd in reversed(fdinfo_chain):
                    os.close(fd)

        with tempfile.TemporaryDirectory(prefix="tmpfs-audit-drift-") as temporary:
            base = Path(temporary)
            _, shm_fd, fdinfo_fd, fdinfo_chain, mount_id = self._fixture(base)
            out = base / "out"
            out.mkdir(mode=0o700)
            out_fd = os.open(out, tmpfs_audit.DIR_FLAGS)
            inventory_fd = tmpfs_audit._create_output_file(out_fd, "inventory.jsonl")
            db = tmpfs_audit._private_sqlite(out_fd, tmpfs_audit.Limits())
            try:
                first = tmpfs_audit._scan_pass(shm_fd, mount_id, fdinfo_fd, db, inventory_fd, 1, time.monotonic() + 10)
                (base / "synthetic-shm" / "tree" / "data.bin").write_bytes(b"changed size and identity metadata")
                second = tmpfs_audit._scan_pass(shm_fd, mount_id, fdinfo_fd, db, None, 2, time.monotonic() + 10)
                self.assertEqual(first["status"], "COMPLETE")
                self.assertEqual(second["status"], "UNKNOWN")
                self.assertIn("pass_identity_drift", {item["category"] for item in second["errors"]})
            finally:
                db.close()
                os.close(inventory_fd)
                os.close(out_fd)
                os.close(shm_fd)
                for fd in reversed(fdinfo_chain):
                    os.close(fd)

    def test_full_scan_keeps_memory_samples_contextual_and_outputs_private_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tmpfs-audit-run-") as temporary:
            base = Path(temporary)
            shm = base / "empty-shm"
            shm.mkdir(mode=0o700)
            shm_fd = os.open(shm, tmpfs_audit.DIR_FLAGS)
            fdinfo_chain, fdinfo_fd = tmpfs_audit._open_own_fdinfo()
            mount_id = tmpfs_audit._fd_mount_id(shm_fd, fdinfo_fd)
            out = base / "private-output"
            out.mkdir(mode=0o700)
            out_fd = os.open(out, tmpfs_audit.DIR_FLAGS)
            meminfo = base / "meminfo"
            meminfo.write_bytes(b"MemAvailable: 10 kB\nShmem: 2 kB\n")
            try:
                result = tmpfs_audit._run_scan(shm_fd, mount_id, fdinfo_fd, out_fd, meminfo_path=str(meminfo))
                self.assertEqual(result["status"], "COMPLETE_METADATA")
                self.assertEqual(result["memory_counters"]["before"]["mem_available_bytes"], 10 * 1024)
                self.assertEqual(result["memory_counters"]["after"]["shmem_bytes"], 2 * 1024)
                self.assertIn("not an admission decision", result["memory_counters_note"])
                for name in ("inventory.jsonl", "inode-index.sqlite"):
                    info = os.stat(out / name, follow_symlinks=False)
                    self.assertTrue(stat.S_ISREG(info.st_mode))
                    self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                    self.assertEqual(info.st_nlink, 1)
                _, summary_sha, summary_status = tmpfs_audit._write_summary(out_fd, result)
                terminal_sha = tmpfs_audit._write_terminal(out_fd, result["status"], summary_sha, [], [])
                self.assertEqual(len(summary_sha), 64)
                self.assertEqual(len(terminal_sha), 64)
                self.assertEqual(summary_status, "COMPLETE_METADATA")
                for name in ("summary.json", "terminal.json"):
                    info = os.stat(out / name, follow_symlinks=False)
                    self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                    self.assertEqual(info.st_nlink, 1)
                self.assertEqual(
                    tmpfs_audit.execution_disposition(0, "COMPLETE_METADATA", False),
                    "COMPLETE_METADATA",
                )
                self.assertEqual(
                    tmpfs_audit.execution_disposition(2, "COMPLETE_METADATA", True),
                    "UNKNOWN",
                )
                tmpfs_audit._write_failure(out_fd, "final_directory_fsync", OSError(5, "synthetic"))
                failure = json.loads((out / "failure.json").read_text(encoding="ascii"))
                self.assertEqual(failure["status"], "UNKNOWN")
                self.assertEqual(failure["stage"], "final_directory_fsync")
                oversized_out = base / "oversized-output"
                oversized_out.mkdir(mode=0o700)
                oversized_fd = os.open(oversized_out, tmpfs_audit.DIR_FLAGS)
                try:
                    oversized = {"status": "COMPLETE_METADATA", "payload": "x" * (256 * 1024)}
                    _, _, oversized_status = tmpfs_audit._write_summary(oversized_fd, oversized)
                    self.assertEqual(oversized_status, "UNKNOWN")
                    summary = json.loads((oversized_out / "summary.json").read_text(encoding="ascii"))
                    self.assertEqual(summary["errors"][0]["category"], "summary_size_limit")
                finally:
                    os.close(oversized_fd)
            finally:
                os.close(out_fd)
                os.close(shm_fd)
                for fd in reversed(fdinfo_chain):
                    os.close(fd)

    def test_public_main_completes_full_synthetic_scan_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tmpfs-audit-main-") as temporary:
            base = Path(temporary)
            shm = base / "synthetic-shm"
            shm.mkdir(mode=0o700)
            (shm / "fixture.bin").write_bytes(b"synthetic metadata scan")
            output = base / "private-output"
            fixed_sample = {
                "captured_at_utc": "2026-10-14T00:00:00Z",
                "status": "OBSERVED",
                "mem_available_kib": 1024,
                "mem_available_bytes": 1_048_576,
                "shmem_kib": 12,
                "shmem_bytes": 12_288,
                "source": "synthetic fixture",
                "scope_note": "contextual counters only; no admission decision or ownership attribution",
            }

            def synthetic_shm_root() -> tuple[list[int], int, int]:
                fd = os.open(shm, tmpfs_audit.DIR_FLAGS)
                return [fd], fd, os.fstat(fd).st_dev

            def synthetic_output_dir(path: Path) -> tuple[list[int], int]:
                parent_fd = os.open(path.parent, tmpfs_audit.DIR_FLAGS)
                os.mkdir(path.name, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
                return [parent_fd], os.open(path.name, tmpfs_audit.DIR_FLAGS, dir_fd=parent_fd)

            with (
                patch.object(tmpfs_audit.resource, "setrlimit"),
                patch.object(tmpfs_audit, "open_shm_root", side_effect=synthetic_shm_root),
                patch.object(tmpfs_audit, "create_output_dir", side_effect=synthetic_output_dir),
                patch.object(tmpfs_audit, "read_memory_sample", return_value=fixed_sample),
                patch("sys.stdout", new=io.StringIO()),
            ):
                exit_code = tmpfs_audit.main(["--output-dir", str(output)])

            self.assertEqual(exit_code, 0)
            summary = json.loads((output / "summary.json").read_text(encoding="ascii"))
            terminal = json.loads((output / "terminal.json").read_text(encoding="ascii"))
            self.assertEqual(summary["status"], "COMPLETE_METADATA")
            self.assertEqual(terminal["status"], "COMPLETE_METADATA")
            self.assertFalse((output / "failure.json").exists())
            self.assertEqual(summary["passes"][0]["status"], "COMPLETE")
            self.assertEqual(summary["passes"][1]["status"], "COMPLETE")

if __name__ == "__main__":
    unittest.main()
