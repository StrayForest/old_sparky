"""Dual-source handoff and fail-closed no-op receipt contracts.

The runner SHA identifies checked-out CI/workflow/client code. A different
active app SHA is accepted only through a completed, exact baseline-reconcile
no-op receipt bound to the active release tuple and its authenticated run
attempts. These tests cover the receipt parser, artifact boundary, and the
load/cleanup/live-QA workflow source split.
"""

from __future__ import annotations

from contextlib import redirect_stdout
import copy
import base64
import email.message
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
import zipfile
from unittest.mock import patch
from unittest.mock import Mock
import yaml


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLATFORM_ROOT.parent
sys.path.insert(0, str(PLATFORM_ROOT))

from tools import platform_ci_classifier, platform_load, platform_noop_source_binding  # noqa: E402
from tools.platform_workflow_provenance import ProvenanceError  # noqa: E402
from tools.platform_noop_source_binding import (  # noqa: E402
    RECEIPT_FILE,
    parse_receipt_json,
    validate_active_source_binding,
    validate_active_runtime_binding,
    validate_active_runtime_tuple,
    validate_noop_receipt_artifact,
    validate_noop_receipt_document,
    validate_source_binding_handoff,
)
from tools import platform_workflow_input_guard  # noqa: E402
from tools import platform_workflow_remote_dispatch  # noqa: E402


RUNNER_SHA = "a" * 40
APP_SHA = "b" * 40
DEPLOY_RUN_ID = "123456"
DEPLOY_ATTEMPT = "2"
SECURITY_RUN_ID = "234567"
SECURITY_ATTEMPT = "1"
AUTODEPLOY_RUN_ID = "345678"
AUTODEPLOY_ATTEMPT = "3"
ARTIFACT_ID = "456789"
ARTIFACT_NAME = (
    f"platform-production-noop-source-receipt-{DEPLOY_RUN_ID}-{DEPLOY_ATTEMPT}"
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _baseline_identity() -> dict[str, object]:
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


def _canonical_noop_manifests() -> tuple[dict[str, object], dict[str, object]]:
    files = [".github/workflows/platform-production-autodeploy.yml"]
    incremental = platform_ci_classifier.classify(
        files,
        event="push",
        branch="dev",
        target_sha=RUNNER_SHA,
        repository_ready=True,
    )
    return incremental, incremental


def _receipt_document() -> dict[str, object]:
    incremental, cumulative = _canonical_noop_manifests()
    return {
        "schema": 1,
        "kind": "platform-production-baseline-noop",
        "runner_sha": RUNNER_SHA,
        "mode": "baseline-reconcile",
        "production_deploy": {
            "run_id": DEPLOY_RUN_ID,
            "run_attempt": DEPLOY_ATTEMPT,
        },
        "source_security": {
            "run_id": SECURITY_RUN_ID,
            "run_attempt": SECURITY_ATTEMPT,
        },
        "autodeploy": {
            "run_id": AUTODEPLOY_RUN_ID,
            "run_attempt": AUTODEPLOY_ATTEMPT,
        },
        "baseline_identity": _baseline_identity(),
        "route": {
            "no_op": True,
            "runtime_required": False,
            "cumulative_manifest_sha256": hashlib.sha256(
                _canonical_json(cumulative)
            ).hexdigest(),
            "incremental_manifest": incremental,
            "cumulative_manifest": cumulative,
        },
    }


def _expected_run_bindings() -> dict[str, str]:
    return {
        "expected_runner_sha": RUNNER_SHA,
        "expected_deploy_run_id": DEPLOY_RUN_ID,
        "expected_deploy_attempt": DEPLOY_ATTEMPT,
        "expected_security_run_id": SECURITY_RUN_ID,
        "expected_security_attempt": SECURITY_ATTEMPT,
        "expected_autodeploy_run_id": AUTODEPLOY_RUN_ID,
        "expected_autodeploy_attempt": AUTODEPLOY_ATTEMPT,
    }


def _zip_receipt(document: dict[str, object]) -> bytes:
    raw = _canonical_json(document) + b"\n"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        info = zipfile.ZipInfo(RECEIPT_FILE)
        info.external_attr = (0o100600 & 0xFFFF) << 16
        archive.writestr(info, raw)
    return stream.getvalue()


def _artifact_metadata(archive: bytes) -> dict[str, object]:
    return {
        "id": int(ARTIFACT_ID),
        "name": ARTIFACT_NAME,
        "expired": False,
        "size_in_bytes": len(archive),
        "digest": f"sha256:{hashlib.sha256(archive).hexdigest()}",
        "workflow_run": {
            "id": int(DEPLOY_RUN_ID),
            "run_attempt": int(DEPLOY_ATTEMPT),
            "head_sha": RUNNER_SHA,
            "head_branch": "dev",
        },
    }


def _validated_artifact_binding() -> dict[str, object]:
    document = _receipt_document()
    archive = _zip_receipt(document)
    return validate_noop_receipt_artifact(
        _artifact_metadata(archive),
        archive,
        **_expected_run_bindings(),
        expected_artifact_id=ARTIFACT_ID,
        expected_artifact_name=ARTIFACT_NAME,
    )


def _expected_noop_handoff_kwargs(binding: dict[str, object]) -> dict[str, object]:
    return {
        "expected_runner_sha": RUNNER_SHA,
        "expected_app_target_sha": APP_SHA,
        "expected_security_run_id": SECURITY_RUN_ID,
        "expected_security_attempt": SECURITY_ATTEMPT,
        "expected_autodeploy_run_id": AUTODEPLOY_RUN_ID,
        "expected_autodeploy_attempt": AUTODEPLOY_ATTEMPT,
        "expected_deploy_run_id": DEPLOY_RUN_ID,
        "expected_deploy_attempt": DEPLOY_ATTEMPT,
        "expected_artifact_id": ARTIFACT_ID,
        "expected_artifact_name": ARTIFACT_NAME,
        "expected_artifact_digest": binding["receipt_artifact_digest"],
    }


def _expected_same_source_handoff_kwargs() -> dict[str, object]:
    return {
        "expected_runner_sha": RUNNER_SHA,
        "expected_app_target_sha": RUNNER_SHA,
        "expected_security_run_id": None,
        "expected_security_attempt": None,
        "expected_autodeploy_run_id": None,
        "expected_autodeploy_attempt": None,
        "expected_deploy_run_id": None,
        "expected_deploy_attempt": None,
        "expected_artifact_id": None,
        "expected_artifact_name": None,
        "expected_artifact_digest": None,
    }


def _validated_source_binding_handoff() -> dict[str, object]:
    receipt = _validated_artifact_binding()
    resolved = validate_active_source_binding(
        runner_sha=RUNNER_SHA,
        active_baseline=_baseline_identity(),
        receipt_binding=receipt,
    )
    return validate_source_binding_handoff(
        resolved,
        **_expected_noop_handoff_kwargs(resolved),
    )


class NoopSourceBindingTests(unittest.TestCase):
    def test_command_line_entrypoint_rejects_help_with_nonzero_status(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(PLATFORM_ROOT / "tools" / "platform_noop_source_binding.py"),
                "--help",
            ],
            cwd=PLATFORM_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.strip(), "SOURCE_BINDING_OPERATION status=invalid"
        )

    def test_incomplete_resolver_cli_fails_before_creating_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "source-binding"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PLATFORM_ROOT / "tools" / "platform_noop_source_binding.py"),
                    "resolve-workflow-source-binding",
                    RUNNER_SHA,
                    "example/repository",
                    "https://api.github.com",
                    str(output_directory),
                ],
                cwd=PLATFORM_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertEqual(
                completed.stderr.strip(), "SOURCE_BINDING_OPERATION status=invalid"
            )
            self.assertFalse(output_directory.exists())

    def test_workflow_shaped_resolver_cli_fails_closed_without_token_or_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "source-binding"
            workflow_output = Path(temporary_directory) / "github-output"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PLATFORM_ROOT / "tools" / "platform_noop_source_binding.py"),
                    "resolve-workflow-source-binding",
                    RUNNER_SHA,
                    "example/repository",
                    "https://api.github.com",
                    str(output_directory),
                    str(workflow_output),
                ],
                cwd=PLATFORM_ROOT,
                env={"PATH": os.environ.get("PATH", "")},
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.strip(), "SOURCE_BINDING_RESOLUTION status=failed"
            )
            self.assertFalse(output_directory.exists())
            self.assertFalse(workflow_output.exists())

    def test_exact_noop_receipt_derives_app_sha_without_replacing_runner_sha(self) -> None:
        document = _receipt_document()
        binding = validate_noop_receipt_document(
            document, **_expected_run_bindings()
        )

        self.assertEqual(binding["binding_mode"], "verified-noop")
        self.assertEqual(binding["runner_sha"], RUNNER_SHA)
        self.assertEqual(binding["app_target_sha"], APP_SHA)
        self.assertEqual(binding["baseline_identity"], _baseline_identity())
        self.assertNotEqual(binding["runner_sha"], binding["app_target_sha"])

    def test_same_source_binding_needs_no_receipt_and_rejects_substitution(self) -> None:
        baseline = _baseline_identity()
        baseline["source_sha"] = RUNNER_SHA
        same_source = validate_active_source_binding(
            runner_sha=RUNNER_SHA, active_baseline=baseline
        )
        self.assertEqual(same_source["binding_mode"], "same-source")
        self.assertEqual(same_source["app_target_sha"], RUNNER_SHA)
        with self.assertRaises(ProvenanceError):
            validate_active_source_binding(
                runner_sha=RUNNER_SHA,
                active_baseline=baseline,
                receipt_binding=_validated_artifact_binding(),
            )

    def test_mismatched_source_requires_exact_receipt_and_live_tuple(self) -> None:
        receipt = _validated_artifact_binding()
        accepted = validate_active_source_binding(
            runner_sha=RUNNER_SHA,
            active_baseline=_baseline_identity(),
            receipt_binding=receipt,
        )
        self.assertEqual(accepted["app_target_sha"], APP_SHA)

        with self.assertRaises(ProvenanceError):
            validate_active_source_binding(
                runner_sha=RUNNER_SHA,
                active_baseline=_baseline_identity(),
            )
        changed_baseline = _baseline_identity()
        changed_baseline["release_ino"] = 999
        with self.assertRaises(ProvenanceError):
            validate_active_source_binding(
                runner_sha=RUNNER_SHA,
                active_baseline=changed_baseline,
                receipt_binding=receipt,
            )

    def test_resolver_handoff_and_runtime_check_rebind_exact_release_tuple(self) -> None:
        receipt = _validated_artifact_binding()
        resolved = validate_active_source_binding(
            runner_sha=RUNNER_SHA,
            active_baseline=_baseline_identity(),
            receipt_binding=receipt,
        )
        handoff = validate_source_binding_handoff(
            resolved,
            **_expected_noop_handoff_kwargs(resolved),
        )
        accepted = validate_active_runtime_binding(
            handoff,
            _baseline_identity(),
            **_expected_noop_handoff_kwargs(handoff),
        )
        self.assertEqual(accepted["runner_sha"], RUNNER_SHA)
        self.assertEqual(accepted["app_target_sha"], APP_SHA)
        self.assertEqual(accepted["binding_mode"], "verified-noop")

        changed_values: dict[str, object] = {
            "schema": 2,
            "source_sha": "d" * 40,
            "release_slug": "gha-123457-1-bbbbbbbbbbbb",
            "release_json_sha256": "e" * 64,
            "current_link_dev": 200,
            "current_link_ino": 201,
            "release_dev": 200,
            "release_ino": 202,
            "pending_operation": True,
        }
        for field, value in changed_values.items():
            changed = _baseline_identity()
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ProvenanceError):
                validate_active_runtime_binding(
                    handoff,
                    changed,
                    **_expected_noop_handoff_kwargs(handoff),
                )

    def test_installed_runtime_tuple_gate_rejects_mutated_binding(self) -> None:
        binding = _validated_source_binding_handoff()
        accepted = validate_active_runtime_tuple(
            binding, _baseline_identity(), expected_runner_sha=RUNNER_SHA
        )
        self.assertEqual(accepted["runner_sha"], RUNNER_SHA)
        self.assertEqual(accepted["app_target_sha"], APP_SHA)
        self.assertEqual(accepted["baseline_identity"], _baseline_identity())

        mutations: dict[str, object] = {
            "schema": 2,
            "source_sha": "d" * 40,
            "release_slug": "gha-123457-1-bbbbbbbbbbbb",
            "release_json_sha256": "e" * 64,
            "current_link_dev": 200,
            "current_link_ino": 201,
            "release_dev": 200,
            "release_ino": 202,
            "pending_operation": True,
        }
        for field, value in mutations.items():
            changed = _baseline_identity()
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ProvenanceError):
                validate_active_runtime_tuple(
                    binding, changed, expected_runner_sha=RUNNER_SHA
                )

        with self.assertRaises(ProvenanceError):
            validate_active_runtime_tuple(
                binding, _baseline_identity(), expected_runner_sha=APP_SHA
            )
        for field, value in (
            ("receipt_artifact_id", True),
            ("receipt_artifact_name", "wrong-artifact"),
            ("receipt_artifact_digest", "sha256:" + "0" * 64),
            ("source_security_run_attempt", True),
            ("autodeploy_run_id", "0"),
            ("production_deploy_run_attempt", "x"),
        ):
            malformed = {**binding, field: value}
            with self.subTest(binding_field=field), self.assertRaises(ProvenanceError):
                validate_active_runtime_tuple(
                    malformed, _baseline_identity(), expected_runner_sha=RUNNER_SHA
                )

    def test_same_source_handoff_rechecks_runtime_identity_without_receipt(self) -> None:
        baseline = _baseline_identity()
        baseline["source_sha"] = RUNNER_SHA
        resolved = validate_active_source_binding(
            runner_sha=RUNNER_SHA,
            active_baseline=baseline,
        )
        handoff = validate_source_binding_handoff(
            resolved,
            **_expected_same_source_handoff_kwargs(),
        )
        accepted = validate_active_runtime_binding(
            handoff,
            baseline,
            **_expected_same_source_handoff_kwargs(),
        )
        self.assertEqual(accepted["binding_mode"], "same-source")
        changed = dict(baseline)
        changed["source_sha"] = APP_SHA
        with self.assertRaises(ProvenanceError):
            validate_active_runtime_binding(
                handoff,
                changed,
                **_expected_same_source_handoff_kwargs(),
            )

    def test_source_binding_handoff_rejects_forged_receipt_metadata(self) -> None:
        receipt = _validated_artifact_binding()
        resolved = validate_active_source_binding(
            runner_sha=RUNNER_SHA,
            active_baseline=_baseline_identity(),
            receipt_binding=receipt,
        )
        expected = _expected_noop_handoff_kwargs(resolved)
        mutations = (
            {**resolved, "unexpected": "closed schema"},
            {**resolved, "runner_sha": APP_SHA},
            {**resolved, "app_target_sha": "d" * 40},
            {**resolved, "receipt_document_sha256": "bad"},
            {**resolved, "cumulative_manifest_sha256": "bad"},
            {**resolved, "receipt_archive_sha256": "bad"},
            {**resolved, "receipt_artifact_id": "0"},
            {**resolved, "receipt_artifact_name": "unrelated.zip"},
            {**resolved, "receipt_artifact_digest": "sha256:" + "0" * 64},
            {**resolved, "source_security_run_id": "999999"},
            {**resolved, "source_security_run_attempt": "0"},
            {**resolved, "autodeploy_run_id": "999999"},
            {**resolved, "autodeploy_run_attempt": "9"},
            {**resolved, "production_deploy_run_id": "999999"},
            {**resolved, "production_deploy_run_attempt": "9"},
        )
        for index, binding in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ProvenanceError):
                validate_source_binding_handoff(
                    binding,
                    **expected,
                )

    def test_closed_c2_guards_accept_schema2_only_with_runner_bound_source_binding(self) -> None:
        binding = _validated_source_binding_handoff()
        external = {
            "schema": 2,
            "confirmation": "RUN-PRODUCTION-EXTERNAL-LOAD",
            "target_sha": RUNNER_SHA,
            "control_email": "control@example.invalid",
            "setup_concurrency": "8",
            "run_id": "567890",
            "profile": "external-vote",
            "tournament_count": "1",
            "users_per_tournament": "14",
            "timeout_diagnostics": "false",
            "source_binding": binding,
        }
        cleanup = {
            "schema": 2,
            "target_sha": RUNNER_SHA,
            "control_email": "control@example.invalid",
            "load_run_id": "567890",
            "cleanup_run_id": "678901",
            "source_binding": binding,
        }
        live = {
            "schema": 2,
            "base_url": "https://old-sparky.com",
            "provision": "false",
            "marker": "",
            "target_sha": RUNNER_SHA,
            "source_binding": binding,
        }

        validated = (
            platform_workflow_input_guard.validate_external_payload(external),
            platform_workflow_input_guard.validate_cleanup_payload(cleanup),
            platform_workflow_input_guard.validate_live_payload(live),
        )
        for payload in validated:
            self.assertEqual(payload["schema"], "2")
            self.assertEqual(payload["target_sha"], RUNNER_SHA)
            self.assertEqual(payload["source_binding"]["runner_sha"], RUNNER_SHA)
            self.assertEqual(payload["source_binding"]["app_target_sha"], APP_SHA)

        mutations = (
            {**binding, "runner_sha": APP_SHA},
            {**binding, "app_target_sha": "d" * 40},
            {**binding, "unexpected": "closed schema"},
            {**binding, "receipt_artifact_digest": "sha256:" + "0" * 64},
        )
        for index, bad_binding in enumerate(mutations):
            with self.subTest(index=index):
                for validator, payload in (
                    (platform_workflow_input_guard.validate_external_payload, external),
                    (platform_workflow_input_guard.validate_cleanup_payload, cleanup),
                    (platform_workflow_input_guard.validate_live_payload, live),
                ):
                    with self.assertRaises(platform_workflow_input_guard.WorkflowInputError):
                        validator({**payload, "source_binding": bad_binding})

        for validator, payload in (
            (platform_workflow_input_guard.validate_external_payload, external),
            (platform_workflow_input_guard.validate_cleanup_payload, cleanup),
            (platform_workflow_input_guard.validate_live_payload, live),
        ):
            with self.assertRaises(platform_workflow_input_guard.WorkflowInputError):
                validator({**payload, "source_binding": None})
            with self.assertRaises(platform_workflow_input_guard.WorkflowInputError):
                validator({**payload, "schema": True})

    def test_legacy_c2_guard_payloads_remain_same_source_only(self) -> None:
        external = {
            "schema": 1,
            "confirmation": "RUN-PRODUCTION-EXTERNAL-LOAD",
            "target_sha": RUNNER_SHA,
            "control_email": "control@example.invalid",
            "setup_concurrency": "8",
            "run_id": "567890",
            "profile": "external-vote",
            "tournament_count": "1",
            "users_per_tournament": "14",
            "timeout_diagnostics": "false",
        }
        cleanup = {
            "schema": 1,
            "target_sha": RUNNER_SHA,
            "control_email": "control@example.invalid",
            "load_run_id": "567890",
            "cleanup_run_id": "678901",
        }
        live = {
            "schema": 1,
            "base_url": "https://old-sparky.com",
            "provision": "false",
            "marker": "",
            "target_sha": RUNNER_SHA,
        }
        for validator, payload in (
            (platform_workflow_input_guard.validate_external_payload, external),
            (platform_workflow_input_guard.validate_cleanup_payload, cleanup),
            (platform_workflow_input_guard.validate_live_payload, live),
        ):
            self.assertEqual(validator(payload)["schema"], "1")
            no_binding = {**payload, "target_sha": APP_SHA, "source_binding": _validated_source_binding_handoff()}
            with self.assertRaises(platform_workflow_input_guard.WorkflowInputError):
                validator(no_binding)

    def test_dispatch_context_keeps_runner_sha_and_passes_verified_app_binding(self) -> None:
        binding = _validated_source_binding_handoff()
        external = {
            "schema": 2,
            "confirmation": "RUN-PRODUCTION-EXTERNAL-LOAD",
            "target_sha": RUNNER_SHA,
            "control_email": "control@example.invalid",
            "setup_concurrency": "8",
            "run_id": "567890",
            "profile": "external-vote",
            "tournament_count": "1",
            "users_per_tournament": "14",
            "timeout_diagnostics": "false",
            "source_binding": binding,
        }
        validated = platform_workflow_input_guard.validate_external_payload(external)
        app_sha, baseline, suffix = platform_workflow_remote_dispatch._source_binding_context(
            validated
        )
        self.assertEqual(validated["target_sha"], RUNNER_SHA)
        self.assertEqual(app_sha, APP_SHA)
        self.assertEqual(baseline, _baseline_identity())
        self.assertEqual(suffix[0], "--source-binding-base64")
        decoded = json.loads(base64.b64decode(suffix[1], validate=True))
        self.assertEqual(decoded, binding)

        same_source = dict(validated)
        same_source["schema"] = "1"
        same_source.pop("source_binding")
        app_sha, baseline, suffix = platform_workflow_remote_dispatch._source_binding_context(
            same_source
        )
        self.assertEqual(app_sha, RUNNER_SHA)
        self.assertIsNone(baseline)
        self.assertEqual(suffix, [])

    def test_external_fixture_dispatch_forwards_runner_and_binding_only_to_fixed_helper(self) -> None:
        binding = _validated_source_binding_handoff()
        payload = platform_workflow_input_guard.validate_external_payload(
            {
                "schema": 2,
                "confirmation": "RUN-PRODUCTION-EXTERNAL-LOAD",
                "target_sha": RUNNER_SHA,
                "control_email": "control@example.invalid",
                "setup_concurrency": "8",
                "run_id": "567890",
                "profile": "external-vote",
                "tournament_count": "1",
                "users_per_tournament": "14",
                "timeout_diagnostics": "false",
                "source_binding": binding,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools_root = root / "tools"
            tools_root.mkdir()
            helper = tools_root / "platform_production_external_fixture_qa.sh"
            helper.write_text("#!/bin/sh\n", encoding="ascii")
            helper.chmod(0o755)
            child = Mock()
            written: list[bytes] = []
            child.stdin = Mock()
            child.stdin.write.side_effect = written.append
            with patch.object(platform_workflow_remote_dispatch, "ACTIVE_TOOLS_DIR", tools_root), \
                patch.object(platform_workflow_remote_dispatch, "EXTERNAL_HELPER", helper), \
                patch.object(platform_workflow_remote_dispatch.subprocess, "Popen", return_value=child) as popen:
                self.assertEqual(platform_workflow_remote_dispatch._external_fixture(payload), 0)

        command = popen.call_args.args[0]
        self.assertEqual(command[5], RUNNER_SHA)
        self.assertEqual(command[-2], "--source-binding-base64")
        self.assertEqual(
            json.loads(base64.b64decode(command[-1], validate=True)),
            binding,
        )
        self.assertNotIn("control@example.invalid", command)
        self.assertEqual(
            written,
            [b'{"schema":1,"control_email":"control@example.invalid"}\n'],
        )

    def test_cleanup_dispatch_keeps_runner_identity_and_forwards_verified_binding(self) -> None:
        binding = _validated_source_binding_handoff()
        payload = platform_workflow_input_guard.validate_cleanup_payload(
            {
                "schema": 2,
                "target_sha": RUNNER_SHA,
                "control_email": "control@example.invalid",
                "load_run_id": "567890",
                "cleanup_run_id": "678901",
                "source_binding": binding,
            }
        )
        with patch.object(
            platform_workflow_remote_dispatch,
            "load_stdin_payload",
            return_value=payload,
        ), patch.object(
            platform_workflow_remote_dispatch,
            "_run_retained_cleanup_sudo",
            return_value=0,
        ) as cleanup:
            self.assertEqual(
                platform_workflow_remote_dispatch.main(["external-cleanup"]), 0
            )

        helper, arguments = cleanup.call_args.args
        self.assertEqual(helper, platform_workflow_remote_dispatch.CLEANUP_HELPER)
        self.assertEqual(
            arguments[:4],
            [
                platform_workflow_remote_dispatch.DELETE_CONFIRMATION,
                RUNNER_SHA,
                "567890",
                "678901",
            ],
        )
        self.assertEqual(arguments[-2], "--source-binding-base64")
        self.assertEqual(
            json.loads(base64.b64decode(arguments[-1], validate=True)), binding
        )
        self.assertEqual(
            cleanup.call_args.kwargs["control_email"], "control@example.invalid"
        )

    def test_live_qa_dispatchers_keep_runner_sha_and_append_binding(self) -> None:
        binding = _validated_source_binding_handoff()
        payload = platform_workflow_input_guard.validate_live_payload(
            {
                "schema": 2,
                "base_url": "https://old-sparky.com",
                "provision": "false",
                "marker": "",
                "target_sha": RUNNER_SHA,
                "source_binding": binding,
            }
        )
        with patch.object(
            platform_workflow_remote_dispatch,
            "load_stdin_payload",
            return_value=payload,
        ), patch.object(
            platform_workflow_remote_dispatch, "_run_sudo", return_value=0
        ) as user_qa:
            self.assertEqual(
                platform_workflow_remote_dispatch.main(["live-user-qa"]), 0
            )
        user_arguments = user_qa.call_args.args[1]
        self.assertEqual(user_arguments[0], RUNNER_SHA)
        self.assertEqual(user_arguments[-2], "--source-binding-base64")
        self.assertEqual(
            json.loads(base64.b64decode(user_arguments[-1], validate=True)), binding
        )

        with patch.object(
            platform_workflow_remote_dispatch,
            "load_stdin_payload",
            return_value=payload,
        ), patch.object(
            platform_workflow_remote_dispatch,
            "_run_trusted_live_launch",
            return_value=0,
        ) as public_qa:
            self.assertEqual(platform_workflow_remote_dispatch.main(["live-launch"]), 0)
        public_arguments = public_qa.call_args.args[0]
        self.assertEqual(public_arguments[:3], ["https://old-sparky.com", "false", ""])
        self.assertEqual(public_arguments[-1], RUNNER_SHA)
        self.assertEqual(public_arguments[3], "--source-binding-base64")
        self.assertEqual(
            json.loads(base64.b64decode(public_arguments[4], validate=True)), binding
        )

    def test_finalizer_and_export_cleanup_pin_app_sha_and_full_tuple(self) -> None:
        binding = _validated_source_binding_handoff()
        payload = {
            "schema": "2",
            "run_id": "567890",
            "target_sha": RUNNER_SHA,
            "source_binding": binding,
        }
        with patch.object(
            platform_workflow_remote_dispatch,
            "_current_pin_matches_host_generation",
            return_value=True,
        ) as pin, patch.object(
            platform_workflow_remote_dispatch,
            "_run_retained_export_executor",
            return_value=0,
        ) as executor:
            self.assertEqual(platform_workflow_remote_dispatch._touch_complete(payload), 0)
        pin.assert_called_once_with(
            target_sha=APP_SHA,
            expected_baseline_identity=_baseline_identity(),
        )
        self.assertEqual(executor.call_args.args[0], "touch-complete")

        with patch.object(
            platform_workflow_remote_dispatch,
            "_current_pin_matches_host_generation",
            return_value=False,
        ) as pin, patch.object(
            platform_workflow_remote_dispatch, "_run_retained_export_executor"
        ) as executor:
            self.assertEqual(platform_workflow_remote_dispatch._touch_complete(payload), 1)
        pin.assert_called_once_with(
            target_sha=APP_SHA,
            expected_baseline_identity=_baseline_identity(),
        )
        executor.assert_not_called()

        with patch.object(
            platform_workflow_remote_dispatch,
            "_current_pin_matches_host_generation",
            return_value=True,
        ) as pin, patch.object(
            platform_workflow_remote_dispatch,
            "_run_retained_export_executor",
            return_value=0,
        ) as executor:
            self.assertEqual(
                platform_workflow_remote_dispatch._remove_exports(
                    load_run_id="567890",
                    cleanup_run_id="678901",
                    target_sha=APP_SHA,
                    expected_baseline_identity=_baseline_identity(),
                ),
                0,
            )
        pin.assert_called_once_with(
            target_sha=APP_SHA,
            expected_baseline_identity=_baseline_identity(),
        )
        self.assertEqual(executor.call_args.args[0], "remove")

        changed = _baseline_identity()
        changed["release_ino"] += 1  # type: ignore[operator]
        with patch.object(
            platform_workflow_remote_dispatch,
            "_current_pin_matches_host_generation",
            return_value=False,
        ) as pin, patch.object(
            platform_workflow_remote_dispatch,
            "_run_retained_export_executor",
        ) as executor:
            self.assertEqual(
                platform_workflow_remote_dispatch._remove_exports(
                    load_run_id="567890",
                    cleanup_run_id="678901",
                    target_sha=APP_SHA,
                    expected_baseline_identity=changed,
                ),
                1,
            )
        pin.assert_called_once()
        executor.assert_not_called()

    def test_host_generation_without_binding_capability_fails_closed(self) -> None:
        manifest_bytes = b'{"schema":1}\n'
        legacy_capabilities = (
            b"capability=release_baseline\n"
            b"capability=retained_load_export_cleanup\n"
        )
        supported_capabilities = legacy_capabilities + b"capability=retained_load_source_binding\n"

        def stable_file(path: Path, *, mode: int) -> bytes | None:
            del mode
            if path.name == "manifest.json":
                return manifest_bytes
            if path.name == "capabilities.txt":
                return capabilities
            return None

        for capabilities, expected in (
            (legacy_capabilities, False),
            (supported_capabilities, True),
        ):
            with self.subTest(capability=expected), \
                patch.object(platform_workflow_remote_dispatch, "_trusted_generation", return_value=True), \
                patch.object(platform_workflow_remote_dispatch, "_trusted_data", return_value=True), \
                patch.object(platform_workflow_remote_dispatch, "_stable_host_file", side_effect=stable_file), \
                patch.object(platform_workflow_remote_dispatch, "_verify_host_tools_contract", return_value=True):
                self.assertEqual(
                    platform_workflow_remote_dispatch._host_baseline_generation_ready(),
                    expected,
                )

    def test_c_cleanup_baseline_comparator_rejects_each_tuple_mutation(self) -> None:
        expected = _baseline_identity()
        self.assertTrue(
            platform_workflow_remote_dispatch._baseline_identity_matches(
                expected, _baseline_identity()
            )
        )
        mutations: dict[str, object] = {
            "schema": 2,
            "source_sha": "d" * 40,
            "release_slug": "gha-123457-1-bbbbbbbbbbbb",
            "release_json_sha256": "e" * 64,
            "current_link_dev": 200,
            "current_link_ino": 201,
            "release_dev": 200,
            "release_ino": 202,
            "pending_operation": True,
        }
        for field, value in mutations.items():
            changed = _baseline_identity()
            changed[field] = value
            with self.subTest(field=field):
                self.assertFalse(
                    platform_workflow_remote_dispatch._baseline_identity_matches(
                        expected, changed
                    )
                )

    def test_external_workflow_uses_shared_authenticated_resolver_and_redirect_guards(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/platform-production-external-load.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        resolver = next(
            step
            for step in workflow["jobs"]["validate-external-inputs"]["steps"]
            if step.get("id") == "resolve-source-binding"
        )
        self.assertIn("tools/platform_noop_source_binding.py", resolver["run"])
        self.assertIn("resolve-workflow-source-binding", resolver["run"])
        self.assertIn('"$TARGET_SHA" "$GITHUB_REPOSITORY" "$GITHUB_API_URL"', resolver["run"])
        self.assertEqual(resolver["env"]["GH_TOKEN"], "${{ github.token }}")
        app_sha_consumer = next(
            step
            for step in workflow["jobs"]["validate-external-inputs"]["steps"]
            if step.get("name") == "Validate explicit external production load"
        )
        self.assertIn("APP_TARGET_SHA", app_sha_consumer["env"])

        archive = _zip_receipt(_receipt_document())
        metadata = _artifact_metadata(archive)
        deploy_run = {
            "id": int(DEPLOY_RUN_ID),
            "run_attempt": int(DEPLOY_ATTEMPT),
            "workflow_id": 11,
            "head_sha": RUNNER_SHA,
            "head_branch": "dev",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
        }
        security_run = {
            "id": int(SECURITY_RUN_ID),
            "run_attempt": int(SECURITY_ATTEMPT),
            "workflow_id": 1,
            "head_sha": RUNNER_SHA,
            "head_branch": "dev",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
        }
        autodeploy_run = {
            "id": int(AUTODEPLOY_RUN_ID),
            "run_attempt": int(AUTODEPLOY_ATTEMPT),
            "workflow_id": 13,
            "head_sha": RUNNER_SHA,
            "head_branch": "dev",
            "event": "workflow_run",
            "status": "completed",
            "conclusion": "success",
        }

        class Response:
            status = 200

            def __init__(self, body: bytes, reads: list[int] | None = None) -> None:
                self.body = body
                self.reads = reads

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, limit: int = -1) -> bytes:
                if self.reads is not None:
                    self.reads.append(limit)
                return self.body if limit < 0 else self.body[:limit]

        def run_resolver(
            *,
            include_artifact: bool,
            duplicate_artifact: bool = False,
            wrong_event_for: str | None = None,
            boolean_field_for: str | None = None,
        ) -> tuple[dict[str, object], list[str], object, object, list[int]]:
            calls: list[str] = []
            redirects: list[object] = []
            archive_read_limits: list[int] = []
            security_run["event"] = "push"
            security_run["run_attempt"] = int(SECURITY_ATTEMPT)
            security_run["workflow_id"] = 1
            autodeploy_run["event"] = "workflow_run"
            autodeploy_run["run_attempt"] = int(AUTODEPLOY_ATTEMPT)
            if wrong_event_for == "security":
                security_run["event"] = "workflow_dispatch"
            elif wrong_event_for == "autodeploy":
                autodeploy_run["event"] = "push"
            if boolean_field_for == "security_attempt":
                security_run["run_attempt"] = True
            elif boolean_field_for == "security_workflow_id":
                security_run["workflow_id"] = True
            artifact_rows = (
                [{"id": int(ARTIFACT_ID), "name": ARTIFACT_NAME}]
                * (2 if duplicate_artifact else 1)
                if include_artifact else []
            )

            class BoundOpener:
                def __init__(self, handler: object) -> None:
                    self.handler = handler

                def open(self, request: object, timeout: int) -> Response:
                    del timeout
                    url = request.full_url  # type: ignore[attr-defined]
                    calls.append(url)
                    parsed = urllib.parse.urlsplit(url)
                    path = parsed.path.removeprefix("/repos/StrayForest/old_sparky")
                    if path == "/actions/workflows/platform-production-deploy.yml":
                        body = {"id": 11, "path": ".github/workflows/platform-production-deploy.yml", "state": "active"}
                    elif path == "/actions/workflows/platform-security.yml":
                        body = {"id": 1, "path": ".github/workflows/platform-security.yml", "state": "active"}
                    elif path == "/actions/workflows/platform-production-autodeploy.yml":
                        body = {"id": 13, "path": ".github/workflows/platform-production-autodeploy.yml", "state": "active"}
                    elif path == "/actions/workflows/platform-production-deploy.yml/runs":
                        body = {"total_count": 1, "workflow_runs": [deploy_run]}
                    elif path == f"/actions/runs/{DEPLOY_RUN_ID}/artifacts":
                        body = {"total_count": len(artifact_rows), "artifacts": artifact_rows}
                    elif path == f"/actions/runs/{SECURITY_RUN_ID}":
                        body = security_run
                    elif path == f"/actions/runs/{AUTODEPLOY_RUN_ID}":
                        body = autodeploy_run
                    elif path == f"/actions/artifacts/{ARTIFACT_ID}":
                        body = metadata
                    elif path == f"/actions/artifacts/{ARTIFACT_ID}/zip":
                        redirect_url = "https://objects.example.invalid/noop-receipt.zip"
                        redirected = self.handler.redirect_request(
                            request, None, 302, "Found", {"Location": redirect_url}, redirect_url
                        )
                        if redirected is None:
                            raise AssertionError("artifact redirect was rejected")
                        redirects.append(redirected)
                        return Response(archive, archive_read_limits)
                    else:
                        raise AssertionError(f"unexpected source-binding API route: {path}")
                    return Response(json.dumps(body).encode("ascii"))

            class Transport:
                def __init__(self) -> None:
                    self.api_handler: object | None = None
                    self.artifact_handler: object | None = None

                def build_opener(self, handler: object) -> BoundOpener:
                    if type(handler).__name__ == "_SameOriginAPIRedirect":
                        self.api_handler = handler
                    elif type(handler).__name__ == "_SafeArtifactRedirect":
                        self.artifact_handler = handler
                    else:
                        raise AssertionError("unexpected source-binding redirect handler")
                    return BoundOpener(handler)

            transport = Transport()
            resolved = platform_noop_source_binding.resolve_workflow_source_binding(
                runner_sha=RUNNER_SHA,
                repository="StrayForest/old_sparky",
                token="unit-test-token",
                api_base="https://api.github.com",
                opener=transport,
            )
            self.assertIsNotNone(transport.api_handler)
            self.assertIsNotNone(transport.artifact_handler)
            return resolved, calls, transport.api_handler, transport.artifact_handler, archive_read_limits

        binding, api_calls, api_redirect, artifact_redirect, read_limits = run_resolver(include_artifact=True)
        self.assertEqual(binding["runner_sha"], RUNNER_SHA)
        self.assertEqual(binding["app_target_sha"], APP_SHA)
        self.assertEqual(binding["source_binding"], _validated_source_binding_handoff())
        self.assertTrue(all("unit-test-token" not in url for url in api_calls))
        self.assertEqual(sum(url.endswith(f"/actions/runs/{SECURITY_RUN_ID}") for url in api_calls), 1)
        self.assertEqual(sum(url.endswith(f"/actions/runs/{AUTODEPLOY_RUN_ID}") for url in api_calls), 1)
        self.assertEqual(read_limits, [platform_noop_source_binding.MAX_ARCHIVE_BYTES + 1])

        class MockHTTPResponse(io.BytesIO):
            def __init__(self, body: bytes, url: str, code: int, headers: object) -> None:
                super().__init__(body)
                self.url = url
                self.code = code
                self.status = code
                self.headers = headers
                self.msg = "mock response"

            def geturl(self) -> str:
                return self.url

            def getcode(self) -> int:
                return self.code

            def info(self) -> object:
                return self.headers

        class MockHTTPSHandler(urllib.request.BaseHandler):
            handler_order = 400

            def __init__(self, redirect_to: str, seen: list[tuple[str, dict[str, str | None]]]) -> None:
                self.redirect_to = redirect_to
                self.seen = seen

            def https_open(self, request: urllib.request.Request) -> MockHTTPResponse:
                self.seen.append(
                    (
                        request.full_url,
                        {
                            name: request.get_header(name)
                            for name in ("Authorization", "Proxy-authorization", "Cookie", "Cookie2")
                        },
                    )
                )
                headers = email.message.Message()
                if len(self.seen) == 1:
                    headers["Location"] = self.redirect_to
                    return MockHTTPResponse(b"", request.full_url, 302, headers)
                return MockHTTPResponse(b"ok", request.full_url, 200, headers)

        def actual_opener_redirects(
            handler: object, redirect_to: str, *, expect_follow: bool
        ) -> list[tuple[str, dict[str, str | None]]]:
            seen: list[tuple[str, dict[str, str | None]]] = []
            mock_handler = MockHTTPSHandler(redirect_to, seen)
            opener = urllib.request.build_opener(handler, mock_handler)
            request = urllib.request.Request(
                "https://api.github.com/start",
                headers={
                    "Authorization": "Bearer fake-token",
                    "Proxy-Authorization": "Basic fake-proxy",
                    "Cookie": "sid=private",
                    "Cookie2": "legacy=private",
                },
            )
            if expect_follow:
                with opener.open(request, timeout=1) as response:
                    self.assertEqual(response.read(), b"ok")
                self.assertEqual(len(seen), 2)
            else:
                with self.assertRaises(urllib.error.HTTPError):
                    opener.open(request, timeout=1)
                self.assertEqual(len(seen), 1)
            return seen

        api_same_origin = actual_opener_redirects(
            platform_noop_source_binding._SameOriginAPIRedirect(),
            "https://api.github.com/next",
            expect_follow=True,
        )
        self.assertEqual(api_same_origin[1][1]["Authorization"], "Bearer fake-token")
        self.assertEqual(api_same_origin[1][1]["Cookie"], "sid=private")
        for rejected_api_target in (
            "https://objects.example.invalid/next",
            "http://api.github.com/next",
            "https://user:pass@api.github.com/next",
        ):
            with self.subTest(rejected_api_target=rejected_api_target):
                actual_opener_redirects(
                    platform_noop_source_binding._SameOriginAPIRedirect(),
                    rejected_api_target,
                    expect_follow=False,
                )
        artifact_cross_host = actual_opener_redirects(
            platform_noop_source_binding._SafeArtifactRedirect(),
            "https://objects.example.invalid/next",
            expect_follow=True,
        )
        self.assertEqual(artifact_cross_host[1][0], "https://objects.example.invalid/next")
        self.assertTrue(all(value is None for value in artifact_cross_host[1][1].values()))
        artifact_same_origin = actual_opener_redirects(
            platform_noop_source_binding._SafeArtifactRedirect(),
            "https://api.github.com/next",
            expect_follow=True,
        )
        self.assertEqual(artifact_same_origin[1][1]["Authorization"], "Bearer fake-token")
        self.assertEqual(artifact_same_origin[1][1]["Cookie"], "sid=private")
        for rejected_artifact_target in (
            "http://objects.example.invalid/next",
            "https://user:pass@objects.example.invalid/next",
        ):
            with self.subTest(rejected_artifact_target=rejected_artifact_target):
                actual_opener_redirects(
                    platform_noop_source_binding._SafeArtifactRedirect(),
                    rejected_artifact_target,
                    expect_follow=False,
                )

        request = urllib.request.Request(
            "https://api.github.com/repos/StrayForest/old_sparky",
            headers={
                "Authorization": "Bearer fake-token",
                "Proxy-Authorization": "Basic fake-proxy",
                "Cookie": "sid=private",
                "Cookie2": "legacy=private",
            },
        )
        api_same = api_redirect.redirect_request(
            request, None, 302, "Found", {}, "https://api.github.com/next"
        )
        self.assertIsNotNone(api_same)
        self.assertEqual(api_same.get_header("Authorization"), "Bearer fake-token")
        self.assertIsNone(
            api_redirect.redirect_request(
                request, None, 302, "Found", {}, "https://blob.example.invalid/next"
            )
        )
        self.assertIsNone(
            api_redirect.redirect_request(
                request, None, 302, "Found", {}, "http://api.github.com/next"
            )
        )
        self.assertIsNone(
            api_redirect.redirect_request(
                request, None, 302, "Found", {}, "https://user:pass@api.github.com/next"
            )
        )
        artifact_same = artifact_redirect.redirect_request(
            request, None, 302, "Found", {}, "https://api.github.com/next"
        )
        self.assertEqual(artifact_same.get_header("Authorization"), "Bearer fake-token")
        artifact_cross = artifact_redirect.redirect_request(
            request, None, 302, "Found", {}, "https://objects.example.invalid/next"
        )
        self.assertIsNotNone(artifact_cross)
        for name in ("Authorization", "Proxy-Authorization", "Cookie", "Cookie2"):
            self.assertIsNone(artifact_cross.get_header(name))
        for target in (
            "http://objects.example.invalid/next",
            "https://user:pass@objects.example.invalid/next",
        ):
            self.assertIsNone(artifact_redirect.redirect_request(request, None, 302, "Found", {}, target))

        for wrong_event_for in ("security", "autodeploy"):
            with self.subTest(wrong_event_for=wrong_event_for), self.assertRaises(ProvenanceError):
                run_resolver(include_artifact=True, wrong_event_for=wrong_event_for)
        for boolean_field_for in ("security_attempt", "security_workflow_id"):
            with self.subTest(boolean_field_for=boolean_field_for), self.assertRaises(ProvenanceError):
                run_resolver(include_artifact=True, boolean_field_for=boolean_field_for)
        empty_binding, *_ = run_resolver(include_artifact=False)
        self.assertEqual(empty_binding, {
            "schema": 1,
            "runner_sha": RUNNER_SHA,
            "app_target_sha": RUNNER_SHA,
            "source_binding": None,
        })
        with self.assertRaises(ProvenanceError):
            run_resolver(include_artifact=True, duplicate_artifact=True)

    def test_live_qa_secret_consumers_use_inline_authenticated_handoff(self) -> None:
        for filename, marker, verifier in (
            (
                "platform-live-launch.yml",
                "platform-live-launch-input",
                "LIVE_HANDOFF status=verified",
            ),
            (
                "platform-live-user-qa.yml",
                "platform-live-user-qa-input",
                "LIVE_USER_HANDOFF_VERIFIED",
            ),
        ):
            source = (REPO_ROOT / ".github/workflows" / filename).read_text(
                encoding="utf-8"
            )
            secret_job = source.split("  live-", 1)[1]
            self.assertIn(marker, source)
            self.assertIn(verifier, source)
            self.assertNotIn("actions/checkout", secret_job)
            self.assertNotIn("platform_noop_source_binding.py", secret_job)
            self.assertIn("HANDOFF_ARTIFACT_ID", secret_job)
            self.assertIn("GITHUB_RUN_ATTEMPT", secret_job)
            self.assertIn("workflow_dispatch", secret_job)
            self.assertIn("in_progress", secret_job)

    def test_external_cleanup_handoff_keeps_runner_sha_and_nested_app_binding(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/platform-production-external-load.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        step = next(
            item
            for item in workflow["jobs"]["fixture-finalize"]["steps"]
            if item.get("id") == "revalidate-finalizer"
        )
        marker = 'if /usr/bin/python3 - "$cleanup_input_path"'
        begin = step["run"].index(marker)
        heredoc = step["run"].index("<<'PY'", begin) + len("<<'PY'")
        end = step["run"].index("\nPY", heredoc)
        script = step["run"][heredoc:end].lstrip("\n")
        binding = _validated_source_binding_handoff()
        payload = platform_workflow_input_guard.validate_external_payload(
            {
                "schema": 2,
                "confirmation": "RUN-PRODUCTION-EXTERNAL-LOAD",
                "target_sha": RUNNER_SHA,
                "control_email": "control@example.invalid",
                "setup_concurrency": "20",
                "run_id": "567890",
                "profile": "external-vote",
                "tournament_count": "1",
                "users_per_tournament": "20",
                "timeout_diagnostics": "false",
                "source_binding": binding,
            }
        )

        with tempfile.TemporaryDirectory(prefix="external-source-cleanup-handoff-") as directory:
            root = Path(directory)
            runner_temp = root / "runner-temp"
            runner_temp.mkdir(mode=0o700)
            control_path = runner_temp / "platform-production-control-email"
            control_path.write_text("control@example.invalid\n", encoding="ascii")
            control_path.chmod(0o600)
            input_path = root / "input.json"
            input_path.write_text(json.dumps(payload) + "\n", encoding="ascii")
            cleanup_path = root / "cleanup.json"

            def invoke(candidate: dict[str, object]) -> subprocess.CompletedProcess[str]:
                input_path.write_text(json.dumps(candidate) + "\n", encoding="ascii")
                return subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(cleanup_path),
                        str(input_path),
                        RUNNER_SHA,
                        APP_SHA,
                        "567890",
                    ],
                    env={**os.environ, "RUNNER_TEMP": str(runner_temp)},
                    capture_output=True,
                    text=True,
                    check=False,
                )

            completed = invoke(payload)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            cleanup = json.loads(cleanup_path.read_text(encoding="ascii"))
            self.assertEqual(
                set(cleanup),
                {"schema", "target_sha", "control_email", "load_run_id", "cleanup_run_id", "source_binding"},
            )
            self.assertEqual(cleanup["schema"], "2")
            self.assertEqual(cleanup["target_sha"], RUNNER_SHA)
            self.assertEqual(cleanup["source_binding"], binding)
            self.assertEqual(cleanup["source_binding"]["runner_sha"], RUNNER_SHA)
            self.assertEqual(cleanup["source_binding"]["app_target_sha"], APP_SHA)
            self.assertNotIn("app_target_sha", cleanup)
            self.assertNotIn("source_binding_sha256", cleanup)

            cleanup_path.unlink()
            forged = {**payload, "app_target_sha": APP_SHA}
            rejected = invoke(forged)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(cleanup_path.exists())

    def test_retained_cleanup_workflow_binds_artifact_before_ssh_handoff(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        resolver_job = workflow["jobs"]["resolve-host-tools-pin"]
        self.assertEqual(resolver_job["permissions"]["actions"], "read")
        self.assertEqual(
            resolver_job["outputs"]["source_binding_artifact_id"],
            "${{ steps.publish-source-binding.outputs.artifact-id }}",
        )
        self.assertNotIn("source_binding_artifact_digest", resolver_job["outputs"])
        resolver = next(
            step for step in resolver_job["steps"]
            if step.get("id") == "resolve-source-binding"
        )
        self.assertIn("resolve-workflow-source-binding", resolver["run"])
        self.assertIn('"$TARGET_SHA" "$GITHUB_REPOSITORY" "$GITHUB_API_URL"', resolver["run"])
        self.assertEqual(
            resolver["env"]["GH_TOKEN"], "${{ github.token }}"
        )
        publish = next(
            step for step in resolver_job["steps"]
            if step.get("id") == "publish-source-binding"
        )
        self.assertEqual(publish["with"]["retention-days"], 1)
        cleanup_job = workflow["jobs"]["cleanup"]
        self.assertEqual(cleanup_job["permissions"]["actions"], "read")
        self.assertNotIn("SOURCE_BINDING_ARTIFACT_DIGEST", cleanup_job.get("env", {}))
        download = cleanup_job["steps"][0]
        self.assertEqual(download["name"], "Download exact source-binding handoff")
        self.assertEqual(
            download["with"]["artifact-ids"],
            "${{ needs.resolve-host-tools-pin.outputs.source_binding_artifact_id }}",
        )
        validation = next(
            step for step in cleanup_job["steps"]
            if step.get("id") == "validate_cleanup_inputs"
        )
        self.assertNotIn("SOURCE_BINDING_ARTIFACT_DIGEST", validation.get("env", {}))
        self.assertNotIn("source_binding_artifact_digest", json.dumps(workflow))
        configure_ssh = next(
            index for index, step in enumerate(cleanup_job["steps"])
            if step.get("name") == "Configure production SSH"
        )
        validation_index = cleanup_job["steps"].index(validation)
        self.assertLess(validation_index, configure_ssh)

        marker = '/usr/bin/python3 - "$TARGET_SHA" "$LOAD_RUN_ID" "$GITHUB_RUN_ID" "$GITHUB_EVENT_PATH"'
        begin = validation["run"].index(marker)
        heredoc = validation["run"].index("<<'PY'", begin) + len("<<'PY'")
        end = validation["run"].index("\nPY", heredoc)
        script = validation["run"][heredoc:end].lstrip("\n")
        email = "control@example.invalid"
        binding = _validated_source_binding_handoff()

        with tempfile.TemporaryDirectory(prefix="retained-cleanup-source-binding-") as directory:
            root = Path(directory)
            runner_temp = root / "runner-temp"
            runner_temp.mkdir(mode=0o700)
            event_path = root / "event.json"
            event = {
                "inputs": {
                    "confirmation": "DELETE-PRODUCTION-RETAINED-LOAD",
                    "control_email": email,
                    "load_run_id": "567890",
                }
            }
            event_path.write_text(json.dumps(event) + "\n", encoding="ascii")
            event_path.chmod(0o600)
            binding_path = root / "source-binding.json"
            output_path = runner_temp / "platform-retained-cleanup-input.json"

            def invoke(
                source: dict[str, object], *, app_sha: str = APP_SHA, target_sha: str = RUNNER_SHA
            ) -> subprocess.CompletedProcess[str]:
                output_path.unlink(missing_ok=True)
                binding_path.write_text(json.dumps(source) + "\n", encoding="ascii")
                binding_path.chmod(0o600)
                return subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        target_sha,
                        "567890",
                        "678901",
                        str(event_path),
                        str(binding_path),
                        app_sha,
                    ],
                    env={**os.environ, "RUNNER_TEMP": str(runner_temp)},
                    capture_output=True,
                    text=True,
                    check=False,
                )

            source_handoff = {
                "schema": 1,
                "runner_sha": RUNNER_SHA,
                "app_target_sha": APP_SHA,
                "source_binding": binding,
            }
            completed = invoke(source_handoff)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn(email, completed.stderr)
            self.assertEqual(completed.stdout, f"::add-mask::{email}\n")
            payload = json.loads(output_path.read_text(encoding="ascii"))
            self.assertEqual(payload["schema"], 2)
            self.assertEqual(payload["target_sha"], RUNNER_SHA)
            self.assertEqual(payload["source_binding"], binding)
            self.assertNotIn("app_target_sha", payload)

            for changed in (
                {**source_handoff, "runner_sha": APP_SHA},
                {**source_handoff, "app_target_sha": "d" * 40},
                {
                    **source_handoff,
                    "source_binding": {**binding, "app_target_sha": "d" * 40},
                },
                {
                    **source_handoff,
                    "source_binding": {**binding, "unexpected": "extra"},
                },
            ):
                with self.subTest(changed=changed):
                    rejected = invoke(changed)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertFalse(output_path.exists())
                    self.assertNotIn(email, rejected.stderr)

            same_source = {
                "schema": 1,
                "runner_sha": RUNNER_SHA,
                "app_target_sha": RUNNER_SHA,
                "source_binding": None,
            }
            same_source_result = invoke(same_source, app_sha=RUNNER_SHA)
            self.assertEqual(same_source_result.returncode, 0, same_source_result.stderr)
            self.assertEqual(
                json.loads(output_path.read_text(encoding="ascii"))["schema"], 1
            )

    def test_cleanup_artifact_verifier_binds_api_metadata_attempt_zip_and_file(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        step = next(
            item for item in workflow["jobs"]["cleanup"]["steps"]
            if item.get("name") == "Verify exact source-binding artifact metadata and archive"
        )
        marker = '/usr/bin/python3 - "$RUNNER_TEMP/retained-cleanup-source-binding/source-binding.json"'
        begin = step["run"].index(marker)
        heredoc = step["run"].index("<<'PY'", begin) + len("<<'PY'")
        end = step["run"].index("\nPY", heredoc)
        script = step["run"][heredoc:end].lstrip("\n")
        run_id = "7654321"
        attempt = "2"
        artifact_id = "7654322"
        artifact_name = f"platform-retained-cleanup-source-binding-{run_id}-{attempt}"
        handoff = {
            "schema": 1,
            "runner_sha": RUNNER_SHA,
            "app_target_sha": APP_SHA,
            "source_binding": _validated_source_binding_handoff(),
        }
        handoff_bytes = _canonical_json(handoff) + b"\n"
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            info = zipfile.ZipInfo("source-binding.json")
            info.external_attr = (0o100600 & 0xFFFF) << 16
            archive.writestr(info, handoff_bytes)
        archive_bytes = archive_buffer.getvalue()
        digest = hashlib.sha256(archive_bytes).hexdigest()
        metadata = {
            "id": int(artifact_id),
            "name": artifact_name,
            "expired": False,
            "digest": f"sha256:{digest}",
            "workflow_run": {
                "id": int(run_id),
                "head_sha": RUNNER_SHA,
                "head_branch": "dev",
            },
        }
        attempt_record = {
            "id": int(run_id),
            "run_attempt": int(attempt),
            "head_sha": RUNNER_SHA,
            "head_branch": "dev",
        }

        class Response:
            status = 200

            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, limit: int = -1) -> bytes:
                return self.body if limit < 0 else self.body[:limit]

        def invoke(
            *,
            body: bytes = archive_bytes,
            metadata_value: dict[str, object] = metadata,
            attempt_value: dict[str, object] = attempt_record,
            downloaded_value: bytes = handoff_bytes,
        ) -> tuple[str, list[str], type]:
            calls: list[str] = []

            class Opener:
                def open(self, request: object, timeout: int) -> Response:
                    del timeout
                    url = request.full_url  # type: ignore[attr-defined]
                    calls.append(url)
                    if url.endswith(f"/actions/artifacts/{artifact_id}"):
                        return Response(json.dumps(metadata_value).encode("ascii"))
                    if url.endswith(f"/actions/runs/{run_id}/attempts/{attempt}"):
                        return Response(json.dumps(attempt_value).encode("ascii"))
                    if url.endswith(f"/actions/artifacts/{artifact_id}/zip"):
                        return Response(body)
                    raise AssertionError(f"unexpected cleanup artifact API route: {url}")

            with tempfile.TemporaryDirectory(prefix="cleanup-artifact-verify-") as directory:
                root = Path(directory)
                binding_dir = root / "retained-cleanup-source-binding"
                binding_dir.mkdir(mode=0o700)
                binding_path = binding_dir / "source-binding.json"
                binding_path.write_bytes(downloaded_value)
                binding_path.chmod(0o600)
                old_argv = sys.argv
                sys.argv = ["source-binding-artifact-check", str(binding_path)]
                environment = {
                    "GH_TOKEN": "fake-actions-token",
                    "GITHUB_API_URL": "https://api.github.com",
                    "GITHUB_REPOSITORY": "StrayForest/old_sparky",
                    "SOURCE_BINDING_ARTIFACT_ID": artifact_id,
                    "TARGET_SHA": RUNNER_SHA,
                    "GITHUB_RUN_ID": run_id,
                    "GITHUB_RUN_ATTEMPT": attempt,
                }
                output = io.StringIO()
                try:
                    with patch.dict(os.environ, environment, clear=False), \
                        patch("urllib.request.build_opener", return_value=Opener()), \
                        redirect_stdout(output):
                        namespace = {"__name__": "__main__"}
                        exec(compile(script, str(workflow_path), "exec"), namespace)
                finally:
                    sys.argv = old_argv
                return output.getvalue(), calls, namespace["SafeRedirect"]

        output, calls, safe_redirect_type = invoke()
        self.assertEqual(output, "SOURCE_BINDING_ARTIFACT status=verified\n")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("fake-actions-token" not in url for url in calls))
        # The secret consumer has no action-provided digest output. The API
        # metadata digest and downloaded ZIP bytes are the authenticated pair.
        import urllib.request

        redirect = safe_redirect_type()
        original = urllib.request.Request(
            "https://api.github.com/repos/StrayForest/old_sparky",
            headers={"Authorization": "Bearer test-token", "Cookie": "private=session"},
        )
        cross_host = redirect.redirect_request(
            original, None, 302, "Found", {}, "https://blob.example.invalid/archive.zip"
        )
        self.assertIsNotNone(cross_host)
        self.assertIsNone(cross_host.get_header("Authorization"))
        self.assertIsNone(cross_host.get_header("Cookie"))
        same_host = redirect.redirect_request(
            original, None, 302, "Found", {}, "https://api.github.com/archive.zip"
        )
        self.assertIsNotNone(same_host)
        self.assertEqual(same_host.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(same_host.get_header("Cookie"), "private=session")
        self.assertIsNone(
            redirect.redirect_request(
                original, None, 302, "Found", {}, "http://api.github.com/archive.zip"
            )
        )
        self.assertIsNone(
            redirect.redirect_request(
                original, None, 302, "Found", {}, "https://user:pass@blob.example.invalid/archive.zip"
            )
        )
        with self.assertRaises(SystemExit):
            invoke(metadata_value={**metadata, "digest": "sha256:" + "0" * 64})
        with self.assertRaises(SystemExit):
            invoke(body=b"not a zip")
        with self.assertRaises(SystemExit):
            invoke(metadata_value={**metadata, "digest": None})
        with self.assertRaises(SystemExit):
            invoke(downloaded_value=b"different source binding\n")
        with self.assertRaises(SystemExit):
            invoke(attempt_value={**attempt_record, "run_attempt": True})

    def test_retained_cleanup_workflow_rejects_source_binding_before_ssh(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        validation = next(
            step for step in workflow["jobs"]["cleanup"]["steps"]
            if step.get("id") == "validate_cleanup_inputs"
        )
        marker = '/usr/bin/python3 - "$TARGET_SHA" "$LOAD_RUN_ID" "$GITHUB_RUN_ID" "$GITHUB_EVENT_PATH"'
        begin = validation["run"].index(marker)
        heredoc = validation["run"].index("<<'PY'", begin) + len("<<'PY'")
        end = validation["run"].index("\nPY", heredoc)
        script = validation["run"][heredoc:end].lstrip("\n")
        binding = _validated_source_binding_handoff()
        email = "control@example.invalid"

        with tempfile.TemporaryDirectory(prefix="retained-cleanup-source-negative-") as directory:
            root = Path(directory)
            runner_temp = root / "runner-temp"
            runner_temp.mkdir(mode=0o700)
            event = root / "event.json"
            event.write_text(
                json.dumps({"inputs": {"confirmation": "DELETE-PRODUCTION-RETAINED-LOAD", "control_email": email, "load_run_id": "567890"}}),
                encoding="ascii",
            )
            event.chmod(0o600)
            binding_path = root / "source-binding.json"
            output = runner_temp / "platform-retained-cleanup-input.json"
            bad_handoffs = (
                {"schema": 1, "runner_sha": RUNNER_SHA, "app_target_sha": APP_SHA, "source_binding": {**binding, "receipt_artifact_digest": "sha256:" + "0" * 64}},
                {"schema": 1, "runner_sha": RUNNER_SHA, "app_target_sha": APP_SHA, "source_binding": {**binding, "baseline_identity": {**binding["baseline_identity"], "release_ino": True}}},
                {"schema": 1, "runner_sha": RUNNER_SHA, "app_target_sha": APP_SHA, "source_binding": binding, "unexpected": True},
                {"schema": 1, "runner_sha": RUNNER_SHA, "app_target_sha": APP_SHA, "source_binding": None},
            )
            for candidate in bad_handoffs:
                with self.subTest(candidate=candidate):
                    output.unlink(missing_ok=True)
                    binding_path.write_text(json.dumps(candidate) + "\n", encoding="ascii")
                    binding_path.chmod(0o600)
                    result = subprocess.run(
                        [sys.executable, "-c", script, RUNNER_SHA, "567890", "678901", str(event), str(binding_path), APP_SHA],
                        env={**os.environ, "RUNNER_TEMP": str(runner_temp)},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(output.exists())
                    self.assertNotIn(email, result.stderr)

    def test_external_fixture_manifest_validation_binds_both_source_shas(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/platform-production-external-load.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        step = next(
            item
            for item in workflow["jobs"]["fixture-setup"]["steps"]
            if item.get("name") == "Validate closed fixture manifest"
        )
        script = step["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        binding = _validated_source_binding_handoff()
        binding_sha = hashlib.sha256(_canonical_json(binding)).hexdigest()
        handoff = platform_workflow_input_guard.validate_external_payload(
            {
                "schema": 2,
                "confirmation": "RUN-PRODUCTION-EXTERNAL-LOAD",
                "target_sha": RUNNER_SHA,
                "control_email": "control@example.invalid",
                "setup_concurrency": "20",
                "run_id": "567890",
                "profile": "external-vote",
                "tournament_count": "1",
                "users_per_tournament": "20",
                "timeout_diagnostics": "false",
                "source_binding": binding,
            }
        )
        manifest: dict[str, object] = {
            "schema": 2,
            "purpose": "external_ready_vote",
            "origin": "https://old-sparky.com",
            "session_cookie_name": "deadlock_platform_session",
            "csrf_cookie_name": "deadlock_platform_session_csrf",
            "marker": "preprod26082900000000ab",
            "created_at": "2026-10-08T00:00:00Z",
            "tournaments": [{"id": "tour-1", "slug": "tour-1", "user_count": 20}],
            "users": [
                {
                    "user_id": "user-1",
                    "tournament_slug": "tour-1",
                    "session_token": "s" * 64,
                    "csrf_token": "c" * 64,
                }
            ],
            "runner_sha": RUNNER_SHA,
            "app_target_sha": APP_SHA,
            "source_binding": binding,
            "source_binding_sha256": binding_sha,
        }
        with tempfile.TemporaryDirectory(prefix="external-source-manifest-") as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            handoff_path = root / "input.json"
            handoff_path.write_text(json.dumps(handoff), encoding="ascii")

            def invoke(candidate: dict[str, object]) -> subprocess.CompletedProcess[str]:
                manifest_path.write_text(json.dumps(candidate), encoding="ascii")
                return subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(manifest_path),
                        str(handoff_path),
                        RUNNER_SHA,
                        APP_SHA,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )

            accepted = invoke(manifest)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            for field, value in (
                ("schema", 1),
                ("runner_sha", APP_SHA),
                ("app_target_sha", RUNNER_SHA),
                ("source_binding_sha256", "0" * 64),
            ):
                with self.subTest(field=field):
                    mutated = {**manifest, field: value}
                    rejected = invoke(mutated)
                    self.assertNotEqual(rejected.returncode, 0)

    def test_external_load_status_writer_binds_runner_app_and_report(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/platform-production-external-load.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        step = next(
            item
            for item in workflow["jobs"]["load-client"]["steps"]
            if item.get("id") == "external-evaluate"
        )
        marker = '/usr/bin/python3 - "$artifact_dir/load-status.json"'
        begin = step["run"].index(marker)
        heredoc = step["run"].index("<<'PY'", begin) + len("<<'PY'")
        end = step["run"].index("\nPY", heredoc)
        script = step["run"][heredoc:end].lstrip("\n")
        evaluator_step = next(
            item
            for item in workflow["jobs"]["evaluate-load"]["steps"]
            if item.get("id") == "evaluate-load"
        )
        evaluator_marker = 'if /usr/bin/python3 - "$load_status_file"'
        evaluator_begin = evaluator_step["run"].index(evaluator_marker)
        evaluator_heredoc = evaluator_step["run"].index("<<'PY'", evaluator_begin) + len("<<'PY'")
        evaluator_end = evaluator_step["run"].index("\nPY", evaluator_heredoc)
        evaluator_script = evaluator_step["run"][evaluator_heredoc:evaluator_end].lstrip("\n")
        binding = _validated_source_binding_handoff()
        binding_sha = hashlib.sha256(_canonical_json(binding)).hexdigest()
        handoff = {
            "schema": 2,
            "confirmation": "RUN-PRODUCTION-EXTERNAL-LOAD",
            "target_sha": RUNNER_SHA,
            "control_email": "control@example.invalid",
            "setup_concurrency": "20",
            "run_id": "567890",
            "profile": "external-vote",
            "tournament_count": "1",
            "users_per_tournament": "20",
            "timeout_diagnostics": "false",
            "source_binding": binding,
        }
        report = {
            "schema": 5,
            "source_git_sha": RUNNER_SHA,
            "app_target_sha": APP_SHA,
            "source_binding": binding,
            "source_binding_sha256": binding_sha,
            "acceptance": {"passed": False, "contract_ok": False},
        }
        with tempfile.TemporaryDirectory(prefix="external-load-status-binding-") as directory:
            root = Path(directory)
            handoff_path = root / "input.json"
            report_path = root / "report.json"
            receipt_path = root / "load-status.json"
            handoff_path.write_text(json.dumps(handoff), encoding="ascii")

            def invoke(candidate: dict[str, object]) -> subprocess.CompletedProcess[str]:
                report_path.write_text(json.dumps(candidate), encoding="utf-8")
                return subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(receipt_path),
                        "0",
                        "3",
                        "pending_origin",
                        "1",
                        RUNNER_SHA,
                        APP_SHA,
                        binding_sha,
                        "567890",
                        "2",
                        "ready-vote-slo-v2",
                        str(report_path),
                        str(handoff_path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )

            completed = invoke(report)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["target_sha"], RUNNER_SHA)
            self.assertEqual(receipt["runner_sha"], RUNNER_SHA)
            self.assertEqual(receipt["app_target_sha"], APP_SHA)
            self.assertEqual(receipt["source_binding"], binding)
            self.assertEqual(receipt["source_binding_sha256"], binding_sha)
            self.assertFalse(receipt["report_ready"] is False)

            def verify_evaluator_binding() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        evaluator_script,
                        str(receipt_path),
                        str(report_path),
                        str(handoff_path),
                        RUNNER_SHA,
                        APP_SHA,
                        "567890",
                        "2",
                        "ready-vote-slo-v2",
                        "false",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )

            evaluator_valid = verify_evaluator_binding()
            self.assertEqual(evaluator_valid.returncode, 0, evaluator_valid.stderr)

            forged_report = {**report, "app_target_sha": RUNNER_SHA}
            still_digest_bound = invoke(forged_report)
            self.assertEqual(still_digest_bound.returncode, 0, still_digest_bound.stderr)
            rejected = verify_evaluator_binding()
            self.assertNotEqual(rejected.returncode, 0)

    def test_full_dual_source_report_origin_sanitizer_pipeline_stays_red(self) -> None:
        pipeline_path = PLATFORM_ROOT / "tests" / "test_external_load_pending_origin_pipeline.py"
        spec = importlib.util.spec_from_file_location(
            "external_load_pending_origin_pipeline_for_noop_binding", pipeline_path
        )
        if spec is None or spec.loader is None:
            self.fail("could not load the existing hermetic external-load pipeline fixture")
        pipeline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = pipeline
        spec.loader.exec_module(pipeline)

        binding = _validated_source_binding_handoff()
        result = pipeline.ExternalLoadPendingOriginPipelineTests()._exercise_deferred_pipeline(
            "ready-vote-slo-v2", source_binding=binding
        )
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        candidate = result["candidate"]
        receipt = result["receipt"]
        evaluated = result["evaluated"]
        sanitized = result["sanitized"]
        canonical_digest = hashlib.sha256(_canonical_json(binding)).hexdigest()
        for report in (candidate, evaluated, sanitized):
            self.assertEqual(report["source_git_sha"], RUNNER_SHA)
            self.assertEqual(report["app_target_sha"], APP_SHA)
            self.assertEqual(report["source_binding"], binding)
            self.assertEqual(report["source_binding_sha256"], canonical_digest)
        self.assertEqual(receipt["runner_sha"], RUNNER_SHA)
        self.assertEqual(receipt["app_target_sha"], APP_SHA)
        self.assertEqual(receipt["source_binding"], binding)
        self.assertEqual(receipt["source_binding_sha256"], canonical_digest)
        self.assertFalse(candidate["acceptance"]["passed"])
        self.assertFalse(evaluated["acceptance"]["passed"])
        self.assertFalse(sanitized["acceptance"]["passed"])
        self.assertEqual(evaluated["acceptance"]["decision"], "SLO FAIL")
        self.assertEqual(result["final_gate_exit"], 1)

        expected_env = {
            "SOURCE_GIT_SHA": RUNNER_SHA,
            "APP_TARGET_SHA": APP_SHA,
            "SOURCE_BINDING_BASE64": base64.b64encode(_canonical_json(binding)).decode("ascii"),
            "SOURCE_BINDING_SHA256": canonical_digest,
            "GITHUB_RUN_ID": pipeline.RUN_ID,
            "GITHUB_RUN_ATTEMPT": "1",
        }
        profile, _manifest, _users = pipeline._small_profile(
            pipeline._hermetic_profile("ready-vote-slo-v2")
        )
        with tempfile.TemporaryDirectory(prefix="external-load-binding-mutations-") as directory:
            root = Path(directory)
            observer_path = root / "observer.json"
            observer_path.write_text(
                json.dumps(
                    pipeline._observer(
                        marker=str(candidate["fixture_marker"]), run_id=pipeline.RUN_ID
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            for label, mutation in (
                ("missing", lambda report: report.pop("source_binding")),
                (
                    "forged",
                    lambda report: report["source_binding"].update(
                        {"receipt_artifact_id": "999999"}
                    ),
                ),
            ):
                with self.subTest(binding=label):
                    forged = copy.deepcopy(evaluated)
                    mutation(forged)
                    forged_path = root / f"{label}.json"
                    forged_path.write_text(json.dumps(forged), encoding="utf-8")
                    with (
                        patch.object(
                            platform_load,
                            "get_profile",
                            return_value=profile,
                        ),
                        patch.dict(os.environ, expected_env, clear=False),
                    ):
                        binding_report = platform_load._report_binding(profile, forged)
                        self.assertFalse(binding_report["checks"]["source_binding"])
                        with redirect_stdout(io.StringIO()):
                            exit_code = platform_load.main(
                                [
                                    "evaluate",
                                    "--profile",
                                    "ready-vote-slo-v2",
                                    "--report",
                                    str(forged_path),
                                    "--server-observability",
                                    str(observer_path),
                                    "--defer-completed-slo-failure",
                                ]
                            )
                    self.assertEqual(exit_code, 1)

    def test_receipt_rejects_unknown_fields_wrong_attempts_and_source(self) -> None:
        base = _receipt_document()
        mutations = (
            {**base, "extra": "ignored fields are not permitted"},
            {**base, "schema": True},
            {**base, "runner_sha": APP_SHA},
            {
                **base,
                "production_deploy": {"run_id": DEPLOY_RUN_ID, "run_attempt": "1"},
            },
            {
                **base,
                "source_security": {"run_id": SECURITY_RUN_ID, "run_attempt": "0"},
            },
            {
                **base,
                "autodeploy": {"run_id": "999999", "run_attempt": AUTODEPLOY_ATTEMPT},
            },
            {
                **base,
                "baseline_identity": {
                    **_baseline_identity(),
                    "release_ino": True,
                },
            },
            {
                **base,
                "baseline_identity": {
                    **_baseline_identity(),
                    "pending_operation": True,
                },
            },
            {
                **base,
                "baseline_identity": {
                    **_baseline_identity(),
                    "unrecognized": "closed schemas reject extensions",
                },
            },
            {
                **base,
                "baseline_identity": {
                    **_baseline_identity(),
                    "source_sha": RUNNER_SHA,
                },
            },
        )
        for index, document in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ProvenanceError):
                validate_noop_receipt_document(document, **_expected_run_bindings())

    def test_receipt_json_parser_rejects_duplicate_keys_and_noncanonical_bytes(self) -> None:
        for raw in (
            b'{"schema":1,"schema":1}\n',
            b'{ "schema": 1 }\n',
            b'{"schema":NaN}\n',
        ):
            with self.subTest(raw=raw), self.assertRaises(ProvenanceError):
                parse_receipt_json(raw)

    def test_receipt_rejects_deployable_runtime_and_mixed_classifier_routes(self) -> None:
        base = _receipt_document()
        mutations: list[dict[str, object]] = []
        for field, value in (
            ("no_op", False),
            ("runtime_required", True),
        ):
            candidate = copy.deepcopy(base)
            candidate["route"][field] = value  # type: ignore[index]
            mutations.append(candidate)

        for field, value in (
            ("deployable", True),
            ("runtime_sensitive", True),
            ("fallback", True),
            ("class", "docs-only"),
            ("expected_gates", ["backend"]),
        ):
            candidate = copy.deepcopy(base)
            candidate["route"]["cumulative_manifest"][field] = value  # type: ignore[index]
            candidate["route"]["cumulative_manifest_sha256"] = hashlib.sha256(
                _canonical_json(candidate["route"]["cumulative_manifest"])
            ).hexdigest()  # type: ignore[index]
            mutations.append(candidate)

        for index, document in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ProvenanceError):
                validate_noop_receipt_document(document, **_expected_run_bindings())

    def test_artifact_validation_binds_archive_metadata_and_single_member(self) -> None:
        document = _receipt_document()
        archive = _zip_receipt(document)
        metadata = _artifact_metadata(archive)
        binding = validate_noop_receipt_artifact(
            metadata,
            archive,
            **_expected_run_bindings(),
            expected_artifact_id=ARTIFACT_ID,
            expected_artifact_name=ARTIFACT_NAME,
        )
        self.assertEqual(binding["app_target_sha"], APP_SHA)
        self.assertEqual(binding["receipt_artifact_id"], ARTIFACT_ID)
        self.assertEqual(binding["receipt_archive_sha256"], hashlib.sha256(archive).hexdigest())

        bad_metadata_cases: list[tuple[str, dict[str, object], str, str]] = []
        for field, value in (
            ("id", int(ARTIFACT_ID) + 1),
            ("name", "platform-production-noop-source-receipt-999999-1"),
            ("expired", True),
            ("size_in_bytes", len(archive) + 1),
            ("digest", "sha256:" + "0" * 64),
        ):
            candidate = copy.deepcopy(metadata)
            candidate[field] = value
            bad_metadata_cases.append((field, candidate, ARTIFACT_ID, ARTIFACT_NAME))
        for field, value in (
            ("id", int(DEPLOY_RUN_ID) + 1),
            ("run_attempt", int(DEPLOY_ATTEMPT) + 1),
            ("head_sha", APP_SHA),
            ("head_branch", "main"),
        ):
            candidate = copy.deepcopy(metadata)
            candidate["workflow_run"][field] = value  # type: ignore[index]
            bad_metadata_cases.append((f"workflow_run.{field}", candidate, ARTIFACT_ID, ARTIFACT_NAME))
        bad_metadata_cases.extend(
            (
                ("expected artifact id", metadata, "999999", ARTIFACT_NAME),
                ("expected artifact name", metadata, ARTIFACT_ID, "wrong-name"),
            )
        )
        for label, bad_metadata, artifact_id, artifact_name in bad_metadata_cases:
            with self.subTest(metadata=label), self.assertRaises(ProvenanceError):
                validate_noop_receipt_artifact(
                    bad_metadata,
                    archive,
                    **_expected_run_bindings(),
                    expected_artifact_id=artifact_id,
                    expected_artifact_name=artifact_name,
                )

        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as extra_member:
            extra_member.writestr(RECEIPT_FILE, _canonical_json(document) + b"\n")
            extra_member.writestr("untrusted.json", b"{}\n")
        duplicate_archive = stream.getvalue()
        duplicate_metadata = _artifact_metadata(duplicate_archive)
        with self.assertRaises(ProvenanceError):
            validate_noop_receipt_artifact(
                duplicate_metadata,
                duplicate_archive,
                **_expected_run_bindings(),
                expected_artifact_id=ARTIFACT_ID,
                expected_artifact_name=ARTIFACT_NAME,
            )

        truncated_archive = archive[:-1]
        truncated_metadata = _artifact_metadata(truncated_archive)
        with self.assertRaises(ProvenanceError):
            validate_noop_receipt_artifact(
                truncated_metadata,
                truncated_archive,
                **_expected_run_bindings(),
                expected_artifact_id=ARTIFACT_ID,
                expected_artifact_name=ARTIFACT_NAME,
            )

        wrong_member_archive = io.BytesIO()
        with zipfile.ZipFile(wrong_member_archive, "w") as wrong_member:
            wrong_member.writestr("unexpected.json", _canonical_json(document) + b"\n")
        wrong_member_bytes = wrong_member_archive.getvalue()
        with self.assertRaises(ProvenanceError):
            validate_noop_receipt_artifact(
                _artifact_metadata(wrong_member_bytes),
                wrong_member_bytes,
                **_expected_run_bindings(),
                expected_artifact_id=ARTIFACT_ID,
                expected_artifact_name=ARTIFACT_NAME,
            )

    def test_artifact_reader_caps_actual_decompressed_bytes_not_declared_size(self) -> None:
        document = _receipt_document()
        archive_bytes = _zip_receipt(document)
        metadata = _artifact_metadata(archive_bytes)
        observed_reads: list[int] = []
        emitted = 0
        actual_payload_size = 1024 * 1024 + 1

        class DeclaredSmallMember:
            filename = RECEIPT_FILE
            file_size = 1
            external_attr = (0o100600 & 0xFFFF) << 16

            @staticmethod
            def is_dir() -> bool:
                return False

        class OversizedStream:
            def __enter__(self) -> "OversizedStream":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                nonlocal emitted
                if size < 0:
                    size = actual_payload_size
                observed_reads.append(size)
                count = min(size, actual_payload_size - emitted)
                emitted += count
                return b"x" * count

        class AdversarialZip:
            def __init__(self, _source: object) -> None:
                pass

            def __enter__(self) -> "AdversarialZip":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            @staticmethod
            def infolist() -> list[DeclaredSmallMember]:
                return [DeclaredSmallMember()]

            @staticmethod
            def open(
                _entry: DeclaredSmallMember, _mode: str = "r"
            ) -> OversizedStream:
                return OversizedStream()

            @staticmethod
            def read(_entry: DeclaredSmallMember) -> bytes:
                raise AssertionError("unbounded ZipFile.read must not be used")

        with patch("tools.platform_noop_source_binding.zipfile.ZipFile", AdversarialZip):
            with self.assertRaises(ProvenanceError):
                validate_noop_receipt_artifact(
                    metadata,
                    archive_bytes,
                    **_expected_run_bindings(),
                    expected_artifact_id=ARTIFACT_ID,
                    expected_artifact_name=ARTIFACT_NAME,
                )
        self.assertGreater(emitted, 0)
        self.assertLessEqual(emitted, 1024 * 1024 + 1)
        self.assertTrue(observed_reads)
        self.assertTrue(all(0 < size <= 1024 * 1024 + 1 for size in observed_reads))

    def test_all_workflows_preserve_runner_and_app_sha_roles(self) -> None:
        external = (
            REPO_ROOT / ".github/workflows/platform-production-external-load.yml"
        ).read_text(encoding="utf-8")
        cleanup = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup.yml"
        ).read_text(encoding="utf-8")
        live_launch = (
            REPO_ROOT / ".github/workflows/platform-live-launch.yml"
        ).read_text(encoding="utf-8")
        live_user = (
            REPO_ROOT / ".github/workflows/platform-live-user-qa.yml"
        ).read_text(encoding="utf-8")

        for name, source in (
            ("external-load", external),
            ("retained-cleanup", cleanup),
            ("live-launch", live_launch),
            ("live-user", live_user),
        ):
            with self.subTest(workflow=name):
                self.assertIn("${{ github.sha }}", source, f"{name} lost runner SHA source")
                self.assertIn("APP_TARGET_SHA", source, f"{name} omits app target binding")
                self.assertIn("app_target_sha", source, f"{name} omits app target provenance")
                self.assertIn("runner_sha", source, f"{name} omits runner provenance")

        self.assertIn('SOURCE_GIT_SHA="$TARGET_SHA"', external)
        self.assertIn('source_git_sha', external)
        self.assertIn('target_sha', external)
        self.assertIn("app_target_sha", cleanup)
        self.assertIn("app_target_sha", live_launch)
        self.assertIn("app_target_sha", live_user)
        self.assertIn("create-live-handoff", live_user)
        self.assertIn('"false"', live_user)
        self.assertIn('""', live_user)


if __name__ == "__main__":
    unittest.main()
