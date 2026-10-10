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
    validate_external_payload,
    validate_live_payload,
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
                self.assertEqual(dispatcher._external_fixture(payload), 0)
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
        self.assertEqual(arguments[4], "--source-binding-base64")
        self.assertEqual(
            json.loads(base64.b64decode(arguments[5], validate=True)), _binding()
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
                "run_id": "567890",
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
