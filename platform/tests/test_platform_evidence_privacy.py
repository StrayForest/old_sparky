"""Adversarial checks for load, observer and live-QA evidence boundaries."""

from __future__ import annotations

from io import BytesIO, StringIO
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from tools.platform_evidence_sanitizer import (
    _capacity_claim_has_complete_inputs,
    _project_capacity_attribution,
    _project_runner_cgroup_usage,
    project_public_artifact,
    safe_error_class,
    sanitized_log_summary,
)
from tools.platform_external_load import RequestResult, summarize_results
from tools.platform_external_load_observer import (
    _public_timeline_sample_row,
    _safe_postgres_wait_snapshot,
    _safe_process_lifecycle,
    postgres_statement_delta,
    summarize_ssr_observability,
)
from tools.platform_abort_retained_load import export_abort_evidence
from tools.platform_live_launch_report import sanitize_live_launch_lines
from tools.platform_production_qa import (
    _safe_qa_scenario_detail,
    cli_report_summary,
    response_diagnostics,
)


FORBIDDEN_VALUES = (
    "operator@example.test",
    "Authorization Bearer secret-token",
    "session=secret-session",
    "csrf=secret-csrf",
    "password=secret-password",
    "198.51.100.42",
    "https://old-sparky.example/invite/INVITE-CODE?token=secret-token",
    "INVITE-CODE",
    "SELECT email FROM users WHERE password='secret-password'",
    "/home/operator/private-report.json",
)


def serialized(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


class EvidencePrivacyTests(unittest.TestCase):
    def test_capacity_attribution_projection_preserves_closed_claim_and_unknowns(self) -> None:
        eligible = {
            "schema": 1, "status": "eligible", "reason": "headroom_observed",
            "load_model": "closed_loop_concurrency_ramp", "server_capacity_claim": None,
            "capacity_attribution_eligible": True,
            "server_capacity_claim_status": "unknown",
            "server_capacity_claim_reason": "resource_correlation_not_provided",
            "capacity_scope": "generator_headroom_only",
            "effective_cpu_capacity_cores": 2.0, "max_process_cpu_percent": 125.0,
            "max_cgroup_cpu_usage_cores": 0.5,
            "max_visible_throttled_periods_delta": 0, "offered_actions": 100, "started_actions": 100,
            "client_queue_lag_floor_ms": 2.0, "executor_queue_p95_ms": 0.5,
            "executor_queue_p99_ms": 0.8, "schedule_delay_p95_ms": None,
            "schedule_delay_p99_ms": None,
            "private_path": "/private/report.json",
        }
        self.assertEqual(
            _project_capacity_attribution(eligible),
            {
                "schema": 1, "status": "eligible", "reason": "headroom_observed",
                "server_capacity_claim": None, "capacity_scope": "generator_headroom_only",
                "capacity_attribution_eligible": True,
                "server_capacity_claim_status": "unknown",
                "server_capacity_claim_reason": "resource_correlation_not_provided",
                "load_model": "closed_loop_concurrency_ramp",
                "effective_cpu_capacity_cores": 2.0, "max_process_cpu_percent": 125.0,
                "max_cgroup_cpu_usage_cores": 0.5,
                "offered_actions": 100, "started_actions": 100,
                "max_visible_throttled_periods_delta": 0,
                "client_queue_lag_floor_ms": 2.0, "executor_queue_p95_ms": 0.5,
                "executor_queue_p99_ms": 0.8,
            },
        )
        unknown = {
            "schema": 1, "status": "unknown", "reason": "effective_generator_quota_unknown",
            "load_model": "open_loop_scheduled_rate", "server_capacity_claim": None,
            "capacity_attribution_eligible": None,
            "server_capacity_claim_status": "unknown",
            "server_capacity_claim_reason": "resource_correlation_not_provided",
            "capacity_scope": "generator_headroom_only",
        }
        self.assertEqual(_project_capacity_attribution(unknown)["status"], "unknown")
        forged = copy.deepcopy(eligible)
        forged["capacity_scope"] = "server_capacity_proven"
        self.assertIsNone(_project_capacity_attribution(forged))
        forged_claim = copy.deepcopy(eligible)
        forged_claim["server_capacity_claim"] = True
        self.assertIsNone(_project_capacity_attribution(forged_claim))
        forged_eligibility = copy.deepcopy(eligible)
        forged_eligibility["capacity_attribution_eligible"] = False
        self.assertIsNone(_project_capacity_attribution(forged_eligibility))
        legacy_eligible = copy.deepcopy(eligible)
        for key in ("capacity_attribution_eligible", "server_capacity_claim_status", "server_capacity_claim_reason"):
            legacy_eligible.pop(key)
        self.assertIsNone(_project_capacity_attribution(legacy_eligible))

        report = {
            "capacity_attribution": eligible,
            "arrival_model": "closed_loop_concurrency_ramp",
            "logical": {"timing": {
                "expected_count": 10, "submitted_count": 10, "started_count": 10,
                "completed_count": 10, "scheduled_count": 10,
                "response_completion_count": 10, "user_observed_count": 10,
                "dropped_work": 0, "late_start_count": 0, "partial": False,
                "executor_queue_wait": {"count": 10, "p95_ms": 0.5, "p99_ms": 0.8, "max_ms": 0.8},
            }},
            "client_resource_usage": {
                "schema": 1, "status": "complete", "scope": "load_worker_process",
                "cpu_capacity_status": "unknown", "cpu_capacity_reason": "trusted_host_root_unproven",
                "sample_count": 0, "samples": [],
                "cgroup_diagnostics": {
                    "schema": 1, "status": "complete", "visible_evidence_status": "complete",
                    "quota_completeness": "unknown", "visible_ancestor_count": 0,
                    "visible_ancestors": [], "reasons": ["trusted_host_root_unproven"],
                },
            },
        }
        projected = project_public_artifact("external_load", report)
        self.assertEqual(projected["capacity_attribution"]["status"], "unknown")
        self.assertEqual(projected["capacity_attribution"]["reason"], "effective_generator_quota_unknown")
        self.assertIsNone(projected["capacity_attribution"]["server_capacity_claim"])

        complete_usage = {
            "status": "complete", "cpu_capacity_status": "known",
            "cpu_capacity_reason": "trusted_host_root_unproven",
            "effective_cpu_capacity_cores": 2.0, "sample_count": 1,
            "samples": [{"process_cpu_percent": 125.0}],
            "cgroup_diagnostics": {
                "status": "complete", "quota_completeness": "unknown",
                "visible_ancestors": [{
                    "cpu_stat_completeness": "complete", "cpu_stat_delta": {"nr_throttled": 0},
                }],
            },
        }
        runner_usage = {
            "schema": 1, "status": "complete", "reason": "none",
            "runner_environment": "github-hosted", "sample_count": 3,
            "window_seconds": 10.0, "effective_cpu_capacity_cores": 2.0,
            "max_cgroup_cpu_usage_cores": 0.5, "max_visible_throttled_periods_delta": 0,
            "identity": {
                "outer_pid_namespace_matches_pid1": True,
                "outer_mount_namespace_matches_pid1": True,
                "outer_cgroup_namespace_matches_pid1": True,
                "target_cgroup_namespace_matches_outer": True,
                "target_mount_namespace_is_isolated": True,
                "target_pid_namespace_is_isolated": True,
                "global_cgroup2_mount": True,
                "workload_membership_matches_outer": True,
            },
        }
        projected_eligible = _project_capacity_attribution(eligible)
        self.assertIsNotNone(projected_eligible)
        projected_runner = _project_runner_cgroup_usage(runner_usage)
        self.assertEqual(projected_runner["status"], "complete")
        complete_report = copy.deepcopy(report)
        for name in (
            "expected_count", "submitted_count", "started_count", "completed_count",
            "scheduled_count", "response_completion_count", "user_observed_count",
        ):
            complete_report["logical"]["timing"][name] = 100  # type: ignore[index]
        complete_report["logical"]["timing"]["executor_queue_wait"]["count"] = 100  # type: ignore[index]
        complete_report["logical"]["timing"]["executor_queue_wait"]["p95_ms"] = 0.5  # type: ignore[index]
        complete_report["logical"]["timing"]["executor_queue_wait"]["p99_ms"] = 0.8  # type: ignore[index]
        complete_report["logical"]["timing"]["executor_queue_wait"]["max_ms"] = 0.8  # type: ignore[index]
        self.assertTrue(_capacity_claim_has_complete_inputs(complete_report, complete_usage, projected_runner, projected_eligible))
        mismatched_claim = copy.deepcopy(projected_eligible)
        mismatched_claim["max_process_cpu_percent"] = 126.0
        self.assertFalse(_capacity_claim_has_complete_inputs(complete_report, complete_usage, projected_runner, mismatched_claim))
        lagged = copy.deepcopy(complete_report)
        lagged["logical"]["timing"]["executor_queue_wait"]["p95_ms"] = 2.5  # type: ignore[index]
        lagged["logical"]["timing"]["executor_queue_wait"]["max_ms"] = 3.0  # type: ignore[index]
        self.assertFalse(_capacity_claim_has_complete_inputs(lagged, complete_usage, projected_runner, projected_eligible))

        open_loop_forgery = copy.deepcopy(complete_report)
        open_loop_forgery["arrival_model"] = "open_loop_scheduled_rate"
        open_loop_forgery["logical"]["timing"]["schedule_delay"] = {"p95_ms": 2.5, "p99_ms": 2.5}  # type: ignore[index]
        open_loop_claim = copy.deepcopy(eligible)
        open_loop_claim["load_model"] = "open_loop_scheduled_rate"
        open_loop_claim["schedule_delay_p95_ms"] = 2.5
        open_loop_claim["schedule_delay_p99_ms"] = 2.5
        open_loop_claim["capacity_scope"] = "generator_headroom_only"
        open_loop_forgery["capacity_attribution"] = open_loop_claim
        projected_open_loop = project_public_artifact("external_load", open_loop_forgery)
        self.assertEqual(projected_open_loop["capacity_attribution"]["status"], "unknown")
        self.assertIsNone(projected_open_loop["capacity_attribution"]["server_capacity_claim"])

    def test_external_load_source_identity_is_closed_and_digest_bound(self) -> None:
        binding: dict[str, object] = {
            "schema": 1,
            "binding_mode": "verified-noop",
            "runner_sha": "a" * 40,
            "app_target_sha": "b" * 40,
            "baseline_identity": {
                "schema": 1,
                "source_sha": "b" * 40,
                "release_slug": "gha-123456-1-bbbbbbbbbbbb",
                "release_json_sha256": "c" * 64,
                "current_link_dev": 100,
                "current_link_ino": 101,
                "release_dev": 100,
                "release_ino": 102,
                "pending_operation": False,
            },
            "receipt_document_sha256": "d" * 64,
            "receipt_artifact_id": "456789",
            "receipt_artifact_name": "platform-production-noop-source-receipt-123456-2",
            "receipt_artifact_digest": "sha256:" + "e" * 64,
            "receipt_archive_sha256": "e" * 64,
            "cumulative_manifest_sha256": "f" * 64,
            "source_security_run_id": "234567",
            "source_security_run_attempt": "1",
            "autodeploy_run_id": "345678",
            "autodeploy_run_attempt": "3",
            "production_deploy_run_id": "123456",
            "production_deploy_run_attempt": "2",
        }
        canonical = json.dumps(
            binding, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        digest = hashlib.sha256(canonical).hexdigest()
        source: dict[str, object] = {
            "source_git_sha": "a" * 40,
            "app_target_sha": "b" * 40,
            "source_binding": binding,
            "source_binding_sha256": digest,
            "report_binding": {
                "checks": {
                    "source_git_sha": True,
                    "app_target_sha": True,
                    "source_binding": True,
                    "source_binding_sha256": True,
                }
            },
            "acceptance": {"passed": False, "decision": "SLO FAIL"},
        }

        projected = project_public_artifact("external_load", source)
        self.assertEqual(projected["source_git_sha"], "a" * 40)
        self.assertEqual(projected["app_target_sha"], "b" * 40)
        self.assertEqual(projected["source_binding"], binding)
        self.assertEqual(projected["source_binding_sha256"], digest)
        self.assertFalse(projected["acceptance"]["passed"])

        same_source = {
            **source,
            "app_target_sha": "a" * 40,
            "source_binding": None,
            "source_binding_sha256": None,
        }
        same_source["report_binding"] = {
            "checks": {
                "source_git_sha": True,
                "app_target_sha": True,
                "source_binding": True,
                "source_binding_sha256": True,
            }
        }
        projected_same_source = project_public_artifact("external_load", same_source)
        self.assertEqual(projected_same_source["source_git_sha"], "a" * 40)
        self.assertEqual(projected_same_source["app_target_sha"], "a" * 40)
        self.assertIsNone(projected_same_source["source_binding"])

        mutations: list[dict[str, object]] = []
        forged_app = copy.deepcopy(source)
        forged_app["app_target_sha"] = "a" * 40
        mutations.append(forged_app)
        bad_digest = copy.deepcopy(source)
        bad_digest["source_binding_sha256"] = "0" * 64
        mutations.append(bad_digest)
        for field, value in (
            ("unknown", "extension"),
            ("app_target_sha", "a" * 40),
            ("receipt_artifact_id", True),
        ):
            invalid_binding = copy.deepcopy(binding)
            invalid_binding[field] = value
            invalid_source = copy.deepcopy(source)
            invalid_source["source_binding"] = invalid_binding
            invalid_source["source_binding_sha256"] = hashlib.sha256(
                json.dumps(
                    invalid_binding,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
            ).hexdigest()
            mutations.append(invalid_source)
        false_check = copy.deepcopy(source)
        false_check["report_binding"]["checks"]["source_binding"] = False  # type: ignore[index]
        mutations.append(false_check)
        for index, invalid_source in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                project_public_artifact("external_load", invalid_source)

        # The old report schema may contain only runner provenance. It remains
        # omitted instead of exposing an unverified identity value.
        legacy = project_public_artifact(
            "external_load",
            {"source_git_sha": "a" * 40, "report_binding": {"checks": {"run_identity": True}}},
        )
        self.assertNotIn("source_git_sha", legacy)

    def test_external_load_cpu_usage_projection_requires_complete_in_window_samples(self) -> None:
        base: dict[str, object] = {
            "started_at": "2026-10-11T10:00:00Z",
            "finished_at": "2026-10-11T10:00:03Z",
            "phase_windows": [
                {
                    "phase": "read_burst",
                    "started_at": "2026-10-11T10:00:00.5Z",
                    "finished_at": "2026-10-11T10:00:02.5Z",
                }
            ],
            "client_resource_usage": {
                "schema": 1,
                "status": "complete",
                "scope": "load_worker_process",
                "sample_interval_ms": 1000,
                "affinity_cpu_count": 4,
                "effective_cpu_capacity_cores": 2.0,
                "cpu_capacity_reason": "known",
                "cpu_capacity_status": "known",
                "sample_count": 1,
                "samples": [
                    {
                        "interval_started_at": "2026-10-11T10:00:01Z",
                        "timestamp": "2026-10-11T10:00:02Z",
                        "interval_elapsed_ms": 1000.0,
                        "process_cpu_percent": 135.5,
                        "pid": 1234,
                        "request_id": "private-id",
                    }
                ],
            },
        }
        projected = project_public_artifact("external_load", base)
        self.assertEqual(projected["run_window_status"], "complete")
        self.assertEqual(projected["phase_windows_status"], "complete")
        usage = projected["client_resource_usage"]
        self.assertEqual(usage["status"], "complete")
        self.assertEqual(usage["cpu_capacity_status"], "known")
        self.assertEqual(usage["cpu_capacity_reason"], "known")
        self.assertEqual(usage["effective_cpu_capacity_cores"], 2.0)
        self.assertEqual(usage["sample_count"], 1)
        serialized_usage = serialized(usage)
        self.assertNotIn("private-id", serialized_usage)
        self.assertNotIn("1234", serialized_usage)

        outside = copy.deepcopy(base)
        outside["client_resource_usage"]["samples"][0]["interval_started_at"] = "2026-10-11T09:59:59Z"  # type: ignore[index]
        outside_projection = project_public_artifact("external_load", outside)
        self.assertEqual(outside_projection["client_resource_usage"]["status"], "incomplete")
        self.assertEqual(outside_projection["client_resource_usage"]["samples"], [])
        self.assertEqual(outside_projection["client_resource_usage"]["sample_count"], 0)

        malformed_phase = copy.deepcopy(base)
        malformed_phase["phase_windows"] = [
            {
                "phase": "read_burst",
                "started_at": "2026-10-11T10:00:02Z",
                "finished_at": "2026-10-11T10:00:01Z",
            }
        ]
        self.assertEqual(
            project_public_artifact("external_load", malformed_phase)["phase_windows_status"],
            "invalid",
        )

        legacy = project_public_artifact("external_load", {"status": "complete"})
        self.assertEqual(legacy["run_window_status"], "unavailable")
        self.assertEqual(legacy["client_resource_usage"]["status"], "unavailable")
        self.assertEqual(legacy["client_resource_usage"]["cpu_capacity_status"], "unknown")
        self.assertNotIn("effective_cpu_capacity_cores", legacy["client_resource_usage"])

        inconsistent_capacity = copy.deepcopy(base)
        inconsistent_capacity["client_resource_usage"]["cpu_capacity_status"] = "unknown"  # type: ignore[index]
        inconsistent_capacity["client_resource_usage"]["cpu_capacity_reason"] = "cpu_max_missing"  # type: ignore[index]
        inconsistent = project_public_artifact("external_load", inconsistent_capacity)
        self.assertEqual(
            inconsistent["client_resource_usage"]["cpu_capacity_status"], "unknown"
        )
        self.assertNotIn(
            "effective_cpu_capacity_cores", inconsistent["client_resource_usage"]
        )
        self.assertEqual(
            inconsistent["client_resource_usage"]["cpu_capacity_reason"],
            "cpu_max_missing",
        )

        incomplete_quota_visibility = copy.deepcopy(inconsistent_capacity)
        incomplete_quota_visibility["client_resource_usage"]["cpu_capacity_reason"] = (
            "trusted_host_root_unproven"
        )  # type: ignore[index]
        incomplete_projection = project_public_artifact(
            "external_load", incomplete_quota_visibility
        )
        self.assertEqual(
            incomplete_projection["client_resource_usage"]["cpu_capacity_status"],
            "unknown",
        )
        self.assertEqual(
            incomplete_projection["client_resource_usage"]["cpu_capacity_reason"],
            "trusted_host_root_unproven",
        )

        malformed_capacity_reason = copy.deepcopy(inconsistent_capacity)
        malformed_capacity_reason["client_resource_usage"]["cpu_capacity_reason"] = (
            "/sys/fs/cgroup/private/job/cpu.max"
        )  # type: ignore[index]
        malformed_reason_projection = project_public_artifact(
            "external_load", malformed_capacity_reason
        )
        malformed_usage = malformed_reason_projection["client_resource_usage"]
        self.assertEqual(malformed_usage["cpu_capacity_reason"], "unknown")
        self.assertNotIn(
            "/sys/fs/cgroup", serialized(malformed_reason_projection)
        )

        for status in ("incomplete", "unavailable", "truncated"):
            with self.subTest(status=status):
                partial = copy.deepcopy(base)
                partial["client_resource_usage"]["status"] = status  # type: ignore[index]
                partial_projection = project_public_artifact("external_load", partial)
                self.assertEqual(
                    partial_projection["client_resource_usage"]["status"], status
                )
                self.assertEqual(
                    partial_projection["client_resource_usage"]["sample_count"], 0
                )
                self.assertEqual(
                    partial_projection["client_resource_usage"]["samples"], []
                )

        capped = copy.deepcopy(base)
        capped["client_resource_usage"]["sample_count"] = 4097  # type: ignore[index]
        capped["client_resource_usage"]["samples"] = [{}] * 4097  # type: ignore[index]
        capped_projection = project_public_artifact("external_load", capped)
        self.assertEqual(capped_projection["client_resource_usage"]["status"], "truncated")
        self.assertEqual(capped_projection["client_resource_usage"]["samples"], [])

    def test_external_load_cgroup_diagnostics_are_closed_path_free_and_never_complete_quota(self) -> None:
        report = {
            "client_resource_usage": {
                "status": "unavailable",
                "cpu_capacity_status": "unknown",
                "cpu_capacity_reason": "cpu_max_missing",
                "effective_cpu_capacity_cores": None,
                "sample_count": 0,
                "samples": [],
                "cgroup_diagnostics": {
                    "schema": 1,
                    "status": "complete",
                    "visible_evidence_status": "complete",
                    "quota_completeness": "complete",
                    "namespace_identity_stable": True,
                    "membership_stable": True,
                    "mount_identity_stable": True,
                    "visible_mount_root_is_global": True,
                    "visible_ancestor_count": 1,
                    "reasons": ["trusted_host_root_unproven"],
                    "private_path": "/private/host/path",
                    "visible_ancestors": [
                        {
                            "path": "/sys/fs/cgroup/private/job",
                            "cpu_max_kind": "quota",
                            "cpu_max_quota_us": 99_999_999,
                            "cpu_max_period_us": 100_000_000,
                            "cpu_stat_completeness": "complete",
                            "cpu_stat_delta": {
                                "usage_usec": 10,
                                "user_usec": 5,
                                "system_usec": 5,
                                "nr_periods": 1,
                                "nr_throttled": 0,
                                "throttled_usec": 0,
                            },
                            "host_id": "host-secret",
                            "namespace_inode": 12345,
                        }
                    ],
                    "private_extra": "must not pass through",
                },
            }
        }
        projected = project_public_artifact("external_load", report)
        usage = projected["client_resource_usage"]
        diagnostics = usage["cgroup_diagnostics"]
        self.assertEqual(usage["cpu_capacity_status"], "unknown")
        self.assertEqual(usage["cpu_capacity_reason"], "cpu_max_missing")
        self.assertNotIn("effective_cpu_capacity_cores", usage)
        self.assertEqual(diagnostics["status"], "complete")
        self.assertEqual(diagnostics["quota_completeness"], "unknown")
        self.assertEqual(diagnostics["visible_ancestors"][0]["cpu_max_kind"], "quota")
        self.assertEqual(
            diagnostics["visible_ancestors"][0]["cpu_max_quota_us"], 99_999_999
        )
        self.assertEqual(
            diagnostics["visible_ancestors"][0]["cpu_max_period_us"], 100_000_000
        )
        serialized_report = serialized(projected)
        for private in (
            "/sys/fs/cgroup",
            "/private/host/path",
            "host-secret",
            "namespace_inode",
            "private_extra",
        ):
            self.assertNotIn(private, serialized_report)

        partial = copy.deepcopy(report)
        partial_diagnostics = partial["client_resource_usage"]["cgroup_diagnostics"]
        partial_diagnostics["status"] = "incomplete"  # type: ignore[index]
        partial_diagnostics["visible_evidence_status"] = "incomplete"  # type: ignore[index]
        partial_diagnostics["reasons"].append("cpu_max_unavailable")  # type: ignore[index]
        partial_diagnostics["reasons"].append("cpu_stat_partial")  # type: ignore[index]
        partial_diagnostics["visible_ancestor_count"] = 2  # type: ignore[index]
        partial_diagnostics["visible_ancestors"].append(  # type: ignore[index]
            {
                "cpu_max_kind": "unavailable",
                "cpu_stat_completeness": "partial",
                "cpu_stat_delta": {
                    "usage_usec": 12,
                    "user_usec": 5,
                    "system_usec": 7,
                },
            }
        )
        partial_projection = project_public_artifact("external_load", partial)
        partial_result = partial_projection["client_resource_usage"]["cgroup_diagnostics"]
        self.assertEqual(partial_result["status"], "incomplete")
        self.assertEqual(partial_result["quota_completeness"], "unknown")
        self.assertEqual(partial_result["visible_ancestor_count"], 2)
        self.assertEqual(partial_result["visible_ancestors"][1]["cpu_max_kind"], "unavailable")
        self.assertEqual(
            partial_result["visible_ancestors"][1]["cpu_stat_delta"],
            {"usage_usec": 12, "user_usec": 5, "system_usec": 7},
        )

        reset = copy.deepcopy(partial)
        reset["client_resource_usage"]["cgroup_diagnostics"]["reasons"].append("cpu_stat_reset")  # type: ignore[index]
        reset_result = project_public_artifact("external_load", reset)["client_resource_usage"]["cgroup_diagnostics"]
        self.assertEqual(reset_result["status"], "incomplete")
        self.assertEqual(reset_result["visible_ancestors"], [])

        malformed_partial = copy.deepcopy(partial)
        malformed_partial_row = malformed_partial["client_resource_usage"]["cgroup_diagnostics"]["visible_ancestors"][1]  # type: ignore[index]
        malformed_partial_row["cpu_stat_delta"] = {"usage_usec": 12}
        malformed_partial_result = project_public_artifact("external_load", malformed_partial)["client_resource_usage"]["cgroup_diagnostics"]
        self.assertEqual(malformed_partial_result["status"], "incomplete")
        self.assertEqual(malformed_partial_result["visible_ancestors"], [])
        self.assertIn("diagnostics_invalid", malformed_partial_result["reasons"])

        unavailable_complete = copy.deepcopy(report)
        unavailable_row = unavailable_complete["client_resource_usage"]["cgroup_diagnostics"]["visible_ancestors"][0]  # type: ignore[index]
        unavailable_row["cpu_max_kind"] = "unavailable"
        del unavailable_row["cpu_max_quota_us"]
        del unavailable_row["cpu_max_period_us"]
        unavailable_result = project_public_artifact("external_load", unavailable_complete)["client_resource_usage"]["cgroup_diagnostics"]
        self.assertEqual(unavailable_result["status"], "incomplete")
        self.assertEqual(unavailable_result["visible_ancestors"], [])
        self.assertIn("diagnostics_invalid", unavailable_result["reasons"])

        malformed = copy.deepcopy(report)
        malformed["client_resource_usage"]["cgroup_diagnostics"]["visible_ancestor_count"] = 2  # type: ignore[index]
        malformed_projection = project_public_artifact("external_load", malformed)
        malformed_diagnostics = malformed_projection["client_resource_usage"]["cgroup_diagnostics"]
        self.assertEqual(malformed_diagnostics["status"], "incomplete")
        self.assertEqual(malformed_diagnostics["visible_ancestors"], [])
        self.assertEqual(malformed_diagnostics["quota_completeness"], "unknown")

        malformed_inputs = []
        for location, value in (
            ("reasons", [{}]),
            ("status", {}),
            ("visible_evidence_status", []),
            ("reasons", ["/private/host/path"]),
            ("status", "incomplete"),
            ("reasons", ["cpu_stat_reset"]),
        ):
            candidate = copy.deepcopy(report)
            candidate["client_resource_usage"]["cgroup_diagnostics"][location] = value  # type: ignore[index]
            malformed_inputs.append(candidate)
        malformed_kind = copy.deepcopy(report)
        malformed_kind["client_resource_usage"]["cgroup_diagnostics"]["visible_ancestors"][0]["cpu_max_kind"] = []  # type: ignore[index]
        malformed_inputs.append(malformed_kind)
        for index, candidate in enumerate(malformed_inputs):
            with self.subTest(case=index):
                candidate_projection = project_public_artifact("external_load", candidate)
                candidate_diagnostics = candidate_projection["client_resource_usage"]["cgroup_diagnostics"]
                self.assertEqual(candidate_diagnostics["status"], "incomplete")
                self.assertEqual(candidate_diagnostics["visible_ancestors"], [])

        incomplete_claims = []
        for field in ("cpu_stat_delta",):
            candidate = copy.deepcopy(report)
            del candidate["client_resource_usage"]["cgroup_diagnostics"]["visible_ancestors"][0][field]  # type: ignore[index]
            incomplete_claims.append(candidate)
        for flag in (
            "namespace_identity_stable",
            "membership_stable",
            "mount_identity_stable",
        ):
            candidate = copy.deepcopy(report)
            candidate["client_resource_usage"]["cgroup_diagnostics"][flag] = False  # type: ignore[index]
            incomplete_claims.append(candidate)
            candidate = copy.deepcopy(report)
            del candidate["client_resource_usage"]["cgroup_diagnostics"][flag]  # type: ignore[index]
            incomplete_claims.append(candidate)
        empty_chain = copy.deepcopy(report)
        empty_chain["client_resource_usage"]["cgroup_diagnostics"]["visible_ancestor_count"] = 0  # type: ignore[index]
        empty_chain["client_resource_usage"]["cgroup_diagnostics"]["visible_ancestors"] = []  # type: ignore[index]
        incomplete_claims.append(empty_chain)
        for index, candidate in enumerate(incomplete_claims):
            with self.subTest(complete_claim=index):
                candidate_projection = project_public_artifact("external_load", candidate)
                candidate_diagnostics = candidate_projection["client_resource_usage"]["cgroup_diagnostics"]
                self.assertEqual(candidate_diagnostics["status"], "incomplete")
                self.assertEqual(candidate_diagnostics["visible_ancestors"], [])
                self.assertIn("diagnostics_invalid", candidate_diagnostics["reasons"])

    def test_public_observer_timeline_is_whole_bounded_ordered_and_windowed(self) -> None:
        # SystemSampler emits process groups under `processes`; the observer
        # turns those into the fixed api_process/web_process fields consumed
        # by the public sanitizer. Keep this representative boundary in one
        # owner so a source-shape change cannot silently erase process CPU.
        sampler_row: dict[str, object] = {
            "timestamp": "2026-10-11T10:00:01Z",
            "cpu_per_core_percent": {"cpu0": 12.5, "host-secret": 100},
            "cpu_steal_per_core_percent": {"cpu0": 0.0},
            "postgres_cpu_percent": 4.0,
            "postgres_tcp_connections": {
                "total": 27,
                "established": 9,
                "private_backend_label": "must-not-be-published",
            },
            "processes": {
                "deadlock-api": {
                    "process_count": 2,
                    "cpu_percent": 35.0,
                    "rss_bytes": 1200,
                    "read_bytes_per_second": 10.0,
                    "write_bytes_per_second": 5.0,
                    "pid": 4321,
                },
                "deadlock-web": {
                    "process_count": 1,
                    "cpu_percent": 12.0,
                    "rss_bytes": 2400,
                    "read_bytes_per_second": 2.0,
                    "write_bytes_per_second": 1.0,
                },
                "nginx": {
                    "process_count": 1,
                    "cpu_percent": 7.5,
                    "rss_bytes": 3600,
                    "pid": 5678,
                    "process_name": "nginx-private-name",
                },
            },
            "api_connections": {"established": 4, "total": 5},
        }
        observer_row = _public_timeline_sample_row(sampler_row)
        self.assertEqual(observer_row["nginx_cpu_status"], "known")
        self.assertEqual(observer_row["nginx_process"]["cpu_percent"], 7.5)
        source: dict[str, object] = {
            "started_at": "2026-10-11T10:00:00Z",
            "finished_at": "2026-10-11T10:00:03Z",
            "system": {
                "samples": 2,
                "timeline": [
                    observer_row,
                    {
                        "timestamp": "2026-10-11T10:00:02Z",
                        "cpu_per_core_percent": {"cpu0": 20.0},
                        "cpu_steal_per_core_percent": None,
                        "postgres_cpu_percent": None,
                        "api_connections": None,
                        "api_process": None,
                        "web_process": None,
                        "postgres_waits": None,
                        "celery_backlog": None,
                    },
                ],
            },
        }
        projected = project_public_artifact("server_observability", source)
        system = projected["system"]
        self.assertEqual(system["timeline_status"], "complete")
        self.assertEqual(len(system["timeline"]), 2)
        first_public_row = system["timeline"][0]
        self.assertEqual(first_public_row["processes"]["deadlock-api"]["cpu_percent"], 35.0)
        self.assertEqual(first_public_row["processes"]["deadlock-web"]["cpu_percent"], 12.0)
        self.assertEqual(first_public_row["processes"]["nginx"]["cpu_percent"], 7.5)
        self.assertEqual(first_public_row["nginx_cpu_status"], "known")
        self.assertEqual(first_public_row["api_connections"], {"established": 4, "total": 5})
        self.assertEqual(first_public_row["postgres_tcp_established_connections"], 9)
        self.assertEqual(system["timeline"][1], {
            "timestamp": "2026-10-11T10:00:02+00:00",
            "cpu_per_core_percent": {"cpu0": 20.0},
        })
        no_interval_row = _public_timeline_sample_row(
            {
                "timestamp": "2026-10-11T10:00:02Z",
                "cpu_per_core_percent": {},
                "processes": {
                    "nginx": {
                        "process_count": 1,
                        "cpu_percent": 7.5,
                        "pid": 5678,
                    }
                },
            }
        )
        self.assertEqual(no_interval_row["nginx_cpu_status"], "unknown")
        self.assertIsNone(no_interval_row["nginx_process"])
        unknown_source = copy.deepcopy(source)
        unknown_source["system"]["timeline"][1] = no_interval_row  # type: ignore[index]
        unknown_timeline = project_public_artifact(
            "server_observability", unknown_source
        )["system"]["timeline"]
        self.assertEqual(unknown_timeline[1]["nginx_cpu_status"], "unknown")
        self.assertNotIn("nginx", unknown_timeline[1].get("processes", {}))

        forged_known = copy.deepcopy(source)
        forged_known["system"]["timeline"][0]["cpu_per_core_percent"] = {  # type: ignore[index]
            "host-secret": 100.0
        }
        invalid_known = project_public_artifact(
            "server_observability", forged_known
        )["system"]
        self.assertEqual(invalid_known["timeline_status"], "invalid")
        self.assertNotIn("timeline", invalid_known)

        serialized_timeline = serialized(system["timeline"])
        for forbidden in (
            "host-secret",
            "private-request",
            "4321",
            "pid",
            "private_backend_label",
            "nginx-private-name",
            "must-not-be-published",
            '"postgres_tcp_connections"',
            '"total": 27',
            '"pid": 5678',
        ):
            self.assertNotIn(forbidden, serialized_timeline)

        out_of_window = copy.deepcopy(source)
        out_of_window["system"]["timeline"][1]["timestamp"] = "2026-10-11T10:00:04Z"  # type: ignore[index]
        invalid = project_public_artifact("server_observability", out_of_window)["system"]
        self.assertEqual(invalid["timeline_status"], "invalid")
        self.assertNotIn("timeline", invalid)

        malformed_metric = copy.deepcopy(source)
        malformed_metric["system"]["timeline"][0]["cpu_per_core_percent"]["cpu0"] = float("nan")  # type: ignore[index]
        invalid_metric = project_public_artifact(
            "server_observability", malformed_metric
        )["system"]
        self.assertEqual(invalid_metric["timeline_status"], "invalid")
        self.assertNotIn("timeline", invalid_metric)

        oversized = copy.deepcopy(source)
        oversized["system"]["samples"] = 4097  # type: ignore[index]
        oversized["system"]["timeline"] = source["system"]["timeline"] * 2049  # type: ignore[index]
        truncated = project_public_artifact("server_observability", oversized)["system"]
        self.assertEqual(truncated["timeline_status"], "truncated")
        self.assertNotIn("timeline", truncated)

        full_hour = copy.deepcopy(source)
        full_hour["finished_at"] = "2026-10-11T12:00:00Z"
        full_hour["system"]["samples"] = 3600  # type: ignore[index]
        full_hour["system"]["timeline"] = [  # type: ignore[index]
            {
                "timestamp": f"2026-10-11T{10 + index // 3600:02}:"
                f"{(index % 3600) // 60:02}:{index % 60:02}Z",
                "cpu_per_core_percent": {"cpu0": 30.0},
            }
            for index in range(3600)
        ]
        accepted = project_public_artifact("server_observability", full_hour)["system"]
        self.assertEqual(accepted["timeline_status"], "complete")
        self.assertEqual(len(accepted["timeline"]), 3600)

    def test_server_observability_projects_only_safe_request_perf_sampling_fields(
        self,
    ) -> None:
        report = project_public_artifact(
            "server_observability",
            {
                "server_request_perf_logs": {
                    "scope": "diagnostic_sample",
                    "logged_requests": 3,
                    "request_perf_sampling": {
                        "selection_reason_counts": {
                            "interval": 1,
                            "trigger_and_interval": 1,
                            "operator@example.test": 50,
                        },
                        "annotated_rows": 999,
                        "interval_selected_rows": 999,
                        "sample_interval": 16,
                        "client_identity": "Authorization Bearer secret-token",
                    },
                    "authenticated_read_admission_shed": {
                        "observed": 4,
                        "shed": 2,
                        "not_shed": 1,
                        "unknown": 1,
                    },
                    "request_ids": ["private-request-id"],
                    "raw_route": "/api/v1/tournaments/private-slug/workspace",
                    "by_route": {
                        "tournament_workspace": {
                            "requests": 4,
                            "authenticated_read_admission_shed": {
                                "observed": 4,
                                "shed": 2,
                                "not_shed": 1,
                                "unknown": 1,
                            },
                            "request_ids": ["private-request-id"],
                            "raw_route": "/api/v1/tournaments/private-slug/workspace",
                        },
                        "ready_vote": {
                            "requests": 2,
                            "ready_vote_cpu_pressure_percent": {
                                "sample_count": 2,
                                "avg_percent": 70.5,
                                "p50_percent": 70.5,
                                "p95_percent": 80.05,
                                "p99_percent": 80.41,
                                "max_percent": 80.5,
                            },
                            "ready_vote_count_metrics": {
                                "ready_vote_checkout_count": {
                                    "sample_count": 2,
                                    "avg_count": 2.0,
                                    "p50_count": 2.0,
                                    "p95_count": 2.9,
                                    "p99_count": 2.98,
                                    "max_count": 3.0,
                                },
                            },
                            "legacy_ready_vote_fields_deprecated": True,
                        },
                    },
                    "ready_vote_cpu_pressure_percent": {
                        "sample_count": 2,
                        "avg_percent": 70.5,
                        "p50_percent": 70.5,
                        "p95_percent": 80.05,
                        "p99_percent": 80.41,
                        "max_percent": 80.5,
                    },
                    "ready_vote_count_metrics": {
                        "ready_vote_checkout_count": {
                            "sample_count": 2,
                            "avg_count": 2.0,
                            "p50_count": 2.0,
                            "p95_count": 2.9,
                            "p99_count": 2.98,
                            "max_count": 3.0,
                        },
                    },
                    "legacy_ready_vote_fields_deprecated": True,
                }
            },
        )

        request_perf = report["server_request_perf_logs"]
        self.assertEqual(request_perf["scope"], "diagnostic_sample")
        self.assertEqual(
            request_perf["ready_vote_cpu_pressure_percent"]["max_percent"], 80.5
        )
        self.assertEqual(
            request_perf["ready_vote_count_metrics"]["ready_vote_checkout_count"]["max_count"],
            3.0,
        )
        self.assertIs(request_perf["legacy_ready_vote_fields_deprecated"], True)
        self.assertIs(
            request_perf["by_route"]["ready_vote"]["legacy_ready_vote_fields_deprecated"],
            True,
        )
        self.assertEqual(
            request_perf["request_perf_sampling"],
            {
                "selection_reason_counts": {
                    "interval": 1,
                    "trigger_and_interval": 1,
                },
                "annotated_rows": 2,
                "interval_selected_rows": 2,
                "sample_interval": 16,
            },
        )
        self.assertEqual(
            request_perf["authenticated_read_admission_shed"],
            {
                "observed": 4,
                "shed": 2,
                "not_shed": 1,
                "unknown": 1,
            },
        )
        self.assertEqual(
            request_perf["by_route"]["tournament_workspace"],
            {
                "requests": 4,
                "authenticated_read_admission_shed": {
                    "observed": 4,
                    "shed": 2,
                    "not_shed": 1,
                    "unknown": 1,
                },
            },
        )
        invalid_admission_counts = (
            {"observed": True, "shed": 1, "not_shed": 0, "unknown": 0},
            {"observed": -1, "shed": 0, "not_shed": 0, "unknown": 0},
            {"observed": 1.0, "shed": 1, "not_shed": 0, "unknown": 0},
            {"observed": 1, "shed": 1, "not_shed": 0},
            {"observed": 1, "shed": 1, "not_shed": 0, "unknown": 0, "extra": 0},
            {"observed": 3, "shed": 1, "not_shed": 0, "unknown": 0},
        )
        for invalid_counts in invalid_admission_counts:
            malformed = project_public_artifact(
                "server_observability",
                {
                    "server_request_perf_logs": {
                        "authenticated_read_admission_shed": invalid_counts,
                        "by_route": {
                            "tournament_workspace": {
                                "requests": 1,
                                "authenticated_read_admission_shed": invalid_counts,
                            },
                        },
                    },
                },
            )
            projected_request_perf = malformed["server_request_perf_logs"]
            self.assertNotIn("authenticated_read_admission_shed", projected_request_perf)
            self.assertNotIn(
                "authenticated_read_admission_shed",
                projected_request_perf.get("by_route", {})
                .get("tournament_workspace", {}),
            )
        self.assertNotIn("client_identity", serialized(report))
        self.assertNotIn("private-request-id", serialized(report))
        self.assertNotIn("private-slug", serialized(report))
        for value in FORBIDDEN_VALUES:
            self.assertNotIn(value.lower(), serialized(report).lower())

    def test_live_launch_report_drops_adversarial_values(self) -> None:
        lines = [
            (
                "LIVE_BROWSER_QA_FAILURE test=browser_public "
                "GET /api/v1/tournaments/private-slug/workspace?invite_code=INVITE-CODE "
                "status=500 Authorization Bearer secret-token "
                "Cookie=session=secret-session csrf=secret-csrf "
                "password=secret-password operator@example.test 198.51.100.42 "
                "https://old-sparky.example/invite/INVITE-CODE?token=secret-token "
                "SELECT email FROM users WHERE password='secret-password' "
                "/home/operator/private-report.json"
            )
        ]
        report = sanitize_live_launch_lines(lines)
        output = serialized(report)
        for value in FORBIDDEN_VALUES:
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["tests"][0]["route_class"], "tournament_workspace")
        self.assertEqual(report["tests"][0]["http_status"], 500)

        live_seen: list[int] = []

        def endless_live_lines():
            index = 0
            while True:
                live_seen.append(index)
                yield b"LIVE_BROWSER_QA_SUCCESS test=browser_public\n"
                index += 1

        bounded = sanitize_live_launch_lines(endless_live_lines(), max_lines=2)
        self.assertEqual(live_seen, [0, 1])
        self.assertTrue(bounded["truncated"])
        self.assertEqual(bounded["status"], "truncated")
        self.assertFalse(bounded["success"])

        giant = sanitize_live_launch_lines(
            BytesIO(
                b"LIVE_BROWSER_QA_FAILURE test=browser_public "
                + b"x" * 4096
                + b" operator@example.test Authorization Bearer secret-token"
            ),
            max_line_bytes=64,
        )
        self.assertTrue(giant["truncated"])
        self.assertNotIn("operator@example.test", serialized(giant))
        self.assertNotIn("Authorization Bearer secret-token", serialized(giant))

        text_giant = sanitize_live_launch_lines(
            StringIO("ordinary output " + "x" * 4096 + " operator@example.test"),
            max_line_bytes=64,
        )
        self.assertTrue(text_giant["truncated"])
        self.assertNotIn("operator@example.test", serialized(text_giant))

        invalid_utf8 = sanitize_live_launch_lines(BytesIO(b"\xff\xfe\xfa"))
        self.assertFalse(invalid_utf8["truncated"])
        self.assertEqual(invalid_utf8["line_count"], 1)

        def broken_live_stream():
            raise RuntimeError("operator@example.test Authorization Bearer secret-token")
            yield b"unreachable"

        broken = sanitize_live_launch_lines(broken_live_stream())
        self.assertEqual(broken["status"], "error")
        self.assertTrue(broken["truncated"])
        self.assertNotIn("operator@example.test", serialized(broken))

    def test_generic_canonical_log_sanitizer_drops_adversarial_values(self) -> None:
        summary = sanitized_log_summary(
            [
                "failed operator@example.test Authorization Bearer secret-token "
                "Cookie=session=secret-session 198.51.100.42 "
                "https://old-sparky.example/invite/INVITE-CODE?token=secret-token "
                "SELECT email FROM users WHERE password='secret-password' "
                "/home/operator/private-report.json"
            ]
        )
        output = serialized(summary)
        for value in FORBIDDEN_VALUES:
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(set(summary), {
            "schema",
            "status",
            "line_count",
            "retained_line_count",
            "truncated",
            "class_counts",
            "route_class_counts",
            "status_counts",
        })

        seen: list[int] = []

        def endless_lines():
            index = 0
            while True:
                seen.append(index)
                yield b"ordinary output\n"
                index += 1

        bounded = sanitized_log_summary(endless_lines(), max_lines=3)
        self.assertEqual(seen, [0, 1, 2])
        self.assertTrue(bounded["truncated"])
        self.assertEqual(bounded["status"], "truncated")

        giant = sanitized_log_summary(
            BytesIO(
                b"ordinary output "
                + b"x" * 4096
                + b" operator@example.test password=secret-password"
            ),
            max_line_bytes=64,
        )
        self.assertTrue(giant["truncated"])
        self.assertNotIn("operator@example.test", serialized(giant))
        self.assertNotIn("password=secret-password", serialized(giant))

        text_giant = sanitized_log_summary(
            StringIO("ordinary output " + "x" * 4096 + " operator@example.test"),
            max_line_bytes=64,
        )
        self.assertTrue(text_giant["truncated"])
        self.assertNotIn("operator@example.test", serialized(text_giant))

        invalid_utf8 = sanitized_log_summary(BytesIO(b"\xff\xfe\xfa"))
        self.assertFalse(invalid_utf8["truncated"])
        self.assertEqual(invalid_utf8["line_count"], 1)

        budget = sanitized_log_summary(
            BytesIO(b"safe prefix " + b"a" * 128 + b" secret-token"),
            max_total_bytes=32,
            max_line_bytes=64,
        )
        self.assertTrue(budget["truncated"])
        self.assertNotIn("secret-token", serialized(budget))

        def broken_log_stream():
            raise RuntimeError("operator@example.test password=secret-password")
            yield b"unreachable"

        broken = sanitized_log_summary(broken_log_stream())
        self.assertEqual(broken["status"], "error")
        self.assertTrue(broken["truncated"])
        self.assertNotIn("operator@example.test", serialized(broken))

    def test_external_http_summary_keeps_metrics_without_request_values(self) -> None:
        result = RequestResult(
            phase="ready-vote",
            method="POST",
            path=(
                "/tournaments/private-slug/deadlock/ready-check/vote"
                "?invite_code=INVITE-CODE"
            ),
            status=500,
            elapsed_ms=42.5,
            ok=False,
            response_bytes=12,
            time_to_first_byte_ms=10.5,
            cf_ray="unique-ray-id",
            cf_error_type="522",
            cf_error_origin="origin",
            error_kind="ValueError operator@example.test secret-token",
            response_json={"changed": False},
        )
        output = serialized(summarize_results([result]))
        for value in FORBIDDEN_VALUES + ("private-slug", "unique-ray-id"):
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(json.loads(output)["by_route"], {
            "POST ready_vote": {
                "count": 1,
                "avg_ms": 42.5,
                "p50_ms": 42.5,
                "p90_ms": 42.5,
                "p95_ms": 42.5,
                "p99_ms": 42.5,
                "max_ms": 42.5,
            }
        })
        classified = summarize_results(
            [
                RequestResult(
                    phase="state_read",
                    method="GET",
                    path="/tournaments/qa/deadlock/ready-check",
                    status=200,
                    elapsed_ms=1.0,
                    ok=False,
                    response_bytes=0,
                    error_kind="authoritative_state_mismatch",
                ),
                RequestResult(
                    phase="state_read",
                    method="GET",
                    path="/users/me",
                    status=401,
                    elapsed_ms=1.0,
                    ok=False,
                    response_bytes=0,
                    error_kind="unauthorized",
                ),
                RequestResult(
                    phase="state_read",
                    method="GET",
                    path="/users/me",
                    status=403,
                    elapsed_ms=1.0,
                    ok=False,
                    response_bytes=0,
                    error_kind="forbidden",
                ),
            ]
        )
        self.assertEqual(classified["error_kinds"], {"auth": 2, "other": 1})
        self.assertEqual(safe_error_class("authoritative_state_mismatch", status=200), "other")
        self.assertEqual(safe_error_class("authentication_error", status=200), "auth")
        self.assertEqual(safe_error_class("authoritative_state_mismatch", status=401), "auth")
        self.assertEqual(safe_error_class("authoritative_state_mismatch", status=403), "auth")

    def test_response_diagnostics_is_fixed_schema(self) -> None:
        response = httpx.Response(
            500,
            headers={
                "Authorization": "Bearer secret-token",
                "Cookie": "session=secret-session",
                "CF-Error-Type": "522",
                "ETag": "operator@example.test",
            },
            content=(
                b"https://old-sparky.example/invite/INVITE-CODE?token=secret-token "
                b"/home/operator/private-report.json"
            ),
        )
        output = serialized(
            response_diagnostics(
                response,
                method="GET",
                path="/tournaments/private-slug?invite_code=INVITE-CODE",
            )
        )
        for value in FORBIDDEN_VALUES + ("private-slug",):
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(
            json.loads(output)["route_class"],
            "tournament_detail",
        )

    def test_postgres_statement_delta_drops_raw_query_literals(self) -> None:
        raw_query = "SELECT email FROM users WHERE password='secret-password'"
        before = {
            "available": True,
            "rows": [{
                "queryid": "42",
                "query": raw_query,
                "calls": 1,
                "total_exec_ms": 1.0,
                "mean_exec_ms": 1.0,
                "rows": 1,
                "shared_blks_hit": 2,
                "shared_blks_read": 0,
                "temp_blks_written": 0,
            }],
        }
        after = {
            "available": True,
            "rows": [{**before["rows"][0], "calls": 2, "total_exec_ms": 3.5}],
        }
        output = serialized(postgres_statement_delta(before, after))
        self.assertNotIn(raw_query.lower(), output.lower())
        self.assertNotIn("query\":", output.lower())
        self.assertIn("\"queryid\": \"42\"", output)

    def test_observer_source_has_no_raw_postgres_query_projection(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "tools/platform_external_load_observer.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("activity.query", source)
        self.assertNotIn("pg_stat_statements.query", source)
        self.assertNotIn("'active_query_samples'", source)

    def test_observer_timeline_drops_backend_names_and_process_ids(self) -> None:
        waits = _safe_postgres_wait_snapshot(
            {
                "backend_ownership": [
                    {"application_name": "oldsparky-worker", "current": 2},
                    {"application_name": "operator@example.test", "current": 1},
                ],
                "wait_state_counts": {"Lock": 3, "secret-wait": 4},
                "backend_connections": 3,
            }
        )
        lifecycle = _safe_process_lifecycle(
            {
                "deadlock-api": {
                    "new_processes": [1234],
                    "missing_processes": [5678],
                    "new_process_starts": [{"pid": 1234, "started_at": "/home/operator"}],
                }
            }
        )
        output = serialized({"waits": waits, "lifecycle": lifecycle})
        for value in ("operator@example.test", "oldsparky-worker", "1234", "5678", "/home/operator"):
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(waits["backend_ownership"], {"other": 1, "worker": 2})
        self.assertEqual(waits["wait_state_counts"], {"lock": 3, "other": 4})
        self.assertEqual(lifecycle["deadlock-api"], {"new_process_count": 1, "missing_process_count": 1})

        diagnostic_id = "tdiag-123-00003"
        nginx_record = {
            "method": "GET",
            "status": 504,
            "uri": "/tournaments/private-slug?invite=INVITE-CODE",
            "timeout_diagnostic_id": diagnostic_id,
            "request_id": "request-secret",
            "cf_ray": "ray-secret",
            "request_completion": "timeout",
            "upstream_addr": "127.0.0.1:3000",
            "upstream_status": "504",
            "request_time": "30.0",
        }
        private_ssr = summarize_ssr_observability(
            [],
            [nginx_record],
            [],
            timeout_diagnostic_ids={diagnostic_id},
        )
        private_ssr_output = serialized(private_ssr)
        # The exact ID is required only in this private observer intermediate
        # so the timeout joiner can match the client and origin rows.
        self.assertIn(diagnostic_id, private_ssr_output)
        self.assertIn('"route_class": "tournament_page"', private_ssr_output)

        malformed_ssr = summarize_ssr_observability(
            [],
            [{**nginx_record, "timeout_diagnostic_id": "tdiag-secret"}],
            [],
            timeout_diagnostic_ids={"tdiag-secret"},
        )
        self.assertEqual(malformed_ssr["timeout_diagnostics"]["nginx_records"], 0)
        self.assertEqual(malformed_ssr["timeout_diagnostics"]["rows"], [])

        public_observer = project_public_artifact(
            "server_observability",
            {
                "schema": 1,
                "system": {
                    "processes": {
                        "deadlock-api": {
                            "process_count": 1,
                            "rss_bytes": 123456,
                            "pid": 987654,
                            "uid": 876543,
                            "application_name": "oldsparky-worker",
                        }
                    }
                },
                "server_ssr_observability": private_ssr,
            },
        )
        public_ssr_output = serialized(public_observer)
        for value in FORBIDDEN_VALUES + (
            "private-slug",
            "INVITE-CODE",
            diagnostic_id,
            "request-secret",
            "ray-secret",
            "987654",
            "876543",
            "oldsparky-worker",
        ):
            self.assertNotIn(value.lower(), public_ssr_output.lower())
        projected_timeout = public_observer["server_ssr_observability"]["timeout_diagnostics"]
        self.assertEqual(projected_timeout["nginx_records"], 1)
        self.assertEqual(projected_timeout["rows"][0]["route_class"], "tournament_page")

        private_ssr["ssr_workspace_errors"] = {
            "population": "sampled_ssr_requests",
            "observed_records": 4,
            "invalid_records": 1,
            "matched_by_request_id": 2,
            "unmatched_request_ids": 1,
            "ambiguous_request_ids": 0,
            "by_stage": {"tournament_workspace": 2, "private_stage": 8},
            "by_family": {"platform_api_error": 2, "private_family": 8},
            "by_status": {"500": 1, "503": 1, "private-status": 9},
            "by_response_code": {"unavailable": 1, "private-code": 9},
            "matched_nginx_statuses": {"500": 1},
            "matched_upstream_statuses": {"500": 1},
            "request_ids": ["ssr-private-request"],
            "diagnostic_ids": ["tdiag-private"],
            "exception": "operator@example.test /home/operator/private-report.json",
        }
        public_error_observer = project_public_artifact(
            "server_observability",
            {"schema": 1, "server_ssr_observability": private_ssr},
        )
        error_output = serialized(public_error_observer)
        for value in (
            "ssr-private-request",
            "tdiag-private",
            "private_stage",
            "private_family",
            "private-status",
            "private-code",
            "operator@example.test",
            "/home/operator/private-report.json",
        ):
            self.assertNotIn(value.lower(), error_output.lower())
        self.assertEqual(
            public_error_observer["server_ssr_observability"]["ssr_workspace_errors"],
            {
                "population": "sampled_ssr_requests",
                "observed_records": 4,
                "invalid_records": 1,
                "matched_by_request_id": 2,
                "unmatched_request_ids": 1,
                "ambiguous_request_ids": 0,
                "by_stage": {"tournament_workspace": 2},
                "by_family": {"platform_api_error": 2},
                "by_status": {"500": 1, "503": 1},
                "by_response_code": {"unavailable": 1},
                "matched_nginx_statuses": {"500": 1},
                "matched_upstream_statuses": {"500": 1},
            },
        )
        malformed_error_observer = project_public_artifact(
            "server_observability",
            {
                "schema": 1,
                "server_ssr_observability": {
                    "ssr_workspace_errors": {
                        "population": "operator@example.test",
                        "observed_records": True,
                        "by_stage": {"tournament_workspace": True},
                        "by_status": {"500": True},
                        "raw_exception": "secret-token",
                    }
                },
            },
        )
        self.assertEqual(
            malformed_error_observer["server_ssr_observability"]["ssr_workspace_errors"],
            {"population": "unknown"},
        )
        self.assertNotIn("secret-token", serialized(malformed_error_observer))

    def test_cli_summary_exposes_only_control_preservation_boolean(self) -> None:
        report = {
            "marker": "preprod26082900000000ab",
            "mode": "scale",
            "passed": False,
            "control_account_preserved": True,
            "fatal_error": "RuntimeError operator@example.test password=secret-password",
            "report_path": "/home/operator/private-report.json",
            "tournament_slugs": ["private-slug"],
            "scenarios": [{
                "name": "workspace",
                "ok": False,
                "detail": {
                    "path": "https://old-sparky.example/invite/INVITE-CODE?token=secret-token",
                    "route_class": "tournament_workspace",
                    "status": 500,
                    "error_class": "Authorization Bearer secret-token",
                },
            }],
        }
        output = serialized(cli_report_summary(report))
        for value in FORBIDDEN_VALUES + ("private-slug",):
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(json.loads(output)["control_account_preserved"], True)

        external_source = {
            "fixture_marker": "preprod26082900000000ab",
            "source_git_sha": "a" * 40,
            "external_run_id": "123456",
            "origin": "https://old-sparky.example/private?token=secret-token",
            "profile_id": "ready-vote-slo-v2",
            "profile_version": 2,
            "profile_digest": "b" * 64,
            "mode": "ready-vote",
            "users": 2,
            "tournaments": 1,
            "concurrency_stages": [16, 32],
            "dropped_work": 0,
            "client_transport": "ready-vote-http1-reuse-pair",
            "passed": True,
            "status": "complete",
            "raw_http": {
                "requests": 10,
                "errors": 1,
                "latency": {"p95_ms": 42.5},
                "transport": {
                    "names": {"http1-close": 2, "http1-keepalive": 3, "https://private/path": 99},
                    "http_versions": {"1.1": 5, "private": 9},
                    "connection_reused": 3,
                    "connection_new": 2,
                    "phase_timings": {
                        "tcp_connect_ms": {"count": 2, "p95_ms": 5.5},
                        "ttfb_ms": {"count": 5, "p95_ms": 18.0},
                        "private_header": {"count": 1, "p95_ms": 9.0},
                    },
                    "cookie": "session=synthetic-secret",
                    "csrf": "synthetic-csrf",
                    "body": "{\"choice\":\"yes\"}",
                    "path": "/private/tournament/slug",
                },
                "by_route": {
                    "POST ready_vote": {"p95_ms": 42.5},
                    "POST /tournaments/private-slug": {"p95_ms": 999.0},
                },
                "unknown_nested": {
                    "email": "operator@example.test",
                    "values": ["/home/operator/private-report.json"],
                },
                "error_samples": [{
                    "diagnostic_id": "tdiag-123-00001",
                    "path": "https://old-sparky.example/invite/INVITE-CODE?token=secret-token",
                    "message": "password=secret-password",
                    "status": 500,
                }],
                "timeout_diagnostics": [{
                    "diagnostic_id": "tdiag-123-00002",
                    "phase": "authenticated_page_load",
                    "status": 0,
                    "error_class": "timeout",
                }],
            },
            "logical": {
                "actions": 2,
                "final_successes": 2,
                "final_failures": 0,
                "end_to_end_latency": {"p95_ms": 44.5},
            },
            "acceptance": {
                "passed": True,
                "contract_ok": True,
                "decision": "SLO PASS",
                "checks": {"complete": True, "run_identity": True},
            },
            "phases": {
                "write_external_vote_close_a": {
                    "status": "complete",
                    "transport": "http1-close",
                    "raw_http": {
                        "requests": 2,
                        "transport": {
                            "names": {"http1-close": 2},
                            "phase_timings": {
                                "ttfb_ms": {"count": 2, "p95_ms": 17.0},
                            },
                            "cookie": "session=synthetic-secret",
                        },
                    },
                },
                "capacity_ramp": {
                    "concurrency_stages": [16, 32],
                    "stages": {"16": {"raw_http": {"requests": 2}}},
                    "analysis": {
                        "recommended_max_concurrency": 16,
                        "rollout_required": True,
                    },
                },
            },
            "report_binding": {
                "checks": {"run_identity": True},
                "actual": {"source_git_sha": "a" * 40, "external_run_id": "123456"},
            },
            "unknown": {
                "email": "operator@example.test",
                "auth": "Authorization Bearer secret-token",
                "query": "SELECT email FROM users WHERE password='secret-password'",
                "list": ["invite=INVITE-CODE", "/home/operator/private-report.json"],
            },
        }
        public_report = project_public_artifact("external_load", external_source)
        public_output = serialized(public_report)
        for value in FORBIDDEN_VALUES + (
            "preprod26082900000000ab",
            "a" * 40,
            "123456",
            "tdiag-123-00001",
            "tdiag-123-00002",
        ):
            self.assertNotIn(value.lower(), public_output.lower())
        self.assertEqual(public_report["raw_http"]["requests"], 10)
        self.assertEqual(public_report["raw_http"]["latency"]["p95_ms"], 42.5)
        self.assertEqual(
            public_report["client_transport"], "ready-vote-http1-reuse-pair"
        )
        self.assertEqual(
            public_report["raw_http"]["transport"]["names"],
            {"http1-close": 2, "http1-keepalive": 3},
        )
        self.assertEqual(
            public_report["raw_http"]["transport"]["phase_timings"]["ttfb_ms"]["p95_ms"],
            18.0,
        )
        self.assertEqual(
            public_report["phases"]["write_external_vote_close_a"]["transport"],
            "http1-close",
        )
        self.assertEqual(
            public_report["phases"]["write_external_vote_close_a"]["raw_http"]["transport"]["names"],
            {"http1-close": 2},
        )
        for forbidden in (
            "synthetic-secret",
            "synthetic-csrf",
            "private_header",
            "private/tournament/slug",
            "https://private/path",
        ):
            self.assertNotIn(forbidden.lower(), public_output.lower())
        self.assertTrue(public_report["acceptance"]["checks"]["complete"])
        self.assertEqual(public_report["concurrency_stages"], [16, 32])
        self.assertEqual(public_report["phases"]["capacity_ramp"]["concurrency_stages"], [16, 32])
        self.assertEqual(
            public_report["phases"]["capacity_ramp"]["analysis"]["recommended_max_concurrency"],
            16,
        )
        self.assertTrue(public_report["report_binding"]["checks"]["run_identity"])
        self.assertNotIn("actual", public_report["report_binding"])
        self.assertNotIn("unknown", public_report)

        for completed_decision in (
            "STRESS BEHAVIOR FAIL",
            "SPIKE BEHAVIOR FAIL",
            "CAPACITY EXPERIMENT COMPLETE TARGET FAIL",
        ):
            projected = project_public_artifact(
                "external_load",
                {
                    "acceptance": {
                        "passed": False,
                        "decision": completed_decision,
                        "pending_origin_evidence": False,
                        "checks": {"contract": True},
                    }
                },
            )
            self.assertEqual(projected["acceptance"]["decision"], completed_decision)
        unknown_decision = project_public_artifact(
            "external_load",
            {"acceptance": {"passed": False, "decision": "operator text secret"}},
        )
        self.assertEqual(unknown_decision["acceptance"]["decision"], "other")

        invalid_types = project_public_artifact(
            "external_load",
            {
                "users": "2",
                "raw_http": {"requests": "10", "latency": ["42.5"]},
                "acceptance": {"passed": "true", "checks": ["complete"]},
                "unknown_string": "operator@example.test",
            },
        )
        self.assertNotIn("users", invalid_types)
        self.assertNotIn("raw_http", invalid_types)
        self.assertNotIn("passed", invalid_types["acceptance"])

        worker_failure = {
            "schema": 1,
            "stage": "worker_callback",
            "exception_class": "value_error",
            "module": "load",
            "line": 123,
            "traceback_truncated": False,
        }
        projected_failure = project_public_artifact(
            "external_load", {"worker_failure": worker_failure}
        )
        self.assertEqual(
            projected_failure["worker_failure"],
            {
                "schema": 1,
                "stage": "worker_callback",
                "exception_class": "value_error",
                "module": "load",
                "line": 123,
                "traceback_truncated": False,
            },
        )
        self.assertNotIn("private-report", serialized(projected_failure))
        for invalid_worker_failure in (
            {**worker_failure, "schema": True},
            {**worker_failure, "line": 123.0},
            {**worker_failure, "line": 0},
            {**worker_failure, "line": 1_000_001},
            {**worker_failure, "module": "other", "line": 123},
            {**worker_failure, "raw_path": "/home/operator/private-report.json"},
        ):
            invalid_failure_projection = project_public_artifact(
                "external_load", {"worker_failure": invalid_worker_failure}
            )
            self.assertNotIn("worker_failure", invalid_failure_projection)

        observer_source = {
            "schema": 1,
            "binding": {
                "fixture_marker": "preprod26082900000000ab",
                "external_run_id": "123456",
                "complete": True,
            },
            "started_at": "2026-09-12T00:00:00+00:00",
            "system": {
                "samples": 2,
                "memory": {"p95": 123.0},
                "processes": {
                    "deadlock-api": {
                        "process_count": 1,
                        "rss_bytes": 123456,
                        "pid": 987654,
                        "uid": 876543,
                        "home_path": "/home/operator/private-report.json",
                    },
                },
                "timeline": [{"pid": 987654, "ppid": 123, "start_time_ticks": 44}],
                "cpu_per_core": {"cpu0": {"p95": 20.0}},
            },
            "server_ssr_observability": {
                "event_loop": {
                    "samples": 2,
                    "samples_detail": [{"pid": 987654, "p95_ms": 8.0}],
                    "by_pid": {"987654": {"memory": {"rss_bytes": 123456}}},
                },
                "nginx_html": {
                    "requests": 3,
                    "statuses": {
                        "200": 1,
                        "500": 1,
                        "503": 1,
                        "operator@example.test": 99,
                    },
                    "upstream_statuses": {
                        "502": 1,
                        "503": 1,
                        "secret-token": 7,
                    },
                    "upstream_status_unknown_attempts": 1,
                    "request_time_ms": {"p50_ms": 500.0},
                    "by_status": {
                        "503": {
                            "requests": 1,
                            "upstream_statuses": {"502": 1, "secret-token": 4},
                            "upstream_status_unknown_attempts": 1,
                            "request_time_ms": {"p50_ms": 1500.0},
                            "raw_uri": "/tournaments/private-slug?token=secret-token",
                        },
                        "900": {"requests": 99, "request_time_ms": {"p50_ms": 999.0}},
                        "/private": {"requests": 99},
                    },
                },
                "nginx_api": {
                    "requests": 1,
                    "by_method_route": {
                        "GET tournament_detail": {"p95_ms": 8.0},
                        "GET /tournaments/private-slug": {"p95_ms": 99.0},
                    },
                },
            },
            "cpu_profile": {
                "enabled": True,
                "armed_worker_identities": [{"pid": 987654, "uid": 876543}],
                "profiles": [
                    {
                        "profile_available": True,
                        "filename": "/home/operator/private-profile.pstats",
                        "pid": 987654,
                        "functions": [
                            {
                                "function": "ReadyVoteService.cast_vote",
                                "line": 42,
                                "primitive_calls": 8,
                                "calls": 12,
                                "self_seconds": 0.125,
                                "cumulative_seconds": 0.75,
                                "filename": "/home/operator/private-source.py",
                                "pid": 987654,
                            },
                            {
                                "function": "<dynamic function at 0x1234>",
                                "line": 3.0,
                                "primitive_calls": "1",
                                "calls": 1_000_000_000_001,
                                "self_seconds": float("nan"),
                                "cumulative_seconds": -1.0,
                                "filename": "/home/operator/private-source.py",
                            },
                            {"function": "192.168.1.1"},
                            {"function": "operator@example.test"},
                            {"function": "/tmp/private.py"},
                            {"function": "987654"},
                            {
                                "function": "<lambda>",
                                "self_seconds": float("inf"),
                                "cumulative_seconds": 0.0,
                            },
                        ],
                    }
                ],
                "retention": {
                    "available_profiles": 1,
                    "ignored_stale_profiles": 2,
                    "invalid_profiles": 1,
                },
                "signal_delivery": {
                    "arm": {
                        "requested_count": 4,
                        "delivered_count": 1,
                        "rejected_count": 3,
                        "pidfd_api_available": False,
                        "rejection_reasons": {
                            "pidfd_unavailable": 1,
                            "uid_unavailable": 1,
                            "worker_missing": 1,
                            "operator@example.test": 99,
                        },
                    },
                    "flush": {
                        "requested_count": 0,
                        "delivered_count": 0,
                        "rejected_count": 0,
                        "pidfd_api_available": False,
                        "availability_reason": "uid_unavailable",
                    },
                },
            },
            "postgres_stat_statements": {
                "before": {
                    "available": True,
                    "rows": [{
                        "queryid": "42",
                        "query": "SELECT email FROM users WHERE password='secret-password'",
                        "calls": 1,
                    }],
                },
            },
            "unknown": {
                "email": "operator@example.test",
                "authorization": "Bearer secret-token",
                "url": "https://old-sparky.example/invite/INVITE-CODE?token=secret-token",
                "values": ["/home/operator/private-report.json"],
            },
        }
        public_observer = project_public_artifact("server_observability", observer_source)
        observer_output = serialized(public_observer)
        for value in FORBIDDEN_VALUES + (
            "987654",
            "876543",
        ):
            self.assertNotIn(value.lower(), observer_output.lower())
        self.assertEqual(public_observer["system"]["processes"]["deadlock-api"]["rss_bytes"], 123456)
        self.assertNotIn("binding", public_observer)
        self.assertNotIn("by_pid", public_observer["server_ssr_observability"]["event_loop"])
        public_html = public_observer["server_ssr_observability"]["nginx_html"]
        self.assertEqual(
            public_html["statuses"],
            {"200": 1, "500": 1, "503": 1},
        )
        self.assertEqual(public_html["upstream_statuses"], {"502": 1, "503": 1})
        self.assertEqual(public_html["upstream_status_unknown_attempts"], 1)
        self.assertEqual(public_html["request_time_ms"], {"p50_ms": 500.0})
        self.assertEqual(
            public_html["by_status"],
            {
                    "503": {
                        "requests": 1,
                        "upstream_statuses": {"502": 1},
                        "upstream_status_unknown_attempts": 1,
                    "request_time_ms": {"p50_ms": 1500.0},
                }
            },
        )
        self.assertNotIn("armed_worker_identities", public_observer["cpu_profile"])
        self.assertEqual(public_observer["cpu_profile"]["ignored_stale_profiles"], 2)
        profile = public_observer["cpu_profile"]["profiles"][0]
        self.assertEqual(profile["profile_available"], True)
        self.assertEqual(
            profile["functions"][0],
            {
                "function": "ReadyVoteService.cast_vote",
                "line": 42,
                "primitive_calls": 8,
                "calls": 12,
                "self_seconds": 0.125,
                "cumulative_seconds": 0.75,
            },
        )
        self.assertEqual(profile["functions"][1]["function"], "other")
        self.assertNotIn("line", profile["functions"][1])
        self.assertNotIn("primitive_calls", profile["functions"][1])
        self.assertNotIn("calls", profile["functions"][1])
        self.assertNotIn("self_seconds", profile["functions"][1])
        self.assertNotIn("cumulative_seconds", profile["functions"][1])
        self.assertEqual(
            [row["function"] for row in profile["functions"][1:]],
            ["other", "other", "other", "other", "other", "<lambda>"],
        )
        self.assertNotIn("self_seconds", profile["functions"][5])
        self.assertNotIn("cumulative_seconds", profile["functions"][5])
        self.assertNotIn("self_seconds", profile["functions"][6])
        self.assertEqual(profile["functions"][6]["cumulative_seconds"], 0.0)
        self.assertNotIn("filename", profile)
        self.assertNotIn("pid", profile)
        self.assertNotIn("private-profile", observer_output)
        self.assertNotIn("private-source", observer_output)
        self.assertNotIn("dynamic function", observer_output)
        self.assertNotIn("192.168.1.1", observer_output)
        self.assertNotIn("operator@example.test", observer_output)
        self.assertNotIn("/tmp/private.py", observer_output)
        self.assertNotIn("987654", observer_output)
        self.assertEqual(
            public_observer["cpu_profile"]["signal_delivery"]["arm"]["rejection_reasons"],
            {
                "pidfd_unavailable": 1,
                "uid_unavailable": 1,
                "worker_missing": 1,
            },
        )
        self.assertEqual(
            public_observer["cpu_profile"]["signal_delivery"]["flush"]["availability_reason"],
            "uid_unavailable",
        )
        self.assertEqual(public_observer["postgres_stat_statements"]["before"]["rows"][0]["queryid"], "42")

        bounded_profiles = project_public_artifact(
            "server_observability",
            {
                "cpu_profile": {
                    "profiles": [
                        {
                            "profile_available": True,
                            "functions": [
                                {
                                    "function": "f",
                                    "line": 1,
                                    "primitive_calls": 1,
                                    "calls": 1,
                                    "self_seconds": 0.1,
                                    "cumulative_seconds": 0.2,
                                }
                            ]
                            * 101,
                        }
                    ]
                    * 33
                }
            },
        )["cpu_profile"]["profiles"]
        self.assertEqual(len(bounded_profiles), 32)
        self.assertEqual(len(bounded_profiles[0]["functions"]), 100)

        timeout_source = {
            "schema": 1,
            "profile_id": "authenticated-page-load-v1",
            "summary": {
                "client_timeout_errors": 1,
                "classifications": {"before_origin_observed": 1, "dynamic-route": 9},
                "error": "Authorization Bearer secret-token",
            },
            "rows": [{
                "observation_index": 0,
                "classification": "before_origin_observed",
                "classification_reason_class": "no_origin_observation",
                "origin": {
                    "route_class": "tournament_detail",
                    "method": "GET",
                    "status": 504,
                    "path": "/tournaments/private-slug?invite=INVITE-CODE",
                    "request_id": "request-secret",
                    "diagnostic_id": "tdiag-private",
                },
                "error_path": "/home/operator/private-report.json",
                "unknown": ["SELECT email FROM users WHERE password='secret-password'"],
            }],
            "dynamic_route_map": {"GET /tournaments/private-slug": 1},
        }
        public_timeout = project_public_artifact("timeout_diagnostics", timeout_source)
        timeout_output = serialized(public_timeout)
        for value in FORBIDDEN_VALUES + (
            "private-slug",
            "request-secret",
            "tdiag-private",
        ):
            self.assertNotIn(value.lower(), timeout_output.lower())
        self.assertEqual(public_timeout["summary"]["client_timeout_errors"], 1)
        self.assertEqual(public_timeout["rows"][0]["origin"]["route_class"], "tournament_detail")
        self.assertNotIn("dynamic_route_map", public_timeout)

        # The step summary consumes only this public projection. Keep its
        # source contract free of private binding/runner values as well.
        workflow = (
            Path(__file__).resolve().parents[2]
            / ".github/workflows/platform-production-external-load.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("payload.get('source_git_sha')", workflow)
        self.assertNotIn("payload.get('load_contract')", workflow)
        for artifact_type in (
            "external_load",
            "server_observability",
            "timeout_diagnostics",
        ):
            self.assertIn(f"--artifact-type {artifact_type}", workflow)

    def test_durable_scenario_detail_drops_adversarial_values(self) -> None:
        detail = _safe_qa_scenario_detail(
            {
                "email": "operator@example.test",
                "authorization": "Bearer secret-token",
                "cookie": "session=secret-session; csrf=secret-csrf",
                "password": "secret-password",
                "path": "https://old-sparky.example/api/v1/tournaments/private-slug/invite?token=secret-token",
                "request_id": "req-secret",
                "sql": "SELECT email FROM users WHERE password='secret-password'",
                "remote_ip": "198.51.100.42",
                "safe_status": 500,
                "nested": ["/home/operator/private-report.json", "INVITE-CODE"],
            }
        )
        output = serialized(detail)
        for value in FORBIDDEN_VALUES + ("req-secret",):
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(detail["email"], True)
        self.assertEqual(detail["route_class"], "tournament_participants")
        self.assertEqual(detail["request_id_present"], True)
        self.assertEqual(detail["nested"], {"count": 2})

    def test_retained_abort_exports_projected_observer_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "runs" / "gha-123"
            export_root = root / "exports"
            run_root.mkdir(parents=True)
            export_root.mkdir()
            (run_root / "server-observability.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "binding": {
                            "fixture_marker": "preprod26082900000000ab",
                            "external_run_id": "123",
                            "complete": True,
                        },
                        "system": {
                            "processes": {
                                "deadlock-api": {
                                    "process_count": 1,
                                    "pid": 321,
                                    "uid": 654,
                                    "ppid": 987,
                                    "start_time_ticks": 111,
                                    "rss_bytes": 123456,
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch("tools.platform_abort_retained_load.RUN_ROOT_BASE", root / "runs"), \
                mock.patch("tools.platform_abort_retained_load.ABORT_EXPORT_BASE", export_root), \
                mock.patch.dict(
                    os.environ,
                    {
                        "SUDO_UID": str(os.geteuid()),
                        "SUDO_GID": str(os.getegid()),
                    },
                ):
                exported = export_abort_evidence("123", '{"schema":1,"process_count":1}\n')
            payload = json.loads((exported / "server-observability.json").read_text(encoding="utf-8"))
            output = serialized(payload)
            self.assertNotIn("preprod26082900000000ab", output)
            self.assertNotIn("321", output)
            self.assertNotIn("654", output)
            self.assertNotIn("987", output)
            self.assertNotIn("111", output)
            self.assertEqual(payload["public_projection"], True)
            self.assertEqual(payload["authoritative"], False)
            self.assertEqual(
                payload["system"]["processes"]["deadlock-api"]["rss_bytes"],
                123456.0,
            )


if __name__ == "__main__":
    unittest.main()
