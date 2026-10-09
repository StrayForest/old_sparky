"""End-to-end, hermetic tests for deferred external-load SLO decisions.

The test keeps the production report builder/evaluator and workflow snippets
real.  Only the supervised-worker boundary and HTTP transport are replaced;
no database, remote origin, credentials, or live workflow is used.
"""

from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import hashlib
import base64
import io
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
import unittest
import zipfile
from unittest.mock import patch

import yaml

from tools import platform_load
from tools.platform_external_load import RequestResult, VirtualUser
from tools.platform_load_runtime import PID_NAMESPACE_ISOLATION, WORKER_REPORT_SCHEMA
from tools.platform_load_acceptance import _acceptance_budget_evidence


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLATFORM_ROOT.parent
WORKFLOW_PATH = REPOSITORY_ROOT / ".github/workflows/platform-production-external-load.yml"
SOURCE_SHA = "a" * 40
RUN_ID = "123456789"


def _run_step(job: str, step_name: str) -> str:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    step = next(item for item in workflow["jobs"][job]["steps"] if item.get("name") == step_name)
    return str(step["run"])


def _python_block(script: str, command_marker: str) -> str:
    start = script.index(command_marker)
    heredoc = script.index("<<'PY'", start) + len("<<'PY'")
    end = script.index("\nPY", heredoc)
    return script[heredoc:end].lstrip("\n")


def _hermetic_profile(profile_id: str) -> dict[str, object]:
    """Keep the canonical mode/acceptance shape while shrinking fixture work."""

    profile = deepcopy(platform_load.get_profile(profile_id))
    fixture = profile["fixture"]
    fixture["tournament_count"] = 1
    fixture["users_per_tournament"] = 1
    fixture["max_total_users"] = 1
    profile["traffic"]["spread_seconds"] = 0
    if profile["mode"] == "read-mix":
        profile["traffic"]["manual_refresh_count"] = 0
    return profile


def _fake_manifest() -> tuple[dict[str, object], list[VirtualUser]]:
    manifest: dict[str, object] = {
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
    return manifest, users


def _small_profile(profile: dict[str, object]) -> tuple[dict[str, object], dict[str, object], list[VirtualUser]]:
    scenario = deepcopy(profile)
    fixture = scenario["fixture"]
    traffic = scenario["traffic"]
    phases = traffic.get("phases") or []
    if phases:
        if profile["acceptance"]["kind"] == "capacity":
            phases = phases[:1]
            traffic["phases"] = phases
        for phase in phases:
            phase["logical_actions"] = 1
            phase["duration_seconds"] = 1
            phase["target_logical_actions_per_second"] = 1
        user_count = len(phases)
    else:
        user_count = 1
    fixture["tournament_count"] = 1
    fixture["users_per_tournament"] = user_count
    fixture["max_total_users"] = user_count
    traffic["spread_seconds"] = 0
    traffic["duplicate_count"] = 0
    traffic["manual_refresh_count"] = 0
    manifest = {
        "origin": "https://old-sparky.com",
        "session_cookie_name": "deadlock_platform_session",
        "csrf_cookie_name": "deadlock_platform_session_csrf",
        "marker": "preprod26082900000000ab",
        "tournaments": [
            {"id": "tournament-1", "slug": "qa-tournament", "user_count": user_count}
        ],
    }
    users = [
        VirtualUser(
            user_id=f"user-{index + 1:08d}",
            tournament_slug="qa-tournament",
            session_token="s" * 64,
            csrf_token="c" * 64,
        )
        for index in range(user_count)
    ]
    return scenario, manifest, users


def _observer(*, marker: str, run_id: str) -> dict[str, object]:
    return {
        "schema": 1,
        "stop_file_seen": True,
        "timed_out": False,
        "binding": {
            "complete": True,
            "fixture_marker": marker,
            "external_run_id": run_id,
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


def _false_paths(value: object, prefix: tuple[str, ...] = ()) -> list[str]:
    if isinstance(value, dict):
        return [
            path
            for key, child in value.items()
            for path in _false_paths(child, prefix + (str(key),))
        ]
    if isinstance(value, list):
        return [path for index, child in enumerate(value) for path in _false_paths(child, prefix + (str(index),))]
    if value is False:
        return [".".join(prefix)]
    return []


def _replace_status(value: object, old_status: str, new_status: str) -> None:
    if isinstance(value, dict):
        for key, child in tuple(value.items()):
            if key in {"status_counts", "final_status_counts"} and isinstance(child, dict):
                if old_status in child:
                    child[new_status] = child.pop(old_status)
            elif key == "status" and child == int(old_status):
                value[key] = int(new_status)
            else:
                _replace_status(child, old_status, new_status)
    elif isinstance(value, list):
        for child in value:
            _replace_status(child, old_status, new_status)


def _replace_error_class(value: object, old_class: str, new_class: str) -> None:
    if isinstance(value, dict):
        for key, child in tuple(value.items()):
            if key == "error_kinds" and isinstance(child, dict) and old_class in child:
                child[new_class] = child.pop(old_class)
            elif key == "error_class" and child == old_class:
                value[key] = new_class
            else:
                _replace_error_class(child, old_class, new_class)
    elif isinstance(value, list):
        for child in value:
            _replace_error_class(child, old_class, new_class)


def _mutate_first_failure_rate(value: object, replacement: object) -> bool:
    if isinstance(value, dict):
        if "final_failure_rate_percent" in value:
            value["final_failure_rate_percent"] = replacement
            return True
        return any(_mutate_first_failure_rate(child, replacement) for child in value.values())
    if isinstance(value, list):
        return any(_mutate_first_failure_rate(child, replacement) for child in value)
    return False


def _remove_first_timing_marker(value: object, marker: str) -> bool:
    if isinstance(value, dict):
        timing = value.get("timing")
        if isinstance(timing, dict) and marker in timing:
            timing.pop(marker)
            return True
        return any(_remove_first_timing_marker(child, marker) for child in value.values())
    if isinstance(value, list):
        return any(_remove_first_timing_marker(child, marker) for child in value)
    return False


def _add_explicit_child_to_first_raw_summary(value: object) -> bool:
    if isinstance(value, dict):
        if "requests" in value and "status_counts" in value:
            value["logical"] = {}
            return True
        return any(_add_explicit_child_to_first_raw_summary(child) for child in value.values())
    if isinstance(value, list):
        return any(_add_explicit_child_to_first_raw_summary(child) for child in value)
    return False


class ExternalLoadPendingOriginPipelineTests(unittest.TestCase):
    def test_all_authored_profiles_have_closed_budget_builder_contracts(self) -> None:
        profiles = platform_load.load_profiles()
        self.assertEqual(len(profiles), 11)
        for profile_id, profile in profiles.items():
            with self.subTest(profile=profile_id):
                evidence = _acceptance_budget_evidence(
                    profile["acceptance"], require_statuses=True
                )
                self.assertIs(evidence["complete"], True)
                self.assertIsInstance(evidence["checks"], dict)
                self.assertTrue(evidence["checks"])
                self.assertTrue(
                    all(type(value) is bool for value in evidence["checks"].values())
                )
                scenario, manifest, users = _small_profile(profile)

                def request(origin: str, user: VirtualUser, phase: str | None = None, *_args: object, **kwargs: object) -> RequestResult:
                    del origin, user
                    effective_phase = kwargs.get("phase") or phase
                    started = time.monotonic()
                    response_json = (
                        {"active_round": {"ready_count": len(users)}}
                        if effective_phase == "read_external_vote_state"
                        else {"changed": True}
                        if kwargs.get("method") == "POST"
                        else None
                    )
                    return RequestResult(
                        phase=str(kwargs.get("phase") or phase or "primary"),
                        method=str(kwargs.get("method") or "GET"),
                        path=str(kwargs.get("path") or "/"),
                        status=200,
                        elapsed_ms=10_000,
                        ok=True,
                        response_bytes=128,
                        response_etag='"fixture-etag"',
                        response_json=response_json,
                        started_at_monotonic=started,
                        finished_at_monotonic=started + 10,
                    )

                with tempfile.TemporaryDirectory(prefix="pending-profile-builder-") as temporary:
                    report_path = Path(temporary) / "report.json"
                    with (
                        patch("tools.platform_external_load.load_manifest", return_value=(manifest, users)),
                        patch("tools.platform_external_load._trace", return_value={"status": "200", "ip": "192.0.2.10", "colo": "TEST"}),
                        patch("tools.platform_external_load._request", side_effect=request),
                        patch("tools.platform_external_load._page_request_http11_keepalive", side_effect=request),
                        patch("socket.socket.connect", side_effect=AssertionError("network access is forbidden in this test")),
                        patch.dict(os.environ, {"SOURCE_GIT_SHA": SOURCE_SHA, "GITHUB_RUN_ID": RUN_ID}, clear=False),
                    ):
                        with redirect_stdout(io.StringIO()):
                            worker_exit = platform_load.run_profile_worker(
                                scenario, Path(temporary) / "manifest.json", report_path
                            )
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(worker_exit, 1)
                self.assertTrue(report["acceptance"]["pending_origin_evidence"])
                self.assertFalse(report["acceptance"]["passed"])
                self.assertTrue(report["acceptance"]["timing_evidence"]["complete"])
                report.update(
                    {
                        "worker_report_schema": WORKER_REPORT_SCHEMA,
                        "report_complete": True,
                        "namespace_closed": True,
                        "isolation": PID_NAMESPACE_ISOLATION,
                        "partial_work": False,
                        "inflight_unknown": False,
                        "worker_exit_code": 1,
                        "runtime_supervisor": {
                            "reason": "none",
                            "returncode": 1,
                            "isolation": PID_NAMESPACE_ISOLATION,
                            "namespace_closed": True,
                            "descendants_reaped": True,
                            "partial_work": False,
                            "inflight_unknown": False,
                            "report_error": None,
                        },
                    }
                )
                result = SimpleNamespace(
                    returncode=1, report=report, worker_started=True,
                    worker_exited=True, killed=False, signal=None, reason="none",
                    partial_work=False, inflight_unknown=False,
                    descendants_reaped=True, isolation=PID_NAMESPACE_ISOLATION,
                    namespace_closed=True,
                )
                false_budget_paths = [
                    path for path in _false_paths(report["acceptance"])
                    if "accepted_p95" in path or "accepted_p95_ms" in path or "logical_p95" in path
                ]
                self.assertTrue(false_budget_paths, false_budget_paths)
                with patch.dict(
                    os.environ,
                    {"SOURCE_GIT_SHA": SOURCE_SHA, "GITHUB_RUN_ID": RUN_ID},
                    clear=False,
                ):
                    self.assertTrue(
                        platform_load._is_closed_pending_origin_candidate(
                            scenario, result
                        ),
                        {
                            "profile": profile_id,
                            "decision": report["acceptance"].get("decision"),
                            "pending_origin_evidence": report["acceptance"].get(
                                "pending_origin_evidence"
                            ),
                            "false_budget_paths": false_budget_paths,
                            "budget_only": platform_load._acceptance_failure_is_budget_only(
                                report["acceptance"],
                                str(scenario["acceptance"]["kind"]),
                                allowed_ramp_budget_checks=platform_load._authored_ramp_budget_checks(
                                    scenario, report["acceptance"]
                                ) or frozenset(),
                                allow_no_budget_failure=True,
                                allow_pending_observer_binding=True,
                                allowed_phase_names=platform_load._profile_budget_phase_names(
                                    scenario
                                ),
                            ),
                            "observer_closed": platform_load._closed_missing_observer_binding(
                                report["acceptance"].get("observer_binding")
                            ),
                            "phase_evidence_equal": report["acceptance"].get(
                                "phase_plan_evidence", {}
                            ).get("phase_budgets") == report["acceptance"].get(
                                "phase_budget_evidence"
                            ),
                            "ramp_check": repr(
                                platform_load._authored_ramp_budget_checks(
                                    scenario, report["acceptance"]
                                )
                            ),
                            "report_binding_complete": platform_load._report_binding(
                                scenario, report
                            ).get("complete"),
                        },
                    )
                forged = deepcopy(result.report)
                forged["acceptance"]["unrelated_diagnostic"] = {
                    "checks": {"accepted_p95": False}
                }
                forged_result = SimpleNamespace(**vars(result))
                forged_result.report = forged
                with patch.dict(
                    os.environ,
                    {"SOURCE_GIT_SHA": SOURCE_SHA, "GITHUB_RUN_ID": RUN_ID},
                    clear=False,
                ):
                    self.assertFalse(
                        platform_load._is_closed_pending_origin_candidate(
                            scenario, forged_result
                        ),
                        "a budget-named false leaf outside the authored acceptance paths must be rejected",
                    )
                malformed = deepcopy(result.report)
                acceptance_checks = malformed["acceptance"].get("checks")
                if isinstance(acceptance_checks, dict):
                    acceptance_checks["contract_ok"] = False
                else:
                    malformed["acceptance"]["unrelated_diagnostic"] = {
                        "checks": {"contract": False}
                    }
                malformed_result = SimpleNamespace(**vars(result))
                malformed_result.report = malformed
                with patch.dict(
                    os.environ,
                    {"SOURCE_GIT_SHA": SOURCE_SHA, "GITHUB_RUN_ID": RUN_ID},
                    clear=False,
                ):
                    self.assertFalse(
                        platform_load._is_closed_pending_origin_candidate(
                            scenario, malformed_result
                        ),
                        "a non-budget acceptance failure must not be classified as pending-only",
                    )
                origin_attached = deepcopy(result.report)
                origin_attached["origin_observability"] = {"complete": False}
                origin_result = SimpleNamespace(**vars(result))
                origin_result.report = origin_attached
                with patch.dict(
                    os.environ,
                    {"SOURCE_GIT_SHA": SOURCE_SHA, "GITHUB_RUN_ID": RUN_ID},
                    clear=False,
                ):
                    self.assertFalse(
                        platform_load._is_closed_pending_origin_candidate(
                            scenario, origin_result
                        ),
                        "a report with present but invalid origin evidence is not pending-origin",
                    )
                # The actual authored profile and its scaled hermetic clone
                # retain the same acceptance kind and exact schema keys.
                self.assertEqual(
                    scenario["acceptance"]["kind"], profile["acceptance"]["kind"]
                )

    def test_all_authored_builders_close_transport_and_server_failures(self) -> None:
        for profile_id in platform_load.load_profiles():
            profile = platform_load.get_profile(profile_id)
            for failure_kind, expected_status, expected_error in (
                ("transport", 0, "transport"),
                ("http_error", 500, "server_error"),
            ):
                if (
                    profile.get("client_transport") == "http1-keepalive"
                    and failure_kind == "transport"
                ):
                    expected_error = "other"
                with self.subTest(profile=profile_id, failure=failure_kind):
                    self._exercise_closed_failure_builder(
                        profile_id, failure_kind, expected_status, expected_error
                    )

    def test_explicit_child_boundary_disables_non_ready_raw_alias(self) -> None:
        self._exercise_closed_failure_builder(
            "read-mix-stress-v2",
            "transport",
            0,
            "transport",
            add_explicit_child=True,
        )

    def _exercise_closed_failure_builder(
        self,
        profile_id: str,
        failure_kind: str,
        expected_status: int,
        expected_error: str,
        *,
        add_explicit_child: bool = False,
    ) -> None:
        profile = deepcopy(platform_load.get_profile(profile_id))
        profile, manifest, users = _small_profile(profile)
        if (
            profile.get("mode") == "read-mix"
            and profile.get("traffic", {}).get("concurrency_stages") is not None
        ):
            target_users = len(users)
        elif profile.get("acceptance", {}).get("kind") == "capacity":
            profile["traffic"]["phases"][0]["logical_actions"] = 2
            profile["traffic"]["phases"][0]["target_logical_actions_per_second"] = 2
            target_users = 2
        else:
            target_users = max(2, len(users))
        while len(users) < target_users:
            index = len(users) + 1
            users.append(
                VirtualUser(
                    user_id=f"user-{index:08d}",
                    tournament_slug="qa-tournament",
                    session_token=chr(ord("s") + index) * 64,
                    csrf_token=chr(ord("c") + index) * 64,
                )
            )
        profile["fixture"]["users_per_tournament"] = target_users
        profile["fixture"]["max_total_users"] = target_users
        manifest["tournaments"][0]["user_count"] = target_users
        users = [
            VirtualUser(
                user_id=user.user_id,
                tournament_slug=user.tournament_slug,
                session_token=(
                    user.session_token
                    if index == 0
                    else chr(ord("s") + index) * 64
                ),
                csrf_token=(
                    user.csrf_token
                    if index == 0
                    else chr(ord("c") + index) * 64
                ),
            )
            for index, user in enumerate(users)
        ]
        failure_lock = threading.Lock()
        failing_session_token = users[0].session_token
        failing_attempts = 0
        failed_once = False

        class FakeResponse:
            status = 200
            headers = {"etag": '"fixture-etag"', "cf-ray": "fixture-ray"}

            def __init__(self, body: bytes) -> None:
                self._body = body
                self._offset = 0

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                if size is None or size < 0:
                    size = len(self._body) - self._offset
                chunk = self._body[self._offset : self._offset + size]
                self._offset += len(chunk)
                return chunk

        class FakeHTTP11Response:
            version = 11
            reason = "synthetic"
            will_close = False

            def __init__(self, status: int) -> None:
                self.status = status
                self.headers = {"etag": '"fixture-etag"', "cf-ray": "fixture-ray"}

            def read(self, _size: int = -1) -> bytes:
                return b'<!doctype html><html><body>synthetic</body></html>'

        def urlopen(
            request_object: object,
            *,
            timeout: float,
            context: ssl.SSLContext,
        ) -> FakeResponse:
            nonlocal failing_attempts, failed_once
            del timeout, context
            method = request_object.get_method()
            path = request_object.full_url
            cookie = request_object.get_header("Cookie") or ""
            failure_eligible = (
                method == "POST"
                if profile.get("mode") == "ready-vote"
                else not path.endswith("/deadlock/ready-check")
            )
            with failure_lock:
                inject_failure = (
                    failing_session_token in cookie and failure_eligible
                )
                if inject_failure and profile.get("mode") != "ready-vote":
                    inject_failure = not failed_once
                    failed_once = True
                if inject_failure:
                    failing_attempts += 1
            if inject_failure:
                if failure_kind == "transport":
                    raise URLError("synthetic bounded transport failure")
                raise HTTPError(
                    path,
                    500,
                    "synthetic bounded server failure",
                    {},
                    io.BytesIO(b'{"error":"synthetic"}'),
                )
            body = (
                json.dumps(
                    {"active_round": {"ready_count": max(0, len(users) - 1)}}
                ).encode("ascii")
                if path.endswith("/deadlock/ready-check")
                else b'{"changed":true}'
                if method == "POST"
                else b"<!doctype html><html><body>ok</body></html>"
            )
            return FakeResponse(body)

        def http11_request(
            connection: object, *_args: object, **kwargs: object
        ) -> None:
            headers = kwargs.get("headers")
            cookie = headers.get("Cookie", "") if isinstance(headers, dict) else ""
            setattr(connection, "_terminal_test_cookie", cookie)

        def http11_response(connection: object) -> FakeHTTP11Response:
            nonlocal failing_attempts
            cookie = getattr(connection, "_terminal_test_cookie", "")
            with failure_lock:
                inject_failure = failing_session_token in cookie
                if inject_failure:
                    failing_attempts += 1
            if inject_failure and failure_kind == "transport":
                raise OSError("synthetic bounded HTTP/1.1 transport failure")
            return FakeHTTP11Response(
                500 if inject_failure and failure_kind == "http_error" else 200
            )

        with tempfile.TemporaryDirectory(prefix="terminal-failure-builder-") as temporary:
            report_path = Path(temporary) / "report.json"
            with ExitStack() as stack:
                stack.enter_context(
                    patch("tools.platform_external_load.load_manifest", return_value=(manifest, users))
                )
                stack.enter_context(
                    patch("tools.platform_external_load._trace", return_value={"status": "200", "ip": "192.0.2.10", "colo": "TEST"})
                )
                if profile.get("client_transport") == "http1-keepalive":
                    stack.enter_context(
                        patch("tools.platform_http_transport._TimedHTTPConnection.request", autospec=True, side_effect=http11_request)
                    )
                    stack.enter_context(
                        patch("tools.platform_http_transport._TimedHTTPConnection.getresponse", autospec=True, side_effect=http11_response)
                    )
                else:
                    stack.enter_context(
                        patch("tools.platform_external_load.urlopen", side_effect=urlopen)
                    )
                stack.enter_context(
                    patch("socket.socket.connect", side_effect=AssertionError("network access is forbidden in this test"))
                )
                stack.enter_context(
                    patch.dict(os.environ, {"SOURCE_GIT_SHA": SOURCE_SHA, "GITHUB_RUN_ID": RUN_ID}, clear=False)
                )
                with redirect_stdout(io.StringIO()):
                    worker_exit = platform_load.run_profile_worker(
                        profile, Path(temporary) / "manifest.json", report_path
                    )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        report.update(
            {
                "worker_report_schema": WORKER_REPORT_SCHEMA,
                "report_complete": True,
                "namespace_closed": True,
                "isolation": PID_NAMESPACE_ISOLATION,
                "partial_work": False,
                "inflight_unknown": False,
                "worker_exit_code": worker_exit,
                "runtime_supervisor": {
                    "reason": "none",
                    "returncode": worker_exit,
                    "isolation": PID_NAMESPACE_ISOLATION,
                    "namespace_closed": True,
                    "descendants_reaped": True,
                    "partial_work": False,
                    "inflight_unknown": False,
                    "report_error": None,
                },
            }
        )
        if add_explicit_child:
            self.assertTrue(
                _add_explicit_child_to_first_raw_summary(report.get("phases"))
            )
        result = SimpleNamespace(
            returncode=worker_exit,
            report=report,
            worker_started=True,
            worker_exited=True,
            killed=False,
            signal=None,
            reason="none",
            partial_work=False,
            inflight_unknown=False,
            descendants_reaped=True,
            isolation=PID_NAMESPACE_ISOLATION,
            namespace_closed=True,
        )
        with patch.dict(
            os.environ,
            {"SOURCE_GIT_SHA": SOURCE_SHA, "GITHUB_RUN_ID": RUN_ID},
            clear=False,
        ):
            closed_failure = platform_load._closed_terminal_status_failure(profile, report)
            if add_explicit_child:
                self.assertFalse(closed_failure)
            else:
                self.assertTrue(
                    closed_failure,
                    {
                        "profile": profile_id,
                        "failure": failure_kind,
                        "status_counts": report.get("raw_http", {}).get("status_counts"),
                        "error_kinds": report.get("raw_http", {}).get("error_kinds"),
                        "contract_ok": report.get("acceptance", {}).get("contract_ok"),
                        "passed": report.get("acceptance", {}).get("passed"),
                        "failed_checks": {
                            key: value
                            for key, value in (
                                report.get("acceptance", {}).get("checks") or {}
                            ).items()
                            if value is False
                        },
                        "raw_http": {
                            key: report.get("raw_http", {}).get(key)
                            for key in (
                                "requests", "successful_responses", "errors",
                                "unexpected_statuses", "status_counts", "error_kinds",
                                "temporary_overload_responses",
                            )
                        },
                        "summary_keys": {
                            key: sorted(report.get(key, {}).keys())
                            for key in ("raw_http", "overall", "logical")
                            if isinstance(report.get(key), dict)
                        },
                        "logical_status_counts": report.get("logical", {}).get(
                            "final_status_counts",
                            report.get("logical", {}).get("status_counts"),
                        ),
                        "overall_status_counts": report.get("overall", {}).get(
                            "status_counts"
                        ),
                    },
                )
            if not add_explicit_child:
                self.assertTrue(
                    platform_load._is_closed_pending_origin_candidate(profile, result)
                )
        self.assertFalse(report["acceptance"]["contract_ok"])
        self.assertFalse(report["acceptance"]["passed"])
        observed_failures = report["raw_http"]["status_counts"].get(
            str(expected_status), 0
        )
        self.assertGreaterEqual(observed_failures, 1)
        self.assertEqual(
            report["raw_http"]["error_kinds"].get(expected_error),
            observed_failures,
        )

    def test_cli_pending_then_origin_budget_failure_reaches_yaml_publish_and_final_fail(self) -> None:
        for profile_id in ("read-mix-human-v2", "ready-vote-capacity-ramp-v2"):
            with self.subTest(profile=profile_id):
                self._exercise_deferred_pipeline(profile_id)

    def test_cli_completed_transport_failure_reaches_origin_bound_red_gate(self) -> None:
        self._exercise_deferred_pipeline(
            "ready-vote-slo-v2", transport_failure=True
        )

    def test_terminal_status_failure_classifier_rejects_unclosed_mutations(self) -> None:
        for mutation in (
            "status_418",
            "unknown_error_class",
            "nonbudget_false_leaf",
            "partial_work",
            "inflight_unknown",
            "forged_source",
            "forged_run",
            "forged_profile",
            "forged_digest",
            "missing_counts",
            "unrecognized_status1",
            "forged_logical_failure_rate",
            "forged_raw_failure_rate",
            "forged_phase_failure_rate",
            "boolean_failure_rate",
            "nonfinite_failure_rate",
            "missing_raw_timing_schema",
            "missing_logical_partial_marker",
            "missing_phase_timing_schema",
            "missing_phase_partial_marker",
            "unrelated_phase_budget",
        ):
            with self.subTest(mutation=mutation):
                self._exercise_deferred_pipeline(
                    "ready-vote-spike-v1"
                    if mutation == "unrelated_phase_budget"
                    else "ready-vote-slo-v2",
                    transport_failure=True,
                    candidate_mutation=mutation,
                    expected_candidate_exit=1,
                )

    def _exercise_deferred_pipeline(
        self,
        profile_id: str,
        *,
        transport_failure: bool = False,
        candidate_mutation: str | None = None,
        expected_candidate_exit: int = 3,
        source_binding: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        profile = _hermetic_profile(profile_id)
        profile, manifest, users = _small_profile(profile)
        if transport_failure:
            # Preserve one real successful mutation before the typed transport
            # failure so the producer still performs its final state read.
            target_users = max(2, len(users))
            profile["fixture"]["users_per_tournament"] = target_users
            profile["fixture"]["max_total_users"] = target_users
            manifest["tournaments"][0]["user_count"] = target_users
            while len(users) < target_users:
                index = len(users) + 1
                users.append(
                    VirtualUser(
                        user_id=f"user-{index:08d}",
                        tournament_slug="qa-tournament",
                        session_token=chr(ord("s") + index) * 64,
                        csrf_token=chr(ord("c") + index) * 64,
                    )
                )
        report_marker = str(manifest["marker"])
        app_target_sha = SOURCE_SHA
        source_binding_digest: str | None = None
        source_binding_b64 = ""
        if source_binding is not None:
            app_value = source_binding.get("app_target_sha")
            if not isinstance(app_value, str):
                raise AssertionError("source-binding test fixture lacks app target SHA")
            app_target_sha = app_value
            canonical_binding = json.dumps(
                source_binding,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            source_binding_digest = hashlib.sha256(canonical_binding).hexdigest()
            source_binding_b64 = base64.b64encode(canonical_binding).decode("ascii")
            manifest.update(
                {
                    "schema": 2,
                    "runner_sha": SOURCE_SHA,
                    "app_target_sha": app_target_sha,
                    "source_binding": source_binding,
                    "source_binding_sha256": source_binding_digest,
                }
            )
        candidate_debug: dict[str, object] = {}

        def request(origin: str, user: VirtualUser, **kwargs: object) -> RequestResult:
            del origin, user
            started = time.monotonic()
            # A completed response outside the authored latency target creates
            # a genuine evaluator-produced budget failure without wall delay.
            return RequestResult(
                phase=str(kwargs.get("phase") or "read_mix"),
                method=str(kwargs.get("method") or "GET"),
                path=str(kwargs.get("path") or "/"),
                status=200,
                elapsed_ms=10_000,
                ok=True,
                response_bytes=128,
                response_etag='"fixture-etag"',
                response_json=(
                    {"active_round": {"ready_count": len(users)}}
                    if kwargs.get("phase") == "read_external_vote_state"
                    else {"changed": True}
                    if kwargs.get("method") == "POST"
                    else None
                ),
                started_at_monotonic=started,
                finished_at_monotonic=started + 10,
            )

        transport_failures = 0
        post_requests = 0

        class FakeResponse:
            status = 200
            headers = {
                "etag": '"fixture-etag"',
                "cf-ray": "fixture-ray",
            }

            def __init__(self, body: bytes) -> None:
                self._body = body
                self._offset = 0

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                if size is None or size < 0:
                    size = len(self._body) - self._offset
                chunk = self._body[self._offset : self._offset + size]
                self._offset += len(chunk)
                return chunk

        def urlopen(
            request_object: object,
            *,
            timeout: float,
            context: ssl.SSLContext,
        ) -> FakeResponse:
            nonlocal post_requests, transport_failures
            del timeout, context
            method = request_object.get_method()
            path = request_object.full_url
            if method == "POST":
                post_requests += 1
                if post_requests == 2:
                    transport_failures += 1
                    raise URLError("synthetic bounded transport failure")
            body = (
                b'{"changed":true}'
                if method == "POST"
                else b'{"active_round":{"ready_count":1}}'
                if path.endswith("/deadlock/ready-check")
                else b"{}"
            )
            return FakeResponse(body)

        def supervised_worker(
            *, worker_config: dict[str, object], report_path: Path,
            worker_report_path: Path, **_kwargs: object,
        ) -> SimpleNamespace:
            with redirect_stdout(io.StringIO()):
                worker_exit = platform_load.run_profile_worker(
                    profile,
                    Path(str(worker_config["manifest_path"])),
                    worker_report_path,
                )
            report = json.loads(worker_report_path.read_text(encoding="utf-8"))
            report.update(
                {
                    "worker_report_schema": WORKER_REPORT_SCHEMA,
                    "report_complete": True,
                    "namespace_closed": True,
                    "isolation": PID_NAMESPACE_ISOLATION,
                    "partial_work": False,
                    "inflight_unknown": False,
                    "worker_exit_code": worker_exit,
                    "runtime_supervisor": {
                        "reason": "none",
                        "returncode": worker_exit,
                        "isolation": PID_NAMESPACE_ISOLATION,
                        "namespace_closed": True,
                        "descendants_reaped": True,
                        "partial_work": False,
                        "inflight_unknown": False,
                        "report_error": None,
                    },
                }
            )
            supervisor_reason = "none"
            if candidate_mutation == "status_418":
                _replace_status(report, "0", "418")
            elif candidate_mutation == "unknown_error_class":
                _replace_error_class(report, "transport", "unrecognized")
            elif candidate_mutation == "nonbudget_false_leaf":
                population = report["acceptance"]["phase_plan_evidence"]["state_evidence"]["population"]
                population["timing"]["partial"] = True
            elif candidate_mutation == "partial_work":
                report["partial_work"] = True
                report["runtime_supervisor"]["partial_work"] = True
            elif candidate_mutation == "inflight_unknown":
                report["inflight_unknown"] = True
                report["runtime_supervisor"]["inflight_unknown"] = True
            elif candidate_mutation == "forged_source":
                report["source_git_sha"] = "b" * 40
            elif candidate_mutation == "forged_run":
                report["external_run_id"] = "987654321"
            elif candidate_mutation == "forged_profile":
                report["profile_id"] = "ready-vote-capacity-ramp-v2"
            elif candidate_mutation == "forged_digest":
                report["profile_digest"] = "f" * 64
            elif candidate_mutation == "missing_counts":
                report["raw_http"].pop("status_counts", None)
            elif candidate_mutation == "unrecognized_status1":
                supervisor_reason = "worker_error"
                report["runtime_supervisor"]["reason"] = supervisor_reason
            elif candidate_mutation == "forged_logical_failure_rate":
                report["logical"]["final_failure_rate_percent"] += 1.0
            elif candidate_mutation == "forged_raw_failure_rate":
                report["raw_http"]["final_failure_rate_percent"] += 1.0
            elif candidate_mutation == "forged_phase_failure_rate":
                phase_summaries = report.get("phases")
                self.assertTrue(_mutate_first_failure_rate(phase_summaries, 99.0))
            elif candidate_mutation == "boolean_failure_rate":
                report["logical"]["final_failure_rate_percent"] = True
            elif candidate_mutation == "nonfinite_failure_rate":
                report["raw_http"]["final_failure_rate_percent"] = float("inf")
            elif candidate_mutation == "missing_raw_timing_schema":
                report["raw_http"]["timing"].pop("timing_schema", None)
            elif candidate_mutation == "missing_logical_partial_marker":
                report["logical"]["timing"].pop("partial", None)
            elif candidate_mutation == "missing_phase_timing_schema":
                self.assertTrue(
                    _remove_first_timing_marker(report.get("phases"), "timing_schema")
                )
            elif candidate_mutation == "missing_phase_partial_marker":
                self.assertTrue(
                    _remove_first_timing_marker(report.get("phases"), "partial")
                )
            elif candidate_mutation == "unrelated_phase_budget":
                raw_phases = report.get("phases", {})
                status_phases = {
                    name
                    for name, phase in raw_phases.items()
                    if isinstance(phase, dict)
                    and isinstance(phase.get("raw_http"), dict)
                    and phase["raw_http"].get("status_counts", {}).get("0", 0)
                }
                target_phase = next(
                    name for name in report["acceptance"]["phase_budget_evidence"]
                    if name not in status_phases
                )
                for phase_mapping in (
                    report["acceptance"]["phase_budget_evidence"],
                    report["acceptance"]["phase_plan_evidence"]["phase_budgets"],
                ):
                    phase_mapping[target_phase]["checks"]["logical_timing_complete"] = False
                    phase_mapping[target_phase]["passed"] = False
            report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
            if not candidate_debug:
                candidate_debug.update(
                    {
                    "binding": platform_load._report_binding(profile, report),
                    "ramp": platform_load._authored_ramp_budget_checks(profile, report["acceptance"]),
                    "observer_closed": platform_load._closed_missing_observer_binding(
                        report["acceptance"].get("observer_binding")
                    ),
                    "observer_binding": report["acceptance"].get("observer_binding"),
                    "phase_evidence_equal": report["acceptance"].get(
                        "phase_plan_evidence", {}
                    ).get("phase_budgets") == report["acceptance"].get(
                        "phase_budget_evidence"
                    ),
                    "budget_phases": sorted(platform_load._profile_budget_phase_names(profile)),
                    "scenario_phase_names": [
                        phase.get("name")
                        for phase in profile.get("traffic", {}).get("phases", [])
                    ],
                    "phase_slo_names": sorted(
                        report["acceptance"].get("phase_slo", {})
                    ),
                    "raw_http": report.get("raw_http"),
                    "logical": report.get("logical"),
                    "acceptance_kind": profile.get("acceptance", {}).get("kind"),
                    "origin_observability_present": "origin_observability" in report,
                    "origin_safety": report["acceptance"].get("origin_safety"),
                    "budget_only": platform_load._acceptance_failure_is_budget_only(
                        report["acceptance"], "slo", allow_no_budget_failure=True,
                        allow_pending_observer_binding=True,
                    ),
                    "false_paths": _false_paths(report["acceptance"]),
                    "pending": platform_load._is_closed_pending_origin_candidate(
                        profile,
                        SimpleNamespace(
                            returncode=worker_exit, report=report, worker_started=True,
                            worker_exited=True, killed=False, signal=None, reason="none",
                            partial_work=False, inflight_unknown=False, descendants_reaped=True,
                            isolation=PID_NAMESPACE_ISOLATION, namespace_closed=True,
                        ),
                    ),
                    }
                )
            return SimpleNamespace(
                returncode=worker_exit,
                report=report,
                worker_started=True,
                worker_exited=True,
                killed=False,
                signal=None,
                reason=supervisor_reason,
                partial_work=False,
                inflight_unknown=False,
                descendants_reaped=True,
                isolation=PID_NAMESPACE_ISOLATION,
                namespace_closed=True,
            )

        load_step = _run_step("load-client", "Run checked-out external HTTP load client")
        evaluate_step = _run_step("evaluate-load", "Evaluate checked-out load report")
        sanitize_step = _run_step("evaluate-load", "Sanitize external evidence")
        publish_step = next(
            step
            for step in yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))["jobs"]["evaluate-load"]["steps"]
            if step.get("name") == "Publish external load evidence"
        )
        enforce_step = _run_step("evaluate-load", "Enforce external load and exact cleanup gates")

        with tempfile.TemporaryDirectory(prefix="external-load-pending-origin-") as temporary:
            root = Path(temporary)
            report_path = root / "external-load.json"
            manifest_path = root / "manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            os_env = {
                "SOURCE_GIT_SHA": SOURCE_SHA,
                "APP_TARGET_SHA": app_target_sha,
                "SOURCE_BINDING_BASE64": source_binding_b64,
                "SOURCE_BINDING_SHA256": source_binding_digest or "",
                "GITHUB_RUN_ID": RUN_ID,
                "GITHUB_RUN_ATTEMPT": "1",
            }
            request_transport = (
                patch("tools.platform_external_load.urlopen", side_effect=urlopen)
                if transport_failure
                else patch("tools.platform_external_load._request", side_effect=request)
            )
            with (
                patch("tools.platform_load.get_profile", return_value=profile),
                patch("tools.platform_load_runtime.require_pid_namespace_capability", return_value=None),
                patch("tools.platform_load_runtime.run_supervised", side_effect=supervised_worker),
                patch("tools.platform_external_load.load_manifest", return_value=(manifest, users)),
                patch("tools.platform_external_load._trace", return_value={"status": "200", "ip": "192.0.2.10", "colo": "TEST"}),
                request_transport,
                patch("socket.socket.connect", side_effect=AssertionError("network access is forbidden in this test")),
                patch.dict(os.environ, os_env, clear=False),
            ):
                with redirect_stdout(io.StringIO()):
                    candidate_exit = platform_load.main(
                        [
                            "run", "--profile", str(profile["profile_id"]),
                            "--manifest", str(manifest_path), "--report-path", str(report_path),
                            "--defer-pending-origin",
                        ]
                    )
                    standalone_exit = platform_load.main(
                        [
                            "run", "--profile", str(profile["profile_id"]),
                            "--manifest", str(manifest_path),
                            "--report-path", str(root / "standalone.json"),
                        ]
                    )
            self.assertEqual(standalone_exit, 1)
            candidate = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                candidate_exit,
                expected_candidate_exit,
                json.dumps(
                    {
                        "debug": candidate_debug,
                    },
                    sort_keys=True,
                    default=repr,
                ),
            )
            if candidate_mutation is not None:
                return None
            if source_binding is not None:
                canonical_digest = hashlib.sha256(
                    json.dumps(
                        source_binding,
                        ensure_ascii=True,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("ascii")
                ).hexdigest()
                self.assertEqual(candidate["source_git_sha"], SOURCE_SHA)
                self.assertEqual(candidate["app_target_sha"], app_target_sha)
                self.assertEqual(candidate["source_binding"], source_binding)
                self.assertEqual(candidate["source_binding_sha256"], canonical_digest)
            self.assertTrue(candidate["acceptance"]["pending_origin_evidence"])
            self.assertFalse(candidate["acceptance"]["passed"])
            if transport_failure:
                self.assertEqual(transport_failures, 1)
                self.assertFalse(candidate["acceptance"]["contract_ok"])
                self.assertEqual(candidate["logical"]["actions"], 2)
                self.assertEqual(candidate["raw_http"]["status_counts"].get("0"), 1)
                self.assertEqual(
                    candidate["raw_http"]["error_kinds"], {"transport": 1}
                )

            # Execute the exact candidate-state Bash branch from the workflow.
            status_branch = load_step[load_step.index("client_exit_status=\"$load_status\""):load_step.index("if [[ \"$TIMEOUT_DIAGNOSTICS\" == true ]]; then", load_step.index("client_exit_status=\"$load_status\""))]
            status_shell = "\n".join(
                (
                    'TIMEOUT_DIAGNOSTICS=false', 'load_status=3',
                    'report_ready=1', status_branch,
                    'printf "%s\\n" "$load_status:$client_exit_status:$candidate_state:$report_ready"',
                )
            )
            state_result = subprocess.run(
                ["/bin/bash", "-euo", "pipefail", "-c", status_shell],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(state_result.returncode, 0, state_result.stderr)
            self.assertEqual(state_result.stdout.strip(), "0:3:pending_origin:1")

            # Use the actual YAML receipt writer and downstream receipt
            # verifier so exit 3 is bound to this report/run/source.
            artifact_dir = root / "candidate-artifacts"
            artifact_dir.mkdir()
            receipt_path = artifact_dir / "load-status.json"
            input_path = root / "external-input.json"
            input_payload: dict[str, object] = {
                "schema": 2 if source_binding is not None else 1,
                "target_sha": SOURCE_SHA,
            }
            if source_binding is not None:
                input_payload["source_binding"] = source_binding
            input_path.write_text(json.dumps(input_payload) + "\n", encoding="ascii")
            receipt_script = _python_block(
                load_step,
                '/usr/bin/python3 - "$artifact_dir/load-status.json"',
            )
            receipt_write = subprocess.run(
                [
                    sys.executable, "-c", receipt_script,
                    str(receipt_path), "0", "3", "pending_origin", "1",
                    SOURCE_SHA, app_target_sha, source_binding_digest or "", RUN_ID, "1",
                    str(profile["profile_id"]), str(report_path), str(input_path),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(receipt_write.returncode, 0, receipt_write.stderr)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["report_sha256"], hashlib.sha256(report_path.read_bytes()).hexdigest())
            self.assertEqual(receipt["runner_sha"], SOURCE_SHA)
            self.assertEqual(receipt["app_target_sha"], app_target_sha)
            self.assertEqual(receipt["source_binding"], source_binding)
            self.assertEqual(receipt["source_binding_sha256"], source_binding_digest)
            receipt_script = _python_block(
                evaluate_step,
                'validation_kind="$(/usr/bin/python3 - "$load_status_file"',
            )
            receipt_check = subprocess.run(
                [
                    sys.executable, "-c", receipt_script,
                    str(receipt_path), str(report_path), str(input_path),
                    SOURCE_SHA, app_target_sha, RUN_ID, "1",
                    str(profile["profile_id"]), "false",
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(receipt_check.returncode, 0, receipt_check.stderr)

            # Attach origin using the real evaluator CLI and require a reserved
            # workflow-only exit for the completed SLO miss.
            observer_path = root / "observer.json"
            observer_path.write_text(
                json.dumps(_observer(marker=report_marker, run_id=RUN_ID)) + "\n",
                encoding="utf-8",
            )
            with (
                patch("tools.platform_load.get_profile", return_value=profile),
                patch.dict(os.environ, os_env, clear=False),
            ):
                with redirect_stdout(io.StringIO()):
                    evaluation_exit = platform_load.main(
                        [
                            "evaluate", "--profile", str(profile["profile_id"]),
                            "--report", str(report_path), "--server-observability", str(observer_path),
                            "--defer-completed-slo-failure",
                        ]
                    )
            self.assertEqual(evaluation_exit, 3)
            evaluated = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(evaluated["acceptance"]["passed"])
            expected_decision = {
                "slo": "SLO FAIL",
                "capacity": "CAPACITY EXPERIMENT COMPLETE TARGET FAIL",
            }[profile["acceptance"]["kind"]]
            self.assertEqual(evaluated["acceptance"]["decision"], expected_decision)
            self.assertTrue(evaluated["report_binding"]["complete"])
            self.assertEqual(evaluated["source_git_sha"], SOURCE_SHA)
            self.assertEqual(evaluated["app_target_sha"], app_target_sha)
            self.assertEqual(evaluated["source_binding"], source_binding)
            self.assertEqual(
                evaluated["source_binding_sha256"], source_binding_digest
            )

            # Run the actual YAML sanitizer over the exact public evidence set.
            runner_temp = root / "runner"
            client = runner_temp / "external-client"
            evidence = runner_temp / "external-evidence"
            client.mkdir(parents=True)
            evidence.mkdir(parents=True)
            (client / "external-load.json").write_bytes(report_path.read_bytes())
            (client / "client-raw.log").write_text("CLIENT_RESULT status=complete\n", encoding="utf-8")
            (evidence / "server-observability.json").write_text(observer_path.read_text(encoding="utf-8"), encoding="utf-8")
            (evidence / "timeout-diagnostics.json").write_text('{"schema":1,"enabled":false}\n', encoding="utf-8")
            (evidence / "matrix-summary.json").write_text('{"schema":1,"passed":true}\n', encoding="utf-8")
            (evidence / "cleanup-summary.json").write_text('{"schema":1,"ok":true}\n', encoding="utf-8")
            (evidence / "canonical.log").write_text("FINALIZE status=success\n", encoding="utf-8")
            (evidence / "cleanup-canonical.log").write_text("CLEANUP status=success\n", encoding="utf-8")
            github_output = root / "github-output"
            github_output.touch()
            sanitized = subprocess.run(
                ["/bin/bash", "-e", "-o", "pipefail", "-c", sanitize_step],
                cwd=REPOSITORY_ROOT,
                env={**os.environ, "RUNNER_TEMP": str(runner_temp), "GITHUB_OUTPUT": str(github_output), "TIMEOUT_DIAGNOSTICS": "false"},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(sanitized.returncode, 0, sanitized.stderr)
            self.assertIn("sanitizer_status=0", github_output.read_text(encoding="utf-8"))

            upload_members = [
                Path(value.replace("${{ runner.temp }}/", ""))
                for value in publish_step["with"]["path"].splitlines()
            ]
            self.assertIn(Path("external-evidence/cleanup-canonical.log"), upload_members)
            upload_zip = root / "public-evidence.zip"
            with zipfile.ZipFile(upload_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for member in upload_members:
                    source = runner_temp / member
                    self.assertTrue(source.is_file(), str(member))
                    archive.write(source, member.as_posix())
            with zipfile.ZipFile(upload_zip) as archive:
                self.assertEqual(set(archive.namelist()), {member.as_posix() for member in upload_members})

            # The final gate must still fail on the completed budget miss.
            gate = re.sub(
                r"\$\{\{\s*(.*?)\s*\}\}",
                lambda match: (
                    "slo_failed" if match.group(1).endswith("acceptance_status")
                    else "pending_origin" if match.group(1).endswith("candidate_state")
                    else "1" if match.group(1).endswith(("report_ready", "observer_ready"))
                    else "success" if match.group(1).endswith(".result")
                    else "0"
                ),
                enforce_step,
            )
            final_gate = subprocess.run(
                ["/bin/bash", "-euo", "pipefail", "-c", gate],
                cwd=REPOSITORY_ROOT,
                env={**os.environ, "BASH_ENV": "/dev/null"},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(final_gate.returncode, 1)
            self.assertIn("profile-budget", final_gate.stderr)

            sanitized_report = json.loads(
                (client / "external-load.json").read_text(encoding="utf-8")
            )
            if source_binding is not None:
                self.assertEqual(sanitized_report["source_git_sha"], SOURCE_SHA)
                self.assertEqual(sanitized_report["app_target_sha"], app_target_sha)
                self.assertEqual(sanitized_report["source_binding"], source_binding)
                self.assertEqual(
                    sanitized_report["source_binding_sha256"], source_binding_digest
                )

            # The actual evaluator stays fail-closed for a forged source or
            # incomplete origin, despite the workflow-only completion mode.
            forged = deepcopy(evaluated)
            forged["source_git_sha"] = "b" * 40
            forged_path = root / "forged.json"
            forged_path.write_text(json.dumps(forged), encoding="utf-8")
            with (
                patch("tools.platform_load.get_profile", return_value=profile),
                patch.dict(os.environ, os_env, clear=False),
            ):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        platform_load.main(
                            ["evaluate", "--profile", str(profile["profile_id"]), "--report", str(forged_path), "--server-observability", str(observer_path), "--defer-completed-slo-failure"]
                        ),
                        1,
                    )
            return {
                "candidate": candidate,
                "receipt": receipt,
                "evaluated": evaluated,
                "sanitized": sanitized_report,
                "final_gate_exit": final_gate.returncode,
            }


if __name__ == "__main__":
    unittest.main()
