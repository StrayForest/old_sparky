from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import signal
import tempfile
import unittest
from unittest.mock import patch

from tools.platform_external_load import (
    ExternalLoadError,
    RequestResult,
    VirtualUser,
    planned_http_attempts,
    run_load,
    summarize_results,
)
from tools.platform_external_load_observer import (
    _signal_api_workers_detailed,
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
    run_profile,
    validate_profile,
    _run_external_load_or_report,
    main as platform_load_main,
)


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

        self.assertEqual(len(profiles), 17)
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

    def test_runtime_budget_includes_retries_duplicates_and_state_reads(self) -> None:
        profiles = load_profiles()
        capacity = profiles["ready-vote-capacity-ramp-v2"]
        saturation = profiles["ready-vote-saturation-ramp-v2"]

        self.assertEqual(profile_contract(capacity)["planned_work"]["http_attempts"], 31_540)
        self.assertEqual(capacity["portfolio"]["request_budget"]["max_http_attempts"], 31_540)
        self.assertEqual(profile_contract(saturation)["planned_work"]["http_attempts"], 51_340)
        self.assertEqual(saturation["portfolio"]["request_budget"]["max_http_attempts"], 51_340)
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

    def test_dispatchability_excludes_deprecated_and_unbound_lifecycle_profiles(self) -> None:
        profiles = load_profiles()
        with self.assertRaises(LoadProfileError):
            ensure_dispatchable(profiles["ready-vote-saturation-ramp-v1"])
        for profile_id in (
            "tournament-lifecycle-slo-v1",
            "tournament-lifecycle-scale-v1",
            "tournament-lifecycle-capacity-v1",
        ):
            profile = profiles[profile_id]
            self.assertTrue(profile["execution"]["non_dispatchable"])
            self.assertEqual(
                profile["execution"]["profile_binding"],
                "not-integrated-with-production-qa",
            )
            with self.assertRaises(LoadProfileError):
                ensure_dispatchable(profile)

        # A historical artifact may still be inspected, but evaluate is an
        # authoritative boundary and must reject it before report evidence can
        # be accepted or upgraded to PASS.
        rejected_profile_ids = (
            "ready-vote-saturation-ramp-v1",
            "tournament-lifecycle-slo-v1",
        )
        with (
            patch.dict(
                os.environ,
                {"SOURCE_GIT_SHA": "a" * 40, "GITHUB_RUN_ID": "123"},
                clear=False,
            ),
            tempfile.TemporaryDirectory() as directory,
        ):
            for profile_id in rejected_profile_ids:
                profile = profiles[profile_id]
                report_path = Path(directory) / f"{profile_id}.json"
                report_path.write_text(
                    json.dumps(self._bound_report(profile)),
                    encoding="utf-8",
                )
                result = evaluate_report(profile, report_path, None)
                evaluated = json.loads(report_path.read_text(encoding="utf-8"))
                with self.subTest(non_dispatchable_profile=profile_id):
                    self.assertEqual(result, 1)
                    self.assertFalse(evaluated["authoritative"])
                    self.assertFalse(evaluated["dispatchable"])
                    self.assertFalse(evaluated["acceptance"]["passed"])
                    self.assertEqual(
                        evaluated["acceptance"]["decision"],
                        "LOAD PROFILE NON-AUTHORITATIVE",
                    )

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
                result = run_profile(
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
                    missing_result = run_profile(
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
                    run_result = run_profile(
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
            profile.write_bytes(b"old")
            wrong_generation.write_bytes(b"wrong")
            partial.write_bytes(b"partial")
            baseline = profile_artifact_snapshot(output_dir)

            class FakeStats:
                stats: dict[object, object] = {}

                def __init__(self, path: str) -> None:
                    if "124-457" in path:
                        raise ValueError("partial profile")

            identities = {123: {"pid": 123, "start_time_ticks": 456}}
            with patch("tools.platform_external_load_observer.Stats", FakeStats):
                stale = cpu_profile_summary(
                    output_dir,
                    armed_identities=identities,
                    baseline_artifacts=baseline,
                )
                self.assertEqual(len(stale["profiles"]), 0)
                self.assertEqual(stale["retention"]["ignored_stale_profiles"], 1)

                profile.write_bytes(b"new-profile")
                partial.write_bytes(b"partial-new")
                fresh = cpu_profile_summary(
                    output_dir,
                    armed_identities={
                        123: {"pid": 123, "start_time_ticks": 456},
                        124: {"pid": 124, "start_time_ticks": 457},
                    },
                    baseline_artifacts=baseline,
                )

            self.assertEqual(len(fresh["profiles"]), 1)
            self.assertEqual(fresh["retention"]["invalid_profiles"], 1)
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
            signalled, reasons = _signal_api_workers_detailed(
                signal.SIGUSR1,
                processes=processes,
                expected_uid=994,
                process_reader=lambda pid: records.get(pid),  # type: ignore[arg-type]
            )

        self.assertEqual(signalled, [])
        self.assertEqual(reasons, {"pidfd_unavailable": 1})
        kill.assert_not_called()

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
            signalled = signal_api_workers(
                signal.SIGUSR2,
                processes=reused_processes,
                expected_uid=994,
                armed_identities=armed,
                process_reader=process_reader,
            )

        self.assertEqual(signalled, [102])
        pidfd_send.assert_called_once_with(1102, signal.SIGUSR2)
        kill.assert_not_called()

    def test_supervisor_binds_observer_and_workflow_blocks_deprecated_profiles(self) -> None:
        root = Path(__file__).resolve().parents[1]
        supervisor = (root / "tools" / "platform_production_external_fixture_qa.sh").read_text(
            encoding="utf-8"
        )
        observer = (root / "tools" / "platform_external_load_observer.py").read_text(
            encoding="utf-8"
        )
        lock_helper = (root / "tools" / "platform_release_lock.sh").read_text(
            encoding="utf-8"
        )
        workflow = (root.parent / ".github" / "workflows" / "platform-production-external-load.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("re.fullmatch(r\"preprod[0-9]{12}[0-9a-f]{4}\", marker)", supervisor)
        self.assertIn('--fixture-marker "$fixture_marker"', supervisor)
        self.assertIn('--external-run-id "$run_id"', supervisor)
        self.assertLess(supervisor.index('--fixture-marker "$fixture_marker"'), supervisor.index(': > "$external_vote_ready"'))
        self.assertLess(
            supervisor.index("platform_retained_load_lock_supervise"),
            supervisor.index("platform_external_load_observer.py"),
        )
        self.assertIn("/run/lock/oldsparky-retained-load-matrix.lock", lock_helper)
        self.assertIn("pidfd_send_signal", observer)
        self.assertNotIn("os.kill(pid, signum)", observer)
        for deprecated in (
            "ready-vote-saturation-ramp-v1",
            "ready-vote-saturation-ramp-v2",
            "ready-vote-saturation-ramp-v3",
        ):
            self.assertNotIn(f"          - {deprecated}", workflow)
        self.assertIn("platform_load.py validate", workflow)
        self.assertIn('--profile "$PROFILE_ID" --dispatchable', workflow)

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

    def test_deprecated_profile_is_retained_for_listing_but_not_runnable(self) -> None:
        profiles = load_profiles()
        deprecated_id = "ready-vote-saturation-ramp-v1"

        self.assertIn(deprecated_id, profiles)
        with self.assertRaises(LoadProfileError):
            run_profile(
                profiles[deprecated_id],
                manifest_path=Path("/tmp/manifest.json"),
                report_path=Path("/tmp/report.json"),
            )


if __name__ == "__main__":
    unittest.main()
