"""Closed schema and fixed dispatcher handoff tests for dual-source QA."""

from __future__ import annotations

import base64
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from tools import platform_workflow_remote_dispatch as dispatcher  # noqa: E402
from tools.platform_workflow_input_guard import (  # noqa: E402
    EXTERNAL_CONFIRMATION,
    WorkflowInputError,
    validate_cleanup_payload,
    validate_cpu_diagnostic_payload,
    validate_external_payload,
    validate_live_payload,
    validate_ssr_capacity_memory_watch_payload,
)
from tools.platform_autodeploy_freshness_guard import (  # noqa: E402
    main as freshness_guard_main,
    validate_dev_response,
)


RUNNER_SHA = "a" * 40
APP_SHA = "b" * 40


def _baseline() -> dict[str, object]:
    return {
        "schema": 1,
        "source_sha": APP_SHA,
        "release_slug": "gha-123456-1-bbbbbbbbbbbb",
        "release_json_sha256": "c" * 64,
        "current_link_dev": 100,
        "current_link_ino": 101,
        "release_dev": 100,
        "release_ino": 102,
        "pending_operation": False,
    }


def _binding() -> dict[str, object]:
    deploy_id = "123456"
    deploy_attempt = "2"
    archive_sha = "d" * 64
    return {
        "schema": 1,
        "binding_mode": "verified-noop",
        "runner_sha": RUNNER_SHA,
        "app_target_sha": APP_SHA,
        "baseline_identity": _baseline(),
        "receipt_document_sha256": "e" * 64,
        "receipt_artifact_id": "456789",
        "receipt_artifact_name": (
            f"platform-production-noop-source-receipt-{deploy_id}-{deploy_attempt}"
        ),
        "receipt_artifact_digest": f"sha256:{archive_sha}",
        "receipt_archive_sha256": archive_sha,
        "cumulative_manifest_sha256": "f" * 64,
        "source_security_run_id": "234567",
        "source_security_run_attempt": "1",
        "autodeploy_run_id": "345678",
        "autodeploy_run_attempt": "3",
        "production_deploy_run_id": deploy_id,
        "production_deploy_run_attempt": deploy_attempt,
    }


def _never_started_cleanup_proof(load_run_id: str = "567890") -> dict[str, object]:
    return {
        "schema": 1,
        "load_run_id": load_run_id,
        "load_run_attempt": "2",
        "repository": "StrayForest/old_sparky",
        "workflow_path": ".github/workflows/platform-production-external-load.yml",
        "head_branch": "dev",
        "head_sha": "d" * 40,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "failure",
        "jobs": {
            "validate-external-inputs": {"status": "completed", "conclusion": "failure"},
            "fixture-setup": {"status": "completed", "conclusion": "skipped"},
            "load-client": {"status": "completed", "conclusion": "skipped"},
            "cpu-diagnostic-pair": {"status": "completed", "conclusion": "skipped"},
        },
        "finalizer_steps": {
            "Configure finalizer SSH": "skipped",
            "Signal fixture completion and collect origin evidence": "skipped",
            "Exact cleanup of external fixture": "skipped",
        },
    }


def _external_payload(schema: int = 2) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": schema,
        "confirmation": EXTERNAL_CONFIRMATION,
        "target_sha": RUNNER_SHA,
        "control_email": "qa.control@example.test",
        "setup_concurrency": "2",
        "run_id": "567890",
        "profile": "external-vote",
        "tournament_count": "1",
        "users_per_tournament": "14",
        "timeout_diagnostics": "false",
    }
    if schema == 2:
        payload["source_binding"] = _binding()
    return payload


class SourceBindingDispatchTests(unittest.TestCase):
    def test_html_capacity_workload_crosses_both_cpu_plan_dispatch_guards(self) -> None:
        html_plan = {
            "schema": 1,
            "operation": "prepare",
            "run_id": "a" * 32,
            "source_sha": APP_SHA,
            "workload": "authenticated_html_ssr_capacity_v1",
            "off_start_ms": 1_000_000,
            "off_end_ms": 1_020_000,
            "on_start_ms": 1_025_000,
            "on_end_ms": 1_045_000,
        }
        self.assertEqual(
            validate_cpu_diagnostic_payload(html_plan)["workload"],
            "authenticated_html_ssr_capacity_v1",
        )
        html_watch = {
            "schema": 1,
            "operation": "memory-watch",
            "run_id": html_plan["run_id"],
            "source_sha": APP_SHA,
            "workload": html_plan["workload"],
        }
        self.assertEqual(
            validate_ssr_capacity_memory_watch_payload(html_watch)["workload"],
            "authenticated_html_ssr_capacity_v1",
        )
        for validator, payload in (
            (validate_cpu_diagnostic_payload, {**html_plan, "workload": []}),
            (validate_ssr_capacity_memory_watch_payload, {**html_watch, "workload": []}),
        ):
            with self.subTest(validator=validator.__name__):
                with self.assertRaises(WorkflowInputError):
                    validator(payload)

    def test_ssr_memory_watch_accepts_only_the_two_typed_capacity_workloads(self) -> None:
        payload = {
            "schema": 1,
            "operation": "memory-watch",
            "run_id": "a" * 32,
            "source_sha": APP_SHA,
            "workload": "authenticated_workspace_read_capacity_v1",
        }
        self.assertEqual(
            validate_ssr_capacity_memory_watch_payload(payload)["workload"],
            "authenticated_workspace_read_capacity_v1",
        )
        html = {
            **payload,
            "workload": "authenticated_html_ssr_capacity_v1",
        }
        self.assertEqual(
            validate_ssr_capacity_memory_watch_payload(html)["workload"],
            "authenticated_html_ssr_capacity_v1",
        )
        for mutation in (
            {**payload, "workload": "authenticated_workspace_read_pair_v1"},
            {**payload, "workload": []},
            {**html, "unexpected": True},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(WorkflowInputError):
                    validate_ssr_capacity_memory_watch_payload(mutation)

    def test_autodeploy_freshness_guard_binds_exact_live_dev_sha(self) -> None:
        tested_sha = "a" * 40
        exact_response = json.dumps(
            {"name": "dev", "commit": {"sha": tested_sha}},
            separators=(",", ":"),
        ).encode("ascii")
        moved_response = json.dumps(
            {"name": "dev", "commit": {"sha": "b" * 40}},
            separators=(",", ":"),
        ).encode("ascii")

        self.assertTrue(validate_dev_response(exact_response, tested_sha))
        self.assertFalse(validate_dev_response(moved_response, tested_sha))
        self.assertFalse(validate_dev_response(b'{"name":"dev","commit":{}}', tested_sha))
        self.assertFalse(validate_dev_response(exact_response, ""))
        with tempfile.TemporaryDirectory() as temp_dir:
            response_path = Path(temp_dir) / "branch-response.json"
            response_path.write_bytes(exact_response)
            self.assertEqual(
                freshness_guard_main([
                    "--response", str(response_path), "--expected-sha", tested_sha,
                ]),
                0,
            )
            response_path.write_bytes(moved_response)
            with patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(
                    freshness_guard_main([
                        "--response", str(response_path), "--expected-sha", tested_sha,
                    ]),
                    1,
                )
            response_path.write_bytes(b'{"name":"dev","commit":{}}')
            with patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(
                    freshness_guard_main([
                        "--response", str(response_path), "--expected-sha", tested_sha,
                    ]),
                    1,
                )

    def test_schema_two_is_closed_across_load_cleanup_and_live_inputs(self) -> None:
        external = validate_external_payload(_external_payload())
        self.assertEqual(external["schema"], "2")
        self.assertEqual(external["target_sha"], RUNNER_SHA)
        self.assertEqual(external["source_binding"]["app_target_sha"], APP_SHA)

        cleanup = validate_cleanup_payload(
            {
                "schema": 2,
                "target_sha": RUNNER_SHA,
                "control_email": "qa.control@example.test",
                "load_run_id": "567890",
                "cleanup_run_id": "678901",
                "source_binding": _binding(),
            }
        )
        self.assertEqual(cleanup["target_sha"], RUNNER_SHA)
        self.assertEqual(cleanup["source_binding"]["app_target_sha"], APP_SHA)

        live = validate_live_payload(
            {
                "schema": 2,
                "base_url": "https://old-sparky.com",
                "provision": "false",
                "marker": "",
                "target_sha": RUNNER_SHA,
                "source_binding": _binding(),
            }
        )
        self.assertEqual(live["target_sha"], RUNNER_SHA)
        self.assertEqual(live["source_binding"]["app_target_sha"], APP_SHA)

        malformed = _external_payload()
        malformed["source_binding"] = {**_binding(), "unexpected": "value"}
        with self.assertRaises(WorkflowInputError):
            validate_external_payload(malformed)
        oversized = _external_payload()
        oversized["source_binding"] = {
            **_binding(),
            "receipt_artifact_name": "x" * 65_536,
        }
        with self.assertRaises(WorkflowInputError):
            validate_external_payload(oversized)

    def test_never_started_cleanup_proof_is_exact_and_keeps_old_load_sha_separate(self) -> None:
        proof = _never_started_cleanup_proof()
        payload = validate_cleanup_payload(
            {
                "schema": 2,
                "target_sha": RUNNER_SHA,
                "control_email": "qa.control@example.test",
                "load_run_id": "567890",
                "cleanup_run_id": "678901",
                "source_binding": _binding(),
                "never_started_proof": proof,
            }
        )
        self.assertEqual(payload["never_started_proof"], proof)
        self.assertNotEqual(proof["head_sha"], payload["target_sha"])
        self.assertNotEqual(proof["head_sha"], payload["source_binding"]["app_target_sha"])

        invalid_proofs = []
        invalid_proofs.append({**proof, "load_run_id": "123456"})
        invalid_proofs.append({**proof, "workflow_path": ".github/workflows/other.yml"})
        invalid_proofs.append({**proof, "head_sha": "not-a-sha"})
        invalid_proofs.append({**proof, "conclusion": "success"})
        invalid_proofs.append(
            {**proof, "jobs": {**proof["jobs"], "fixture-setup": {"status": "completed", "conclusion": "failure"}}}
        )
        invalid_proofs.append(
            {
                **proof,
                "finalizer_steps": {
                    **proof["finalizer_steps"],
                    "Configure finalizer SSH": "success",
                },
            }
        )
        invalid_proofs.append({**proof, "unexpected": True})
        for invalid in invalid_proofs:
            with self.subTest(proof=invalid):
                with self.assertRaises(WorkflowInputError):
                    validate_cleanup_payload(
                        {
                            "schema": 2,
                            "target_sha": RUNNER_SHA,
                            "control_email": "qa.control@example.test",
                            "load_run_id": "567890",
                            "cleanup_run_id": "678901",
                            "source_binding": _binding(),
                            "never_started_proof": invalid,
                        }
                    )

    def test_cpu_only_fixture_is_explicit_and_fixed_to_qa_minimum(self) -> None:
        payload = _external_payload()
        payload.update(
            confirmation="RUN-PRODUCTION-CPU-DIAGNOSTIC",
            fixture_kind="cpu_diagnostic_only_v1",
            tournament_count="1",
            users_per_tournament="14",
        )
        validated = validate_external_payload(payload)
        self.assertEqual(validated["fixture_kind"], "cpu_diagnostic_only_v1")
        self.assertNotIn("ssr_capacity_concurrency", validated)
        for mutation in (
            {**payload, "tournament_count": "2"},
            {**payload, "users_per_tournament": "500"},
            {**payload, "timeout_diagnostics": "true"},
            {**payload, "fixture_kind": "external_load"},
            {**payload, "fixture_kind": "cpu_diagnostic_only_v1", "confirmation": EXTERNAL_CONFIRMATION},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(WorkflowInputError):
                    validate_external_payload(mutation)

        cpu_payload = _external_payload()
        cpu_payload.update(
            confirmation="RUN-PRODUCTION-CPU-DIAGNOSTIC",
            fixture_kind="cpu_diagnostic_only_v1",
            tournament_count="1", users_per_tournament="14",
        )
        self.assertEqual(
            validate_external_payload(cpu_payload)["fixture_kind"],
            "cpu_diagnostic_only_v1",
        )
        ordinary = _external_payload()
        with self.assertRaises(WorkflowInputError):
            validate_external_payload({**ordinary, "ssr_capacity_concurrency": "16"})

    def test_ssr_capacity_fixture_is_a_separate_fixed_zero_credit_route(self) -> None:
        payload = _external_payload()
        payload.update(
            confirmation="RUN-PRODUCTION-SSR-CAPACITY-DIAGNOSTIC",
            fixture_kind="ssr_capacity_diagnostic_v1",
            ssr_capacity_concurrency="8",
            tournament_count="1",
            users_per_tournament="14",
        )
        validated = validate_external_payload(payload)
        self.assertEqual(validated["fixture_kind"], "ssr_capacity_diagnostic_v1")
        self.assertEqual(validated["ssr_capacity_concurrency"], "8")
        for concurrency in ("8", "16"):
            self.assertEqual(
                validate_external_payload({**payload, "ssr_capacity_concurrency": concurrency})[
                    "ssr_capacity_concurrency"
                ],
                concurrency,
            )
        for mutation in (
            {**payload, "confirmation": EXTERNAL_CONFIRMATION},
            {**payload, "tournament_count": "2"},
            {**payload, "users_per_tournament": "500"},
            {**payload, "timeout_diagnostics": "true"},
            {**payload, "fixture_kind": "cpu_diagnostic_only_v1"},
            {**payload, "fixture_kind": "external_load"},
            {**payload, "ssr_capacity_concurrency": "4"},
            {**payload, "fixture_kind": "cpu_diagnostic_only_v1", "ssr_capacity_concurrency": "16"},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(WorkflowInputError):
                    validate_external_payload(mutation)

    def test_html_ssr_capacity_is_separately_typed_and_reuses_only_14_fixture_actors(self) -> None:
        payload = _external_payload()
        payload.update(
            confirmation="RUN-PRODUCTION-SSR-CAPACITY-DIAGNOSTIC",
            fixture_kind="ssr_html_capacity_diagnostic_v1",
            ssr_capacity_workload="authenticated_html_ssr_capacity_v1",
            ssr_capacity_concurrency="8",
            tournament_count="1",
            users_per_tournament="14",
        )
        validated = validate_external_payload(payload)
        self.assertEqual(validated["ssr_capacity_workload"], "authenticated_html_ssr_capacity_v1")
        self.assertEqual(validated["ssr_capacity_concurrency"], "8")
        for concurrency in ("8", "16", "32", "64"):
            self.assertEqual(
                validate_external_payload({**payload, "ssr_capacity_concurrency": concurrency})[
                    "ssr_capacity_concurrency"
                ],
                concurrency,
            )
        for mutation in (
            {**payload, "ssr_capacity_workload": "authenticated_workspace_read_capacity_v1"},
            {**payload, "ssr_capacity_concurrency": "128"},
            {**payload, "tournament_count": "2"},
            {**payload, "users_per_tournament": "15"},
            {**payload, "timeout_diagnostics": "true"},
            {**payload, "fixture_kind": "ssr_capacity_diagnostic_v1"},
            {**payload, "fixture_kind": "external_load"},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(WorkflowInputError):
                    validate_external_payload(mutation)

    def test_legacy_schema_one_preserves_same_source_shape(self) -> None:
        payload = _external_payload(schema=1)
        payload["target_sha"] = APP_SHA
        result = validate_external_payload(payload)
        self.assertEqual(result["schema"], "1")
        self.assertNotIn("source_binding", result)

    def test_fixed_binding_argument_keeps_runner_and_app_identities_separate(self) -> None:
        payload = validate_external_payload(_external_payload())
        app_sha, baseline, arguments = dispatcher._source_binding_context(payload)
        self.assertEqual(app_sha, APP_SHA)
        self.assertEqual(baseline, _baseline())
        self.assertEqual(arguments[0], "--source-binding-base64")
        encoded = arguments[1]
        decoded = json.loads(base64.b64decode(encoded, validate=True))
        self.assertEqual(decoded, _binding())
        self.assertEqual(payload["target_sha"], RUNNER_SHA)

        without_binding = _external_payload(schema=1)
        app_sha, baseline, arguments = dispatcher._source_binding_context(without_binding)
        self.assertEqual((app_sha, baseline, arguments), (RUNNER_SHA, None, []))

    def test_fixture_uses_fixed_argv_and_appends_only_the_bounded_binding(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.stdin = io.BytesIO()

        payload = validate_external_payload(_external_payload())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "platform_production_external_fixture_qa.sh"
            helper.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            helper.chmod(0o700)
            process = FakeProcess()
            with (
                patch.object(dispatcher, "ACTIVE_TOOLS_DIR", root),
                patch.object(dispatcher, "EXTERNAL_HELPER", helper),
                patch.object(dispatcher, "SUDO", "/usr/bin/sudo"),
                patch.object(dispatcher.subprocess, "Popen", return_value=process) as popen,
            ):
                self.assertEqual(
                    dispatcher._external_fixture(
                        payload, run_id="567890", run_attempt="3"
                    ), 0
                )
            for fixture_kind in (
                "cpu_diagnostic_only_v1",
                "ssr_capacity_diagnostic_v1",
                "ssr_html_capacity_diagnostic_v1",
            ):
                typed_payload = {**payload, "fixture_kind": fixture_kind}
                typed_child = FakeProcess()
                with (
                    patch.object(dispatcher, "ACTIVE_TOOLS_DIR", root),
                    patch.object(dispatcher, "EXTERNAL_HELPER", helper),
                    patch.object(dispatcher, "SUDO", "/usr/bin/sudo"),
                    patch.object(
                        dispatcher.subprocess, "Popen", return_value=typed_child,
                    ) as typed_popen,
                ):
                    self.assertEqual(
                        dispatcher._external_fixture(
                            typed_payload, run_id="567890", run_attempt="3"
                        ),
                        0,
                    )
                self.assertEqual(
                    typed_popen.call_args.args[0][-2:],
                    ["--fixture-kind", fixture_kind],
                )
        command = popen.call_args.args[0]
        self.assertEqual(command[:4], ["/usr/bin/sudo", "-n", "--", str(helper)])
        self.assertEqual(
            command[4:12],
            [
                EXTERNAL_CONFIRMATION,
                RUNNER_SHA,
                "2",
                "567890",
                "external-vote",
                "1",
                "14",
                "false",
            ],
        )
        self.assertEqual(command[12], "--source-binding-base64")
        decoded = json.loads(base64.b64decode(command[13], validate=True))
        self.assertEqual(decoded, _binding())
        self.assertEqual(command[14:16], ["--run-attempt", "3"])
        self.assertEqual(command[16], "--input-handoff-sha256")
        self.assertRegex(command[17], r"^[0-9a-f]{64}$")
        self.assertEqual(command[18:], [])
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_cleanup_dispatch_keeps_runner_sha_and_forwards_closed_binding(self) -> None:
        payload = validate_cleanup_payload(
            {
                "schema": 2,
                "target_sha": RUNNER_SHA,
                "control_email": "qa.control@example.test",
                "load_run_id": "567890",
                "cleanup_run_id": "567890",
                "source_binding": _binding(),
            }
        )
        with (
            patch.object(dispatcher, "load_stdin_payload", return_value=payload),
            patch.object(dispatcher, "_run_retained_cleanup_sudo", return_value=0) as cleanup,
        ):
            self.assertEqual(
                dispatcher.main(
                    ["external-cleanup", "--run-attempt", "1", "--profile-id", "ready-vote-slo-v2"]
                ),
                0,
            )
        arguments = cleanup.call_args.args[1]
        self.assertEqual(
            arguments[:4],
            [
                "DELETE-PRODUCTION-RETAINED-LOAD",
                RUNNER_SHA,
                "567890",
                "567890",
            ],
        )
        self.assertEqual(
            cleanup.call_args.kwargs["diagnostic_binding"],
            {
                "source_sha": RUNNER_SHA,
                "app_sha": APP_SHA,
                "run_id": "567890",
                "load_run_id": "567890",
                "run_attempt": "1",
                "profile": "ready-vote-slo-v2",
            },
        )

    def test_retained_cleanup_dispatch_forwards_exact_never_started_proof(self) -> None:
        proof = _never_started_cleanup_proof()
        payload = validate_cleanup_payload(
            {
                "schema": 2,
                "target_sha": RUNNER_SHA,
                "control_email": "qa.control@example.test",
                "load_run_id": "567890",
                "cleanup_run_id": "678901",
                "source_binding": _binding(),
                "never_started_proof": proof,
            }
        )
        with (
            patch.object(dispatcher, "load_stdin_payload", return_value=payload),
            patch.object(dispatcher, "_run_retained_cleanup_sudo", return_value=0) as cleanup,
        ):
            self.assertEqual(
                dispatcher.main(
                    ["retained-cleanup", "--run-attempt", "2", "--load-run-id", "567890"]
                ),
                0,
            )
        arguments = cleanup.call_args.args[1]
        self.assertEqual(arguments[:4], [
            "DELETE-PRODUCTION-RETAINED-LOAD", RUNNER_SHA, "567890", "678901"
        ])
        self.assertEqual(arguments[4], "--source-binding-base64")
        self.assertEqual(
            json.loads(base64.b64decode(arguments[5], validate=True)), _binding()
        )
        self.assertEqual(cleanup.call_args.kwargs["diagnostic_binding"]["app_sha"], APP_SHA)
        self.assertEqual(arguments[6], "--never-started-proof-base64")
        self.assertEqual(
            json.loads(base64.b64decode(arguments[7], validate=True)), proof
        )

        mismatched_payload = validate_cleanup_payload(
            {
                "schema": 2,
                "target_sha": RUNNER_SHA,
                "control_email": "qa.control@example.test",
                "load_run_id": "567890",
                "cleanup_run_id": "678901",
                "source_binding": _binding(),
            }
        )
        with (
            patch.object(dispatcher, "load_stdin_payload", return_value=mismatched_payload),
            patch.object(dispatcher, "_run_retained_cleanup_sudo") as rejected_cleanup,
        ):
            self.assertEqual(
                dispatcher.main(
                    ["external-cleanup", "--run-attempt", "1", "--profile-id", "ready-vote-slo-v2"]
                ),
                2,
            )
        rejected_cleanup.assert_not_called()

        with (
            patch.object(dispatcher, "load_stdin_payload", return_value=payload),
            patch.object(dispatcher, "_run_retained_cleanup_sudo", return_value=0) as cleanup,
        ):
            self.assertEqual(
                dispatcher.main(
                    ["retained-cleanup", "--run-attempt", "1", "--load-run-id", "567890"]
                ),
                0,
            )
        self.assertEqual(
            cleanup.call_args.kwargs["diagnostic_binding"],
            {
                "source_sha": RUNNER_SHA,
                "app_sha": APP_SHA,
                "run_id": "678901",
                "load_run_id": "567890",
                "run_attempt": "1",
                "profile": "retained-load-cleanup",
            },
        )

    def test_source_binding_context_rejects_changed_or_malformed_tuple(self) -> None:
        payload = _external_payload()
        payload["source_binding"] = copy.deepcopy(_binding())
        payload["source_binding"]["baseline_identity"]["current_link_ino"] = True
        with self.assertRaises(ValueError):
            dispatcher._source_binding_context(payload)

        payload = _external_payload()
        payload["source_binding"]["runner_sha"] = "c" * 40
        with self.assertRaises(ValueError):
            dispatcher._source_binding_context(payload)

    def test_finalize_and_export_cleanup_pin_the_app_sha_and_exact_tuple(self) -> None:
        payload = validate_external_payload(_external_payload())
        payload["run_id"] = "567890"
        with (
            patch.object(dispatcher, "_current_pin_matches_host_generation", return_value=True) as pin,
            patch.object(dispatcher, "_run_retained_export_executor", return_value=0) as executor,
        ):
            self.assertEqual(dispatcher._touch_complete(payload), 0)
        pin.assert_called_once_with(
            target_sha=APP_SHA,
            expected_baseline_identity=_baseline(),
        )
        executor.assert_called_once_with(
            "touch-complete", {"schema": 1, "load_run_id": "567890"}
        )

        with (
            patch.object(dispatcher, "_current_pin_matches_host_generation", return_value=True) as pin,
            patch.object(dispatcher, "_run_retained_export_executor", return_value=0) as executor,
        ):
            self.assertEqual(
                dispatcher._remove_exports(
                    load_run_id="567890",
                    cleanup_run_id="678901",
                    target_sha=APP_SHA,
                    expected_baseline_identity=_baseline(),
                ),
                0,
            )
        pin.assert_called_once_with(
            target_sha=APP_SHA,
            expected_baseline_identity=_baseline(),
        )
        executor.assert_called_once_with(
            "remove",
            {"schema": 1, "load_run_id": "567890", "cleanup_run_id": "678901"},
        )


if __name__ == "__main__":
    unittest.main()
