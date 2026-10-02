from __future__ import annotations

import json
from collections.abc import Mapping
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import warnings
import zipfile

from tools.platform_ci_classifier import (
    CANDIDATE_PACKAGING_FILES,
    CANDIDATE_PACKAGING_REASON,
    DOCS_ONLY_GATE_IDS,
    FULL_GATE_IDS,
    OUT_OF_SCOPE_GATE_IDS,
    RECOVERY_BOOTSTRAP_REASON,
    RUNTIME_SENSITIVE_FILES,
    ClassifierError,
    SECURITY_WORKFLOW_NAME,
    SECURITY_WORKFLOW_PATH,
    classify,
    manifest_digest,
    validate_security_workflow_run,
    validate_manifest,
)
from tools.platform_ci_classifier import _write_github_output
from tools.platform_safe_zip import UnsafeZipError, extract_single_manifest
from tools.platform_workflow_provenance import ProvenanceError, validate_security_marker
from tools.platform_security_status import (
    FAIL_DESCRIPTION,
    REPOSITORY,
    ReconcilerError,
    complete_workflow_run_keys,
    evaluate_status,
    has_newer_workflow_run,
    publish_workflow_event,
    reconcile_workflow_event,
    status_reconciliation_needed,
    terminal_failure_required,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SECURITY_WORKFLOW = REPO_ROOT / ".github/workflows/platform-security.yml"
AUTO_DEPLOY_WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-autodeploy.yml"
PRODUCTION_WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
STATUS_FINALIZER_WORKFLOW = REPO_ROOT / ".github/workflows/platform-security-status-finalizer.yml"

# PR117 was merged as a real merge commit.  The push range is the first
# parent (the branch before the merge) to that merge commit; the PR range is
# the same base to the source head.  In particular, the PR fixture must not
# use the second parent -> merge range, which is empty for a clean GitHub
# merge and would hide the changed-file classifier regression.
PR117_BASE_SHA = "adb9b56384db7f444596e752088990a39555de1b"
PR117_HEAD_SHA = "7a20f9718c9debcd20d7dc0ac338218576b318a4"
PR117_MERGE_SHA = "d6c4c0922289234735c62728991dffc0db410726"
PR117_CHANGED_FILES = frozenset(
    {
        ".github/workflows/platform-host-tools-candidate.yml",
        ".github/workflows/platform-production-autodeploy.yml",
        ".github/workflows/platform-security.yml",
        "platform/docs/CURRENT.md",
        "platform/docs/adr/production-host-tools-provisioning.md",
        "platform/docs/deployment-runbook.md",
        "platform/docs/development-guide.md",
        "platform/docs/test-suite-governance.md",
        "platform/tests/test_platform_ci_classifier.py",
        "platform/tests/test_platform_host_tools_bundle.py",
        "platform/tools/platform_ci_classifier.py",
        "platform/tools/platform_host_tools_candidate.py",
        "platform/tools/platform_test_catalog.py",
        "platform/tools/platform_verify_contract.py",
    }
)


class PlatformCiClassifierTests(unittest.TestCase):
    TARGET_SHA = "a" * 40

    def _run_classifier_fixture(
        self,
        *,
        event: str,
        payload: dict[str, object],
        target_sha: str,
        branch: str,
    ) -> dict[str, object]:
        """Generate one artifact through the production classifier CLI."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / "event.json"
            output_path = root / "classifier-manifest.json"
            event_path.write_text(json.dumps(payload), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "platform/tools/platform_ci_classifier.py"),
                    "--event",
                    event,
                    "--target-sha",
                    target_sha,
                    "--branch",
                    branch,
                    "--event-file",
                    str(event_path),
                    "--repo-root",
                    str(REPO_ROOT),
                    "--output",
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(output_path.read_text(encoding="utf-8"))

    def _run_auto_deploy_manifest_contract(
        self, manifest: dict[str, object]
    ) -> dict[str, str]:
        """Pass a generated manifest through auto-deploy's no-op boundary."""

        workflow = AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        marker = (
            '          /usr/bin/python3 - "$manifest_path" "$TARGET_SHA" '
            '"$GITHUB_OUTPUT" <<\'PY\'\n'
        )
        start = workflow.index(marker) + len(marker)
        end = workflow.index("          PY\n", start)
        contract = textwrap.dedent(workflow[start:end])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "classifier-manifest.json"
            output_path = root / "github-output"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-",
                    str(manifest_path),
                    str(manifest["target_sha"]),
                    str(output_path),
                ],
                input=contract,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output: dict[str, str] = {}
            for line in output_path.read_text(encoding="utf-8").splitlines():
                key, value = line.split("=", 1)
                output[key] = value
            return output

    def test_docs_only_route_is_nondeployable_and_digest_bound(self) -> None:
        manifest = classify(
            ["platform/docs/deployment-runbook.md", "platform/docs/test-suite-governance.md"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["class"], "docs-only")
        self.assertEqual(manifest["expected_gates"], list(DOCS_ONLY_GATE_IDS))
        self.assertFalse(manifest["deployable"])
        self.assertFalse(manifest["fallback"])
        validate_manifest(manifest, expected_target_sha=self.TARGET_SHA)
        self.assertEqual(manifest["digest"], manifest_digest(manifest))

    def test_full_dev_push_is_the_only_deployable_route(self) -> None:
        manifest = classify(
            ["platform/apps/platform_api/app/main.py"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(manifest["class"], "full")
        self.assertEqual(manifest["expected_gates"], list(FULL_GATE_IDS))
        self.assertTrue(manifest["deployable"])
        self.assertFalse(manifest["fallback"])
        validate_manifest(
            manifest,
            expected_target_sha=self.TARGET_SHA,
            require_deployable=True,
        )

    def test_candidate_packaging_is_full_ci_but_non_deployable_for_pr_and_push(self) -> None:
        candidate_files = {
            ".github/workflows/platform-host-tools-candidate.yml",
            ".github/workflows/platform-production-autodeploy.yml",
            ".github/workflows/platform-security.yml",
            "platform/tests/test_platform_ci_classifier.py",
            "platform/tools/platform_host_tools_candidate.py",
            "platform/tools/platform_ci_classifier.py",
            "platform/tests/test_platform_host_tools_bundle.py",
            "platform/tools/platform_test_catalog.py",
            "platform/tools/platform_verify_contract.py",
        }
        self.assertEqual(CANDIDATE_PACKAGING_FILES, candidate_files)
        files = sorted(
            candidate_files
            | {
                "platform/docs/CURRENT.md",
                "platform/docs/README.md",
                "platform/docs/adr/production-host-tools-provisioning.md",
                "platform/docs/deployment-runbook.md",
                "platform/docs/test-suite-governance.md",
            }
        )
        manifests = {
            event: classify(
                files,
                event=event,
                target_sha=self.TARGET_SHA,
                branch="dev" if event == "push" else "feature/candidate",
            )
            for event in ("pull_request", "push")
        }

        for event, manifest in manifests.items():
            with self.subTest(event=event):
                self.assertEqual(manifest["class"], "full")
                self.assertEqual(manifest["expected_gates"], list(FULL_GATE_IDS))
                self.assertFalse(manifest["fallback"])
                self.assertFalse(manifest["deployable"])
                self.assertFalse(manifest["runtime_sensitive"])
                self.assertEqual(manifest["reason"], CANDIDATE_PACKAGING_REASON)
                validate_manifest(manifest, expected_target_sha=self.TARGET_SHA)

        tampered = dict(manifests["push"])
        tampered["deployable"] = True
        tampered["digest"] = manifest_digest(tampered)
        with self.assertRaises(ClassifierError):
            validate_manifest(tampered)

    def test_pr117_first_parent_push_and_pr_ranges_generate_non_deployable_manifest(self) -> None:
        self.assertNotEqual(PR117_HEAD_SHA, PR117_MERGE_SHA)
        merge_parents = subprocess.check_output(
            ["git", "rev-list", "--parents", "-n", "1", PR117_MERGE_SHA],
            cwd=REPO_ROOT,
            text=True,
        ).split()
        self.assertEqual(merge_parents[1:], [PR117_BASE_SHA, PR117_HEAD_SHA])
        self.assertEqual(
            set(
                subprocess.check_output(
                    ["git", "diff", "--name-only", PR117_BASE_SHA, PR117_HEAD_SHA, "--"],
                    cwd=REPO_ROOT,
                    text=True,
                ).splitlines()
            ),
            PR117_CHANGED_FILES,
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "diff", "--name-only", PR117_HEAD_SHA, PR117_MERGE_SHA, "--"],
                cwd=REPO_ROOT,
                text=True,
            ),
            "",
        )

        push_manifest = self._run_classifier_fixture(
            event="push",
            payload={"before": PR117_BASE_SHA, "after": PR117_MERGE_SHA},
            target_sha=PR117_MERGE_SHA,
            branch="dev",
        )
        pr_manifest = self._run_classifier_fixture(
            event="pull_request",
            payload={
                "pull_request": {
                    "base": {"sha": PR117_BASE_SHA},
                    "head": {"sha": PR117_HEAD_SHA},
                }
            },
            target_sha=PR117_MERGE_SHA,
            branch="classifier-push-parity",
        )

        for label, manifest in (("push", push_manifest), ("pull request", pr_manifest)):
            with self.subTest(event=label):
                self.assertEqual(set(manifest["files"]), PR117_CHANGED_FILES)
                self.assertEqual(manifest["class"], "full")
                self.assertFalse(manifest["fallback"])
                self.assertFalse(manifest["runtime_sensitive"])
                self.assertFalse(manifest["deployable"])
                self.assertEqual(manifest["reason"], CANDIDATE_PACKAGING_REASON)
                validate_manifest(manifest, expected_target_sha=PR117_MERGE_SHA)

        # Auto-deploy consumes the generated artifact's authority bit.  This
        # exercises the same inline manifest contract that emits route_*
        # outputs for the no-op branch; the test never substitutes a literal
        # deployable=false input.
        route = self._run_auto_deploy_manifest_contract(push_manifest)
        self.assertEqual(route["route_class"], push_manifest["class"])
        self.assertEqual(route["route_deployable"], str(push_manifest["deployable"]).lower())
        self.assertEqual(route["route_fallback"], str(push_manifest["fallback"]).lower())
        self.assertEqual(route["route_digest"], push_manifest["digest"])
        self.assertIn(
            'if [[ "$ROUTE_DEPLOYABLE" != "true" ]]',
            AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8"),
        )
        self.assertIn(
            'echo "deploy=false" >> "$GITHUB_OUTPUT"',
            AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8"),
        )

    def test_deployable_push_matrix_keeps_application_runtime_migration_and_release_paths(self) -> None:
        paths = (
            "platform/apps/platform_api/app/main.py",
            "platform/tools/platform_build_release.sh",
            "platform/alembic/versions/20260927_candidate.py",
        )
        for path in paths:
            with self.subTest(path=path):
                pull_request = classify(
                    [path],
                    event="pull_request",
                    target_sha=self.TARGET_SHA,
                    branch="feature/candidate",
                )
                push = classify(
                    [path],
                    event="push",
                    target_sha=self.TARGET_SHA,
                    branch="dev",
                )
                self.assertEqual(pull_request["class"], "full")
                self.assertFalse(pull_request["deployable"])
                self.assertFalse(pull_request["fallback"])
                self.assertEqual(push["class"], "full")
                self.assertTrue(push["deployable"])
                self.assertFalse(push["fallback"])
                validate_manifest(push, expected_target_sha=self.TARGET_SHA, require_deployable=True)

        recovery_only = classify(
            [".github/workflows/platform-production-deploy.yml"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(recovery_only["class"], "full")
        self.assertFalse(recovery_only["deployable"])
        self.assertFalse(recovery_only["fallback"])
        self.assertEqual(recovery_only["reason"], RECOVERY_BOOTSTRAP_REASON)
        validate_manifest(recovery_only, expected_target_sha=self.TARGET_SHA)

        mixed = classify(
            [
                ".github/workflows/platform-host-tools-candidate.yml",
                "platform/apps/platform_api/app/main.py",
            ],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(mixed["class"], "full")
        self.assertTrue(mixed["deployable"])
        validate_manifest(mixed, expected_target_sha=self.TARGET_SHA, require_deployable=True)

        for application_path in (
            "platform/apps/platform_api/app/main.py",
            "platform/tools/platform_build_live_qa_runtime.py",
        ):
            with self.subTest(mixed_application_path=application_path):
                mixed_control = classify(
                    [
                        ".github/workflows/platform-production-autodeploy.yml",
                        ".github/workflows/platform-security.yml",
                        "platform/tests/test_platform_ci_classifier.py",
                        "platform/tools/platform_ci_classifier.py",
                        application_path,
                    ],
                    event="push",
                    target_sha=self.TARGET_SHA,
                    branch="dev",
                )
                self.assertEqual(mixed_control["class"], "full")
                self.assertFalse(mixed_control["fallback"])
                self.assertTrue(mixed_control["deployable"])
                if application_path in RUNTIME_SENSITIVE_FILES:
                    self.assertTrue(mixed_control["runtime_sensitive"])
                validate_manifest(
                    mixed_control,
                    expected_target_sha=self.TARGET_SHA,
                    require_deployable=True,
                )

        unknown_mixed = classify(
            [
                ".github/workflows/platform-security.yml",
                "platform/tools/platform_ci_classifier.py",
                "unknown-root-config.toml",
            ],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(unknown_mixed["class"], "full")
        self.assertTrue(unknown_mixed["fallback"])
        self.assertTrue(unknown_mixed["runtime_sensitive"])
        self.assertFalse(unknown_mixed["deployable"])
        validate_manifest(unknown_mixed, expected_target_sha=self.TARGET_SHA)

        for path, expected_class, expected_fallback, expected_gates in (
            (
                "platform/docs/deployment-runbook.md",
                "docs-only",
                False,
                DOCS_ONLY_GATE_IDS,
            ),
            (
                "unknown-root-config.toml",
                "full",
                True,
                FULL_GATE_IDS,
            ),
        ):
            for event, branch in (
                ("pull_request", "feature/candidate"),
                ("push", "dev"),
            ):
                with self.subTest(path=path, event=event):
                    manifest = classify(
                        [path],
                        event=event,
                        target_sha=self.TARGET_SHA,
                        branch=branch,
                    )
                    self.assertEqual(manifest["class"], expected_class)
                    self.assertEqual(manifest["fallback"], expected_fallback)
                    self.assertEqual(manifest["expected_gates"], list(expected_gates))
                    self.assertFalse(manifest["deployable"])
                    validate_manifest(manifest, expected_target_sha=self.TARGET_SHA)

    def test_release_runtime_sensitivity_is_exact_and_digest_bound(self) -> None:
        runtime_path = "platform/tools/platform_build_live_qa_runtime.py"
        self.assertIn(runtime_path, RUNTIME_SENSITIVE_FILES)
        runtime = classify(
            [runtime_path],
            event="pull_request",
            target_sha=self.TARGET_SHA,
        )
        self.assertTrue(runtime["runtime_sensitive"])
        validate_manifest(runtime, expected_target_sha=self.TARGET_SHA)

        for files in (
            ["platform/docs/test-suite-governance.md"],
            ["platform/apps/platform_web/package.json"],
        ):
            with self.subTest(files=files):
                manifest = classify(
                    files,
                    event="pull_request",
                    target_sha=self.TARGET_SHA,
                )
                self.assertFalse(manifest["runtime_sensitive"])
                validate_manifest(manifest, expected_target_sha=self.TARGET_SHA)

        tampered = dict(runtime)
        tampered["runtime_sensitive"] = False
        tampered["digest"] = manifest_digest(tampered)
        with self.assertRaises(ClassifierError):
            validate_manifest(tampered)

    def test_out_of_scope_change_is_contract_only_and_never_deployable(self) -> None:
        manifest = classify(
            ["oldsparky_app/services/legacy.py", "README.md"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(manifest["class"], "out-of-scope")
        self.assertEqual(manifest["expected_gates"], list(OUT_OF_SCOPE_GATE_IDS))
        self.assertFalse(manifest["deployable"])
        self.assertFalse(manifest["fallback"])
        validate_manifest(manifest, expected_target_sha=self.TARGET_SHA)

    def test_unknown_global_dependency_and_merge_events_fail_closed(self) -> None:
        unknown = classify(
            ["new-root-build-config.toml"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(unknown["class"], "full")
        self.assertTrue(unknown["fallback"])
        self.assertTrue(unknown["runtime_sensitive"])
        self.assertFalse(unknown["deployable"])

        merge_group = classify(
            ["platform/docs/CURRENT.md"],
            event="merge_group",
            target_sha=self.TARGET_SHA,
            branch="",
        )
        self.assertEqual(merge_group["class"], "full")
        self.assertTrue(merge_group["fallback"])
        self.assertTrue(merge_group["runtime_sensitive"])
        self.assertFalse(merge_group["deployable"])

        workflow_dispatch = classify(
            ["platform/docs/CURRENT.md"],
            event="workflow_dispatch",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(workflow_dispatch["event"], "workflow_dispatch")
        self.assertEqual(workflow_dispatch["class"], "docs-only")
        self.assertFalse(workflow_dispatch["fallback"])
        self.assertTrue(workflow_dispatch["runtime_sensitive"])
        self.assertFalse(workflow_dispatch["deployable"])

        shallow = classify(
            ["platform/docs/CURRENT.md"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
            repository_ready=False,
        )
        self.assertEqual(shallow["class"], "full")
        self.assertTrue(shallow["fallback"])
        self.assertTrue(shallow["runtime_sensitive"])
        self.assertFalse(shallow["deployable"])

    def test_malformed_input_cannot_be_promoted_by_tampering(self) -> None:
        malformed = classify(
            ["platform/docs/CURRENT.md", "../outside"],
            event="push",
            target_sha="not-a-sha",
            branch="dev",
        )
        self.assertEqual(malformed["class"], "full")
        self.assertTrue(malformed["fallback"])
        self.assertTrue(malformed["runtime_sensitive"])
        self.assertFalse(malformed["deployable"])
        validate_manifest(malformed)
        tampered = dict(malformed)
        tampered["expected_gates"] = list(DOCS_ONLY_GATE_IDS)
        with self.assertRaises(ClassifierError):
            validate_manifest(tampered)

    def test_security_workflow_is_always_routed_without_top_level_path_filters(self) -> None:
        workflow = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("merge_group:", workflow)
        self.assertNotRegex(workflow, re.compile(r"^\s{4}paths(?:-ignore)?:", re.MULTILINE))
        self.assertIn("platform_ci_classifier.py", workflow)
        self.assertIn("classifier-manifest.json", workflow)
        self.assertIn("if: ${{ always() }}", workflow)
        self.assertIn("platform-security-build", workflow)
        self.assertIn("expected_gates", workflow)
        self.assertIn("runtime_sensitive: ${{ steps.classify.outputs.runtime_sensitive }}", workflow)
        self.assertIn("release-runtime", workflow)
        self.assertIn("release-runtime-real", workflow)
        self.assertIn("name: Conditional release runtime fixture", workflow)
        self.assertIn("name: Trusted dev immutable release runtime", workflow)
        self.assertIn(
            "needs.classifier.outputs.runtime_sensitive == 'true' || needs.classifier.outputs.fallback == 'true'",
            workflow,
        )
        self.assertNotIn("schedule:", workflow)

    def test_cancel_safe_status_finalizer_overwrites_every_terminal_conclusion(self) -> None:
        workflow = STATUS_FINALIZER_WORKFLOW.read_text(encoding="utf-8")
        security = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_run:", workflow)
        self.assertIn("workflows: [Platform security and build]", workflow)
        self.assertIn("types: [completed]", workflow)
        self.assertNotIn("always()", workflow)
        self.assertIn("  authority:", workflow)
        self.assertIn("  finalize-status:", workflow)
        self.assertLess(workflow.index("  authority:"), workflow.index("  finalize-status:"))
        authority = workflow.split("  finalize-status:", 1)[0]
        writer = workflow.split("  finalize-status:", 1)[1]
        self.assertNotIn("statuses: write", authority)
        self.assertNotIn("actions/checkout@", authority)
        self.assertIn("needs: [authority]", writer)
        self.assertIn("if: ${{ needs.authority.result == 'success' }}", writer)
        self.assertIn("needs.authority.outputs.reconciler_sha", writer)
        self.assertIn("AUTHORITY_SOURCE_SHA", writer)
        self.assertIn("AUTHORITY_SOURCE_RUN_ID", writer)
        self.assertIn("AUTHORITY_SOURCE_RUN_ATTEMPT", writer)
        self.assertIn(
            "permissions:\n      actions: read\n      contents: read",
            authority,
        )
        self.assertEqual(workflow.count("statuses: write"), 1)
        self.assertIn(
            "group: ${{ github.workflow }}-${{ github.event.workflow_run.id }}-${{ github.event.workflow_run.run_attempt }}",
            workflow,
        )
        self.assertIn("actions/checkout@", writer)
        self.assertIn("ref: ${{ needs.authority.outputs.reconciler_sha }}", writer)
        self.assertIn("path: trusted-status-source", workflow)
        self.assertIn("persist-credentials: false", workflow)
        for field in (
            "github.event.workflow_run.repository.full_name",
            "github.event.workflow_run.head_repository.full_name",
            "github.event.workflow_run.workflow_id",
            "github.event.workflow_run.name",
            "github.event.workflow_run.path",
            "github.event.workflow_run.event",
            "github.event.workflow_run.head_branch",
            "github.event.workflow_run.status",
            "github.event.workflow_run.head_sha",
            "github.event.workflow_run.id",
            "github.event.workflow_run.run_attempt",
        ):
            self.assertIn(field, workflow)
        for field in (
            "339062797",
            "Platform security and build",
            ".github/workflows/platform-security.yml",
            "EXPECTED_EVENT: push",
            "EXPECTED_BRANCH: dev",
            "EXPECTED_STATUS: completed",
            'env.get("GITHUB_SERVER_URL")',
            'env.get("GITHUB_API_URL")',
            'env.get("GITHUB_EVENT_NAME")',
            'expected_fields = ("id", "run_attempt", "workflow_id", "name", "path", "event", "head_branch", "status", "conclusion", "head_sha")',
        ):
            self.assertIn(field, workflow)
        self.assertNotIn("download-artifact", workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertIn("reconcile --event-file", workflow)
        self.assertNotIn("platform_release", workflow)
        self.assertNotIn("/statuses/", authority)
        curl_lines = [line.strip() for line in workflow.splitlines() if line.strip().startswith("curl ")]
        self.assertEqual(len(curl_lines), 3)
        for line in curl_lines:
            self.assertIn("--fail --silent --show-error --request GET", line)
        self.assertEqual(workflow.count("--connect-timeout 5"), 3)
        self.assertEqual(workflow.count("--max-time 15"), 3)
        self.assertEqual(workflow.count("--retry 0"), 3)
        self.assertEqual(workflow.count("--max-filesize 65536"), 3)
        self.assertIn("actions/runs/$SOURCE_RUN_ID", workflow)
        self.assertIn("actions/workflows/$EXPECTED_WORKFLOW_ID", workflow)
        self.assertIn("len(raw) > 65536", workflow)
        self.assertIn("reconciler_sha=", workflow)
        # The trusted checkout SHA is implementation provenance only.  The
        # completed source SHA is intentionally allowed to be older than dev.
        self.assertIn("source SHA is not required to be the current dev head", workflow)
        self.assertNotIn("SOURCE_SHA == ref", workflow)
        self.assertNotIn("SOURCE_SHA != ref", workflow)
        self.assertLess(
            workflow.index("name: Platform security status finalizer"),
            workflow.index("reconcile --event-file"),
        )

        # Execute the two inline authority validators against independent
        # fixtures.  The mutation matrix keeps each source/API identity field
        # fail-closed without importing the workflow implementation.
        source_marker = '          /usr/bin/python3 -I -B - "$GITHUB_EVENT_PATH" "$GITHUB_OUTPUT" <<\'PY\'\n'
        source_start = workflow.index(source_marker) + len(source_marker)
        source_end = workflow.index("          PY\n", source_start)
        source_contract = textwrap.dedent(workflow[source_start:source_end])
        metadata_marker = '          /usr/bin/python3 -I -B - "$metadata_dir" "$GITHUB_EVENT_PATH" "$GITHUB_OUTPUT" <<\'PY\'\n'
        metadata_start = workflow.index(metadata_marker) + len(metadata_marker)
        metadata_end = workflow.index("          PY\n", metadata_start)
        metadata_contract = textwrap.dedent(workflow[metadata_start:metadata_end])

        source_sha = "a" * 40
        source_run = {
            "id": 123456,
            "run_attempt": 2,
            "workflow_id": 339062797,
            "name": "Platform security and build",
            "path": ".github/workflows/platform-security.yml",
            "event": "push",
            "head_branch": "dev",
            "status": "completed",
            "conclusion": "cancelled",
            "head_sha": source_sha,
            "repository": {"full_name": "StrayForest/old_sparky"},
            "head_repository": {"full_name": "StrayForest/old_sparky"},
        }
        source_event = {"workflow_run": source_run}
        source_environment = os.environ.copy()
        source_environment.update(
            {
                "GITHUB_REPOSITORY": "StrayForest/old_sparky",
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_API_URL": "https://api.github.com",
                "GITHUB_EVENT_NAME": "workflow_run",
                "GITHUB_REF": "refs/heads/dev",
                "EXPECTED_REPOSITORY": "StrayForest/old_sparky",
                "EXPECTED_SERVER_URL": "https://github.com",
                "EXPECTED_API_URL": "https://api.github.com",
                "EXPECTED_WORKFLOW_ID": "339062797",
                "EXPECTED_WORKFLOW_NAME": "Platform security and build",
                "EXPECTED_WORKFLOW_PATH": ".github/workflows/platform-security.yml",
                "EXPECTED_EVENT": "push",
                "EXPECTED_BRANCH": "dev",
                "EXPECTED_STATUS": "completed",
                "SOURCE_REPOSITORY": "StrayForest/old_sparky",
                "SOURCE_HEAD_REPOSITORY": "StrayForest/old_sparky",
                "SOURCE_WORKFLOW_ID": "339062797",
                "SOURCE_WORKFLOW_NAME": "Platform security and build",
                "SOURCE_WORKFLOW_PATH": ".github/workflows/platform-security.yml",
                "SOURCE_EVENT": "push",
                "SOURCE_HEAD_BRANCH": "dev",
                "SOURCE_STATUS": "completed",
                "SOURCE_HEAD_SHA": source_sha,
                "SOURCE_RUN_ID": "123456",
                "SOURCE_RUN_ATTEMPT": "2",
            }
        )

        def run_inline(
            contract: str,
            args: list[str],
            environment: Mapping[str, str],
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-I", "-B", "-", *args],
                input=contract,
                check=False,
                capture_output=True,
                text=True,
                env=dict(environment),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / "event.json"
            output_path = root / "output"
            event_path.write_text(json.dumps(source_event), encoding="utf-8")
            valid = run_inline(
                source_contract,
                [str(event_path), str(output_path)],
                source_environment,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            for label, mutate in (
                ("repository", lambda candidate: candidate["repository"].update(full_name="fork/old_sparky")),
                ("head_repository", lambda candidate: candidate["head_repository"].update(full_name="fork/old_sparky")),
                ("workflow_id", lambda candidate: candidate.update(workflow_id=1)),
                ("name", lambda candidate: candidate.update(name="Other workflow")),
                ("path", lambda candidate: candidate.update(path=".github/workflows/other.yml")),
                ("event", lambda candidate: candidate.update(event="workflow_dispatch")),
                ("branch", lambda candidate: candidate.update(head_branch="feature")),
                ("status", lambda candidate: candidate.update(status="in_progress")),
                ("sha", lambda candidate: candidate.update(head_sha=source_sha.upper())),
                ("run_id", lambda candidate: candidate.update(id=0)),
                ("run_attempt", lambda candidate: candidate.update(run_attempt=0)),
                ("conclusion", lambda candidate: candidate.pop("conclusion")),
            ):
                candidate = json.loads(json.dumps(source_event))
                mutate(candidate["workflow_run"])
                event_path.write_text(json.dumps(candidate), encoding="utf-8")
                rejected = run_inline(
                    source_contract,
                    [str(event_path), str(output_path)],
                    source_environment,
                )
                self.assertNotEqual(rejected.returncode, 0, label)
            for label, key, value in (
                ("server", "GITHUB_SERVER_URL", "https://evil.example"),
                ("api", "GITHUB_API_URL", "https://api.example"),
                ("context", "GITHUB_REPOSITORY", "fork/old_sparky"),
            ):
                mutated_environment = dict(source_environment)
                mutated_environment[key] = value
                event_path.write_text(json.dumps(source_event), encoding="utf-8")
                rejected = run_inline(
                    source_contract,
                    [str(event_path), str(output_path)],
                    mutated_environment,
                )
                self.assertNotEqual(rejected.returncode, 0, label)

        metadata_run = dict(source_run)
        metadata_workflow = {
            "id": 339062797,
            "name": "Platform security and build",
            "path": ".github/workflows/platform-security.yml",
        }
        metadata_ref = {
            "ref": "refs/heads/dev",
            "object": {"type": "commit", "sha": "b" * 40},
        }
        metadata_environment = os.environ.copy()
        metadata_environment.update(
            {
                "SOURCE_SHA": source_sha,
                "SOURCE_RUN_ID": "123456",
                "SOURCE_RUN_ATTEMPT": "2",
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / "event.json"
            output_path = root / "output"
            event_path.write_text(json.dumps(source_event), encoding="utf-8")

            def write_metadata(
                run: object = metadata_run,
                workflow_metadata: object = metadata_workflow,
                ref_metadata: object = metadata_ref,
            ) -> Path:
                metadata_path = root / "metadata"
                metadata_path.mkdir(exist_ok=True)
                (metadata_path / "run.json").write_text(json.dumps(run), encoding="utf-8")
                (metadata_path / "workflow.json").write_text(json.dumps(workflow_metadata), encoding="utf-8")
                (metadata_path / "ref.json").write_text(json.dumps(ref_metadata), encoding="utf-8")
                return metadata_path

            metadata_path = write_metadata()
            valid = run_inline(
                metadata_contract,
                [str(metadata_path), str(event_path), str(output_path)],
                metadata_environment,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertIn("reconciler_sha=", output_path.read_text(encoding="ascii"))
            for label, field, value in (
                ("API run ID", "id", 123457),
                ("API attempt", "run_attempt", 3),
                ("API workflow ID", "workflow_id", 1),
                ("API name", "name", "Other workflow"),
                ("API path", "path", ".github/workflows/other.yml"),
                ("API event", "event", "workflow_dispatch"),
                ("API branch", "head_branch", "feature"),
                ("API status", "status", "in_progress"),
                ("API repository", "repository", {"full_name": "fork/old_sparky"}),
                ("API head_repository", "head_repository", {"full_name": "fork/old_sparky"}),
                ("API SHA", "head_sha", "c" * 40),
            ):
                candidate = dict(metadata_run)
                candidate[field] = value
                metadata_path = write_metadata(run=candidate)
                rejected = run_inline(
                    metadata_contract,
                    [str(metadata_path), str(event_path), str(output_path)],
                    metadata_environment,
                )
                self.assertNotEqual(rejected.returncode, 0, label)
            for label, field, value in (
                ("API workflow ID", "id", 1),
                ("API workflow name", "name", "Other workflow"),
                ("API workflow path", "path", ".github/workflows/other.yml"),
            ):
                candidate = dict(metadata_workflow)
                candidate[field] = value
                metadata_path = write_metadata(workflow_metadata=candidate)
                rejected = run_inline(
                    metadata_contract,
                    [str(metadata_path), str(event_path), str(output_path)],
                    metadata_environment,
                )
                self.assertNotEqual(rejected.returncode, 0, label)
            oversized = root / "metadata-oversized"
            oversized.mkdir()
            (oversized / "run.json").write_bytes(b"{" + b"\"x\":\"" + b"x" * 65550 + b"\"}")
            (oversized / "workflow.json").write_text(json.dumps(metadata_workflow), encoding="utf-8")
            (oversized / "ref.json").write_text(json.dumps(metadata_ref), encoding="utf-8")
            rejected = run_inline(
                metadata_contract,
                [str(oversized), str(event_path), str(output_path)],
                metadata_environment,
            )
            self.assertNotEqual(rejected.returncode, 0, "oversized API response")

        pending_at = security.index("--data", security.index("Mark platform security build pending"))
        first_gate_at = security.index("  backend-static:")
        final_at = security.index("  status-final:")
        self.assertLess(pending_at, first_gate_at)
        self.assertLess(first_gate_at, final_at)
        self.assertIn("needs: [classifier, status-start", security)

    def test_successful_reduced_routes_keep_canonical_status_and_noop_autodeploy(self) -> None:
        security = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        auto = AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("STATUS_FINAL_RESULT: ${{ needs['status-final'].result }}", security)
        self.assertNotIn("Platform security passed; route=", security)
        self.assertNotIn("fail-closed fallback route", security)
        self.assertIn('"$ROUTE_DEPLOYABLE" != "true"', auto)
        self.assertIn('echo "deploy=false" >> "$GITHUB_OUTPUT"', auto)
        self.assertIn("Dispatch production deployment", auto)
        self.assertIn("steps.gate.outputs.deploy == 'true'", auto)
        self.assertIn(
            'type(manifest.get("deployable")) is not bool',
            auto,
        )
        self.assertIn("full route may be non-deployable", auto)

        for files in (
            ["platform/docs/deployment-runbook.md"],
            ["README.md"],
            ["unknown-root-config.toml"],
        ):
            with self.subTest(files=files):
                manifest = classify(
                    files,
                    event="push",
                    target_sha=self.TARGET_SHA,
                    branch="dev",
                )
                self.assertFalse(manifest["deployable"])
                self.assertIn(manifest["class"], {"docs-only", "out-of-scope", "full"})
                status = {
                    "id": 9101,
                    "context": "platform-security-build",
                    "state": "success",
                    "description": "Platform security and build passed",
                    "target_url": "https://github.com/StrayForest/old_sparky/actions/runs/1234/attempts/2",
                    "updated_at": "2026-09-11T10:00:00Z",
                    "creator": {
                        "login": "github-actions[bot]",
                        "type": "Bot",
                        "id": 41898282,
                    },
                }
                validate_security_marker(
                    {
                        "id": 77,
                        "path": SECURITY_WORKFLOW_PATH,
                        "name": SECURITY_WORKFLOW_NAME,
                    },
                    {
                        "id": 1234,
                        "workflow_id": 77,
                        "name": SECURITY_WORKFLOW_NAME,
                        "run_attempt": 2,
                        "event": "push",
                        "head_branch": "dev",
                        "head_sha": self.TARGET_SHA,
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": "https://github.com/StrayForest/old_sparky/actions/runs/1234",
                        "repository": {
                            "full_name": "StrayForest/old_sparky",
                            "name": "old_sparky",
                            "owner": {"login": "StrayForest"},
                        },
                    },
                    [status],
                    expected_run_id=1234,
                    expected_attempt=2,
                    expected_target_sha=self.TARGET_SHA,
                    expected_run_url="https://github.com/StrayForest/old_sparky/actions/runs/1234",
                )

    def test_deploy_consumers_validate_the_exact_classifier_artifact(self) -> None:
        auto = AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        production = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        for workflow in (auto, production):
            self.assertIn("classifier-manifest.json", workflow)
            self.assertIn("target_sha", workflow)
            self.assertIn("platform-ci-route", workflow)
            self.assertIn("digest", workflow)
        self.assertIn("runtime_sensitive", auto)
        classifier_tool = (
            REPO_ROOT / "platform/tools/platform_production_classifier_artifact.py"
        ).read_text(encoding="utf-8")
        self.assertIn("runtime_sensitive", classifier_tool)
        self.assertIn("object_pairs_hook=_reject_duplicate_keys", classifier_tool)
        self.assertIn("type(manifest.get(\"deployable\")) is not bool", classifier_tool)
        self.assertIn("classifier_run_id", auto)
        self.assertIn("classifier_run_attempt", auto)
        self.assertIn("platform_production_classifier_artifact.py", production)

    def test_classifier_artifact_enumeration_covers_large_and_mutating_pages(self) -> None:
        auto = AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        production = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("?per_page=100&page=${page}", auto)
        self.assertIn("fetch_classifier_artifacts", auto)
        self.assertIn("total_count", auto)
        self.assertIn("contains duplicate IDs", auto)
        self.assertIn("pagination returned excess rows", auto)
        self.assertIn("pagination is incomplete", auto)
        self.assertIn("listing changed during validation", auto)
        self.assertIn("pagination exceeded its bound", auto)
        self.assertIn("<= 10_000", auto)
        self.assertIn("?per_page=100&page=${page}", production)
        self.assertIn("fetch_classifier_artifacts", production)
        classifier_tool = (
            REPO_ROOT / "platform/tools/platform_production_classifier_artifact.py"
        ).read_text(encoding="utf-8")
        self.assertIn("MAX_ARTIFACT_ROWS = 10_000", classifier_tool)
        self.assertIn("MAX_PAGES = 100", classifier_tool)
        self.assertIn("duplicate_keys", classifier_tool)
        self.assertIn("len(rows) != expected_total", classifier_tool)

    def test_security_run_provenance_accepts_only_the_exact_completed_run(self) -> None:
        workflow = {
            "id": 77,
            "path": SECURITY_WORKFLOW_PATH,
            "name": SECURITY_WORKFLOW_NAME,
        }
        run = {
            "id": 1234,
            "workflow_id": 77,
            "name": SECURITY_WORKFLOW_NAME,
            "run_attempt": 2,
            "event": "push",
            "head_branch": "dev",
            "head_sha": self.TARGET_SHA,
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.com/StrayForest/old_sparky/actions/runs/1234",
            "repository": {
                "full_name": "StrayForest/old_sparky",
                "name": "old_sparky",
                "owner": {"login": "StrayForest"},
            },
        }
        statuses = [
            {
                "id": 9100,
                "context": "platform-security-build",
                "state": "success",
                "description": "Platform security and build passed",
                "target_url": f"{run['html_url']}/attempts/{run['run_attempt']}",
                "updated_at": "2026-09-11T10:00:00Z",
                "creator": {
                    "login": "github-actions[bot]",
                    "type": "Bot",
                    "id": 41898282,
                },
            }
        ]
        validate_security_workflow_run(
            workflow,
            run,
            statuses,
            expected_run_id="1234",
            expected_run_attempt="2",
            expected_target_sha=self.TARGET_SHA,
        )
        validate_security_marker(
            workflow,
            run,
            statuses,
            expected_run_id=1234,
            expected_attempt=2,
            expected_target_sha=self.TARGET_SHA,
            expected_run_url=run["html_url"],
        )
        superseded_status = [dict(statuses[0])]
        superseded_status[0]["target_url"] = f"{run['html_url']}/attempts/3"
        with self.assertRaises(ProvenanceError):
            validate_security_marker(
                workflow,
                run,
                superseded_status,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.TARGET_SHA,
                expected_run_url=run["html_url"],
            )
        with self.assertRaises(ProvenanceError):
            validate_security_marker(
                workflow,
                run,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.TARGET_SHA,
                expected_run_url=f"{run['html_url']}/wrong",
            )
        invalid_statuses = []
        missing_creator = dict(statuses[0])
        missing_creator.pop("creator")
        invalid_statuses.append(("missing creator", missing_creator))
        for label, value in (
            ("null creator", None),
            (
                "wrong creator",
                {"login": "attacker", "type": "User", "id": 1},
            ),
            ("wrong context", "other-context"),
            ("wrong state", "failure"),
            (
                "wrong target URL",
                "https://github.com/StrayForest/old_sparky/actions/runs/1234/attempts/1",
            ),
        ):
            candidate = dict(statuses[0])
            if "creator" in label:
                candidate["creator"] = value
            else:
                status_field = {
                    "wrong context": "context",
                    "wrong state": "state",
                    "wrong target URL": "target_url",
                }[label]
                candidate[status_field] = value
            invalid_statuses.append((label, candidate))
        for label, candidate in invalid_statuses:
            with self.subTest(status=label):
                with self.assertRaises(ClassifierError):
                    validate_security_workflow_run(
                        workflow,
                        run,
                        [candidate],
                        expected_run_id="1234",
                        expected_run_attempt="2",
                        expected_target_sha=self.TARGET_SHA,
                    )
        mismatches = {
            "run id": ("expected_run_id", "1235"),
            "run attempt": ("expected_run_attempt", "3"),
            "branch": ("run", {**run, "head_branch": "feature"}),
            "SHA": ("run", {**run, "head_sha": "b" * 40}),
            "event": ("run", {**run, "event": "workflow_dispatch"}),
            "conclusion": ("run", {**run, "conclusion": "failure"}),
        }
        for label, (kind, value) in mismatches.items():
            with self.subTest(mismatch=label):
                if kind == "expected_run_id":
                    kwargs = {
                        "expected_run_id": value,
                        "expected_run_attempt": "2",
                        "expected_target_sha": self.TARGET_SHA,
                    }
                    candidate_run = run
                elif kind == "expected_run_attempt":
                    kwargs = {
                        "expected_run_id": "1234",
                        "expected_run_attempt": value,
                        "expected_target_sha": self.TARGET_SHA,
                    }
                    candidate_run = run
                else:
                    kwargs = {
                        "expected_run_id": "1234",
                        "expected_run_attempt": "2",
                        "expected_target_sha": self.TARGET_SHA,
                    }
                    candidate_run = value
                with self.assertRaises(ClassifierError):
                    validate_security_workflow_run(
                        workflow,
                        candidate_run,
                        statuses,
                        **kwargs,
                    )
        for malformed_workflow, malformed_run, malformed_statuses in (
            (None, run, statuses),
            (workflow, None, statuses),
            (workflow, run, None),
            (workflow, run, [None]),
        ):
            with self.subTest(
                malformed=(malformed_workflow, malformed_run, malformed_statuses)
            ):
                with self.assertRaises(ClassifierError):
                    validate_security_workflow_run(
                        malformed_workflow,
                        malformed_run,
                        malformed_statuses,
                        expected_run_id="1234",
                        expected_run_attempt="2",
                        expected_target_sha=self.TARGET_SHA,
                    )

    def test_release_workflows_require_exact_run_metadata_and_status_binding(self) -> None:
        auto = AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        production = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("platform_workflow_provenance.py", auto)
        self.assertGreaterEqual(auto.count('"$PROVENANCE_TOOL" security'), 3)
        self.assertIn('"$PROVENANCE_TOOL" deployment', auto)
        self.assertNotIn("from datetime import", auto)
        self.assertNotIn("timestamp_re =", auto)
        self.assertNotIn("sorted(set(candidates)", auto)
        self.assertIn("statuses: read", auto)
        self.assertIn("?per_page=100&page=${page}", auto)
        self.assertIn("for snapshot in first second", auto)
        self.assertIn("if first != second:", auto)
        self.assertIn("/commits/${TARGET_SHA}/statuses?per_page=100&page=${page}", auto)
        self.assertIn("fetch_statuses", auto)
        self.assertIn("security status list response is malformed", auto)
        self.assertIn("status pagination response is malformed", auto)
        self.assertIn("status rows contain duplicate or malformed IDs", auto)
        self.assertIn("status pagination exceeded its bound", auto)
        self.assertNotIn("/commits/${TARGET_SHA}/status?per_page=100&page=${page}", auto)
        self.assertNotIn("/commits/${TARGET_SHA}/status\"", auto)
        self.assertNotIn("combined-status pagination", auto)
        self.assertIn("deployment run pagination exceeded its bound", auto)
        self.assertIn("deployment job pagination exceeded its bound", auto)
        self.assertIn("contains duplicate IDs", auto)
        self.assertIn("total_count changed during pagination", auto)
        self.assertIn("SECURITY_WORKFLOW_PATH: .github/workflows/platform-security.yml", auto)
        self.assertIn("SECURITY_WORKFLOW_FILE: platform-security.yml", auto)
        self.assertIn("actions/workflows/${SECURITY_WORKFLOW_FILE}", auto)
        self.assertIn("actions/workflows/platform-security.yml", production)
        for workflow, run_token in (
            (auto, "actions/runs/${SOURCE_RUN_ID}"),
            (production, "actions/runs/${CLASSIFIER_RUN_ID}"),
        ):
            self.assertIn(run_token, workflow)
            for field in (
                '"event": "push"',
                '"head_branch": "dev"',
                '"status": "completed"',
                '"conclusion": "success"',
                "run_attempt",
                "target_url",
            ):
                self.assertIn(field, workflow)
        self.assertIn("platform-security-build", auto)

    def test_status_final_is_fail_closed_for_published_statuses_and_routes(self) -> None:
        workflow = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        status_final = workflow.split("  status-final:", 1)[1].split("  status-publish:", 1)[0]
        for event in ("pull_request", "push", "merge_group", "workflow_dispatch"):
            self.assertRegex(workflow, rf"(?m)^  {event}:", msg=f"missing {event} trigger")
        self.assertGreaterEqual(workflow.count("TESTED_SHA: ${{ github.sha }}"), 3)
        self.assertIn('--target-sha "$TESTED_SHA"', workflow)
        self.assertIn('source_head = (pull.get("head") or {}).get("sha")', workflow)
        self.assertIn(
            '["git", "diff", "--name-only", "-z", base, source_head, "--"]',
            workflow,
        )
        self.assertIn("platform_security_status.py", workflow)
        self.assertIn("group: ${{ github.workflow }}-${{ github.ref }}", workflow)
        for job_id in ("status-start", "status-final", "status-publish"):
            self.assertIn(f"  {job_id}:", workflow)
        self.assertIn("expected_by_class", status_final)
        self.assertIn("json.loads(os.environ.get(\"EXPECTED_GATES\")", status_final)
        self.assertIn('export SUMMARY_PATH="$GITHUB_WORKSPACE/platform-security-summary.json"', status_final)
        self.assertNotIn("--github-output \"$GITHUB_OUTPUT\"", status_final)
        self.assertNotIn("id: evaluate_status", status_final)
        self.assertIn("permissions:\n      contents: read", status_final)
        self.assertNotIn("statuses: write", status_final)
        self.assertNotIn("platform_security_status.py", status_final)
        self.assertNotIn("actions/checkout", status_final)
        self.assertIn('statuses/${TESTED_SHA}', workflow)
        self.assertNotIn('statuses/${GITHUB_SHA}', workflow)
        status_publish = workflow.split("  status-publish:", 1)[1]
        self.assertIn(
            "if: ${{ always() && (github.event_name == 'push' || github.event_name == 'workflow_dispatch') && github.ref == 'refs/heads/dev' }}",
            status_publish,
        )
        self.assertIn("needs: [status-final]", status_publish)
        self.assertIn(
            "permissions:\n      actions: read\n      contents: read\n      statuses: write",
            status_publish,
        )
        self.assertIn("actions/checkout@", status_publish)
        self.assertIn("ref: ${{ github.sha }}", status_publish)
        self.assertIn("path: trusted-status-source", status_publish)
        self.assertIn("persist-credentials: false", status_publish)
        self.assertIn("platform_security_status.py", status_publish)
        self.assertIn("publish --event-file", status_publish)
        self.assertNotIn("/statuses/", status_publish)
        self.assertIn("if: ${{ (github.event_name == 'push' || github.event_name == 'workflow_dispatch') && github.ref == 'refs/heads/dev' }}", workflow)
        self.assertNotIn("GITHUB_ENV", status_final)
        self.assertNotIn("ROUTE_PASSED", status_final)

        # A pull request's source head can differ from the synthetic merge SHA
        # being tested; only the former belongs in the changed-file range.
        pr_source_head = "b" * 40
        self.assertNotEqual(pr_source_head, self.TARGET_SHA)
        pr_manifest = classify(
            ["platform/apps/platform_api/app/main.py"],
            event="pull_request",
            target_sha=self.TARGET_SHA,
        )
        self.assertEqual(pr_manifest["target_sha"], self.TARGET_SHA)
        self.assertEqual(pr_manifest["event"], "pull_request")
        self.assertIn("STATUS_START_RESULT", workflow)
        self.assertIn("ROUTE_EVENT", workflow)
        self.assertIn("needs['release-runtime-real']", workflow)

        base_environment = {
            "CLASSIFIER_RESULT": "success",
            "EVENT_NAME": "pull_request",
            "ROUTE_EVENT": "pull_request",
            "ROUTE_CLASS": "docs-only",
            "ROUTE_DEPLOYABLE": "false",
            "ROUTE_FALLBACK": "false",
            "ROUTE_RUNTIME_SENSITIVE": "false",
            "ROUTE_REASON": "trusted docs route",
            "ROUTE_DIGEST": "a" * 64,
            "ROUTE_TARGET_SHA": self.TARGET_SHA,
            "TESTED_SHA": self.TARGET_SHA,
            "EXPECTED_GATES": '["docs", "verification-contract"]',
            "DOCS_RESULT": "success",
            "VERIFICATION_CONTRACT_RESULT": "success",
            "STATUS_START_RESULT": "skipped",
            "WORKFLOW_REF": "refs/heads/feature",
            "RELEASE_RUNTIME_RESULT": "skipped",
            "RELEASE_RUNTIME_REAL_RESULT": "skipped",
        }

        # Every classifier route remains a pure decision, including its exact
        # gate order; a pull request does not publish a commit status.
        for route_class, expected_gates in (
            ("docs-only", ["docs", "verification-contract"]),
            ("out-of-scope", ["verification-contract"]),
            (
                "full",
                [
                    "backend",
                    "python-quality",
                    "security",
                    "migration",
                    "docs",
                    "web-quality",
                    "web-hermetic",
                    "verification-contract",
                ],
            ),
        ):
            with self.subTest(route=route_class):
                environment = dict(base_environment)
                environment.update(
                    {
                        "ROUTE_CLASS": route_class,
                        "EXPECTED_GATES": json.dumps(expected_gates),
                    }
                )
                for gate in expected_gates:
                    environment[gate.upper().replace("-", "_") + "_RESULT"] = "success"
                passed, summary = evaluate_status(environment)
                self.assertTrue(passed)
                self.assertEqual(summary["expected_gates"], expected_gates)
                self.assertEqual(summary["gate_results"][expected_gates[0]], "success")

        full_environment = dict(base_environment)
        full_environment.update(
            {
                "EVENT_NAME": "push",
                "ROUTE_EVENT": "push",
                "ROUTE_CLASS": "full",
                "WORKFLOW_REF": "refs/heads/dev",
                "STATUS_START_RESULT": "success",
                "ROUTE_RUNTIME_SENSITIVE": "true",
                "EXPECTED_GATES": json.dumps(
                    [
                        "backend",
                        "python-quality",
                        "security",
                        "migration",
                        "docs",
                        "web-quality",
                        "web-hermetic",
                        "verification-contract",
                    ]
                ),
                "RELEASE_RUNTIME_RESULT": "success",
                "RELEASE_RUNTIME_REAL_RESULT": "success",
            }
        )
        for gate in (
            "backend",
            "python-quality",
            "security",
            "migration",
            "docs",
            "web-quality",
            "web-hermetic",
            "verification-contract",
        ):
            full_environment[gate.upper().replace("-", "_") + "_RESULT"] = "success"
        passed, summary = evaluate_status(full_environment)
        self.assertTrue(passed)
        self.assertTrue(summary["requires_release_runtime"])
        self.assertTrue(summary["requires_real_release_runtime"])

        trusted_result_cases = (
            ("dev push real skipped", "push", "refs/heads/dev", "true", "false", "success", "skipped", False),
            ("dev push real failure", "push", "refs/heads/dev", "true", "false", "failure", "success", False),
            ("dev manual real", "workflow_dispatch", "refs/heads/dev", "true", "false", "success", "success", True),
            ("fallback dev real", "push", "refs/heads/dev", "true", "true", "success", "success", True),
            ("ordinary dev both skipped", "push", "refs/heads/dev", "false", "false", "skipped", "skipped", True),
            (
                "non-dev manual fixture only",
                "workflow_dispatch",
                "refs/heads/feature",
                "true",
                "false",
                "success",
                "skipped",
                True,
            ),
            (
                "merge group fixture only",
                "merge_group",
                "refs/heads/queue",
                "true",
                "false",
                "success",
                "skipped",
                True,
            ),
        )
        for label, event, ref, runtime, fallback, fixture, real, expected_passed in trusted_result_cases:
            with self.subTest(trusted_result=label):
                environment = dict(full_environment)
                environment.update(
                    {
                        "EVENT_NAME": event,
                        "ROUTE_EVENT": event,
                        "WORKFLOW_REF": ref,
                        "ROUTE_RUNTIME_SENSITIVE": runtime,
                        "ROUTE_FALLBACK": fallback,
                        "RELEASE_RUNTIME_RESULT": fixture,
                        "RELEASE_RUNTIME_REAL_RESULT": real,
                        "STATUS_START_RESULT": "success"
                        if event in {"push", "workflow_dispatch"} and ref == "refs/heads/dev"
                        else "skipped",
                    }
                )
                passed, summary = evaluate_status(environment)
                self.assertEqual(passed, expected_passed)
                self.assertEqual(
                    summary["requires_real_release_runtime"],
                    runtime == "true"
                    and ref == "refs/heads/dev"
                    and event in {"push", "workflow_dispatch"},
                )

        malformed_cases = {
            "missing route": {"ROUTE_CLASS": ""},
            "malformed expected gates": {"EXPECTED_GATES": "not-json"},
            "malformed expected item": {"EXPECTED_GATES": "[{}]"},
            "missing SHA": {"TESTED_SHA": ""},
            "mismatched SHA": {"ROUTE_TARGET_SHA": "b" * 40},
            "missing digest": {"ROUTE_DIGEST": ""},
            "missing reason": {"ROUTE_REASON": ""},
            "invalid deployable": {"ROUTE_DEPLOYABLE": "TRUE"},
            "invalid fallback": {"ROUTE_FALLBACK": "TRUE"},
            "invalid runtime sensitivity": {"ROUTE_RUNTIME_SENSITIVE": "TRUE"},
        }
        for label, updates in malformed_cases.items():
            with self.subTest(malformed=label):
                environment = dict(base_environment)
                environment.update(updates)
                passed, _summary = evaluate_status(environment)
                self.assertFalse(passed)

        for label, updates in (
            ("missing gate", {"DOCS_RESULT": "missing"}),
            ("skipped gate", {"DOCS_RESULT": "skipped"}),
            ("cancelled gate", {"DOCS_RESULT": "cancelled"}),
            ("failed classifier", {"CLASSIFIER_RESULT": "failure"}),
        ):
            with self.subTest(result=label):
                environment = dict(base_environment)
                environment.update(updates)
                passed, summary = evaluate_status(environment)
                self.assertFalse(passed)
                self.assertIsInstance(summary["missing_or_failed"], list)

        for label, updates in (
            ("required fixture skipped", {"ROUTE_RUNTIME_SENSITIVE": "true"}),
            ("required fallback fixture skipped", {"ROUTE_FALLBACK": "true"}),
            ("unexpected fixture success", {"RELEASE_RUNTIME_RESULT": "success"}),
            ("unexpected real result", {"RELEASE_RUNTIME_REAL_RESULT": "success"}),
        ):
            with self.subTest(conditional_result=label):
                environment = dict(base_environment)
                environment.update(updates)
                passed, _summary = evaluate_status(environment)
                self.assertFalse(passed)

        push_without_start = dict(full_environment)
        push_without_start["STATUS_START_RESULT"] = "skipped"
        self.assertFalse(evaluate_status(push_without_start)[0])
        for conclusion in (
            "cancelled",
            "failure",
            "skipped",
            "timed_out",
            "action_required",
            "neutral",
            "stale",
            "startup_failure",
            None,
        ):
            with self.subTest(conclusion=conclusion):
                self.assertTrue(terminal_failure_required("completed", conclusion))
        self.assertTrue(terminal_failure_required("in_progress", None))
        self.assertFalse(terminal_failure_required("completed", "success"))
        self.assertEqual(
            set(evaluate_status(base_environment)[1]),
            {
                "schema",
                "tested_sha",
                "event",
                "route_event",
                "class",
                "reason",
                "deployable",
                "fallback",
                "manifest_digest",
                "expected_gates",
                "gate_results",
                "conditional_gate_results",
                "runtime_sensitive",
                "requires_release_runtime",
                "requires_real_release_runtime",
                "missing_or_failed",
                "route_errors",
                "status_start_result",
                "passed",
            },
        )

        run_sha = "c" * 40
        def run_row(run_id: int, attempt: int = 1, event: str = "push") -> dict[str, object]:
            return {
                "id": run_id,
                "run_attempt": attempt,
                "head_sha": run_sha,
                "head_branch": "dev",
                "event": event,
                "name": SECURITY_WORKFLOW_NAME,
                "path": SECURITY_WORKFLOW_PATH,
                "repository": {"full_name": REPOSITORY},
                "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
            }

        pages = [
            {"total_count": 2, "workflow_runs": [run_row(12)]},
            {"total_count": 2, "workflow_runs": [run_row(11)]},
        ]
        self.assertEqual(
            complete_workflow_run_keys(pages, expected_sha=run_sha),
            ((12, 1), (11, 1)),
        )
        keys = complete_workflow_run_keys(pages, expected_sha=run_sha)
        self.assertTrue(has_newer_workflow_run(11, 1, keys))
        self.assertFalse(has_newer_workflow_run(12, 1, keys))
        self.assertTrue(has_newer_workflow_run(12, 1, ((12, 2),)))
        self.assertEqual(
            complete_workflow_run_keys(
                [{"total_count": 2, "workflow_runs": [run_row(12), run_row(13, event="pull_request")]}],
                expected_sha=run_sha,
            ),
            ((12, 1),),
        )
        for malformed_pages in (
            [{"total_count": 2, "workflow_runs": [run_row(12)]}],
            [
                {"total_count": 2, "workflow_runs": [run_row(12)]},
                {"total_count": 3, "workflow_runs": [run_row(11)]},
            ],
            [{"total_count": 2, "workflow_runs": [run_row(12), run_row(12)]}],
            [{"total_count": 1, "workflow_runs": [run_row(12, event="schedule")]}],
        ):
            with self.subTest(malformed_pages=malformed_pages):
                with self.assertRaises(ReconcilerError):
                    complete_workflow_run_keys(malformed_pages, expected_sha=run_sha)

    def test_reconciler_injected_api_is_dev_only_idempotent_and_newer_safe(self) -> None:
        run_sha = "d" * 40
        run_id = 12
        run_attempt = 1
        target_url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}"

        def run_row(
            event: str = "push",
            branch: str = "dev",
            run_number: int = run_id,
            attempt: int = run_attempt,
        ) -> dict[str, object]:
            return {
                "id": run_number,
                "run_attempt": attempt,
                "head_sha": run_sha,
                "head_branch": branch,
                "event": event,
                "name": SECURITY_WORKFLOW_NAME,
                "path": SECURITY_WORKFLOW_PATH,
                "repository": {"full_name": REPOSITORY},
                "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_number}",
            }

        source_run = run_row()
        source_run.update({"status": "completed", "conclusion": "cancelled"})
        event = {"workflow_run": dict(source_run)}

        class FakeClient:
            def __init__(self, runs: list[dict[str, object]], statuses: list[dict[str, object]]) -> None:
                self.runs = runs
                self.statuses = statuses
                self.calls: list[tuple[str, Mapping[str, object] | None]] = []
                self.posts: list[tuple[str, Mapping[str, object]]] = []

            def get_json(self, path: str, query: Mapping[str, object] | None = None) -> object:
                self.calls.append((path, query))
                if path.endswith(f"/actions/runs/{run_id}"):
                    return source_run
                if "/actions/workflows/" in path:
                    page = int((query or {}).get("page", 1)) - 1
                    return self.runs[page]
                if path.endswith(f"/commits/{run_sha}/statuses"):
                    return self.statuses
                raise AssertionError(path)

            def post_status(self, sha: str, payload: Mapping[str, object]) -> object:
                self.posts.append((sha, payload))
                return {"id": 99}

        runs = [{"total_count": 1, "workflow_runs": [run_row()]}]
        client = FakeClient(runs, [])
        result = reconcile_workflow_event(event, client)
        self.assertEqual(result.action, "publish")
        self.assertEqual(client.posts, [(run_sha, result.payload)])
        self.assertEqual(result.payload["state"], "failure")
        self.assertEqual(result.payload["context"], "platform-security-build")
        self.assertEqual(result.payload["description"], FAIL_DESCRIPTION)
        self.assertEqual(result.payload["target_url"], target_url)

        client = FakeClient(runs, [{"id": 99, "context": "platform-security-build", "state": "failure", "target_url": target_url}])
        result = reconcile_workflow_event(event, client)
        self.assertEqual(result.action, "skip")
        self.assertEqual(client.posts, [])

        client = FakeClient(runs, [{"id": 100, "context": "platform-security-build", "state": "success", "target_url": target_url}])
        result = reconcile_workflow_event(event, client)
        self.assertEqual(result.action, "publish")
        self.assertEqual(result.payload["state"], "failure")

        older_target = f"https://github.com/{REPOSITORY}/actions/runs/11/attempts/1"
        client = FakeClient(
            runs,
            [{"id": 100, "context": "platform-security-build", "state": "success", "target_url": older_target}],
        )
        result = reconcile_workflow_event(event, client)
        self.assertEqual(result.action, "publish")
        self.assertEqual(result.payload["state"], "failure")

        newer_runs = [{"total_count": 2, "workflow_runs": [run_row(), run_row(run_number=13)]}]
        client = FakeClient(newer_runs, [])
        result = reconcile_workflow_event(event, client)
        self.assertEqual(result.action, "skip")
        self.assertIn("newer", result.reason)
        self.assertEqual(client.posts, [])

        running_newer = run_row(run_number=13)
        running_newer["status"] = "in_progress"
        client = FakeClient(
            [{"total_count": 2, "workflow_runs": [run_row(), running_newer]}],
            [],
        )
        result = reconcile_workflow_event(event, client)
        self.assertEqual(result.action, "skip")
        self.assertIn("newer", result.reason)

        publisher_event = {
            "repository": {"full_name": REPOSITORY},
            "ref": "refs/heads/dev",
            "after": run_sha,
        }
        client = FakeClient(
            runs,
            [{"id": 101, "context": "platform-security-build", "state": "success", "target_url": target_url}],
        )
        result = publish_workflow_event(publisher_event, source_run, client, "success")
        self.assertEqual(result.action, "skip")
        self.assertEqual(client.posts, [])

        class PublisherRaceClient(FakeClient):
            def __init__(self) -> None:
                super().__init__(runs, [])
                self.workflow_calls = 0

            def get_json(self, path: str, query: Mapping[str, object] | None = None) -> object:
                if "/actions/workflows/" in path:
                    self.workflow_calls += 1
                    if self.workflow_calls == 1:
                        return {"total_count": 1, "workflow_runs": [run_row()]}
                    return {"total_count": 2, "workflow_runs": [run_row(), run_row(run_number=13)]}
                return super().get_json(path, query)

        race_client = PublisherRaceClient()
        result = publish_workflow_event(publisher_event, source_run, race_client, "success")
        self.assertEqual(result.action, "skip")
        self.assertIn("race", result.reason)
        self.assertEqual(race_client.posts, [])

        mismatched_event = {"workflow_run": {**event["workflow_run"], "head_sha": "e" * 40}}
        with self.assertRaises(ReconcilerError):
            reconcile_workflow_event(mismatched_event, FakeClient(runs, []))

        nondev_event = {"workflow_run": {**event["workflow_run"], "event": "workflow_dispatch", "head_branch": "feature"}}
        client = FakeClient(runs, [])
        result = reconcile_workflow_event(nondev_event, client)
        self.assertEqual(result.action, "skip")
        self.assertEqual(client.calls, [])

        pull_request_event = {"workflow_run": {**event["workflow_run"], "event": "pull_request"}}
        client = FakeClient(runs, [])
        result = reconcile_workflow_event(pull_request_event, client)
        self.assertEqual(result.action, "skip")
        self.assertEqual(client.calls, [])

        self.assertFalse(
            status_reconciliation_needed(
                [{"id": 1, "context": "platform-security-build", "state": "failure", "target_url": target_url}],
                target_url=target_url,
                source_key=(run_id, run_attempt),
            )[0]
        )
        with self.assertRaises(ReconcilerError):
            status_reconciliation_needed(
                [{"id": 1, "context": "platform-security-build", "state": "unknown", "target_url": target_url}],
                target_url=target_url,
                source_key=(run_id, run_attempt),
            )

    def test_manifest_is_json_serializable_for_artifact_transport(self) -> None:
        manifest = classify(
            ["platform/apps/platform_web/package.json"],
            event="pull_request",
            target_sha=self.TARGET_SHA,
        )
        encoded = json.dumps(manifest, sort_keys=True)
        self.assertEqual(json.loads(encoded), manifest)

    def test_github_output_rejects_newline_and_control_in_reason(self) -> None:
        manifest = classify(
            ["platform/apps/platform_web/package.json"],
            event="pull_request",
            target_sha=self.TARGET_SHA,
        )
        manifest["reason"] = "safe\nmalicious=true"
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ClassifierError):
                _write_github_output(Path(temporary) / "output", manifest)

    def test_github_output_is_bounded_single_line(self) -> None:
        manifest = classify(
            ["platform/apps/platform_web/package.json"],
            event="pull_request",
            target_sha=self.TARGET_SHA,
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            _write_github_output(output, manifest)
            raw = output.read_text(encoding="utf-8")
            self.assertTrue(raw.endswith("\n"))
            for line in raw.splitlines():
                self.assertNotIn("\r", line)
                self.assertNotIn("\n", line)
                self.assertLessEqual(len(line), 520)

    def test_classifier_zip_rejects_traversal_symlink_bomb_and_duplicate(self) -> None:
        payload = b'{"schema":1}\n'

        def assert_rejected(entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> None:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / "artifact.zip"
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
                        for name, value in entries:
                            handle.writestr(name, value)
                with self.assertRaises(UnsafeZipError):
                    extract_single_manifest(archive, root / "unpacked")

        assert_rejected([("../classifier-manifest.json", payload)])
        symlink = zipfile.ZipInfo("classifier-manifest.json")
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        assert_rejected([(symlink, b"target")])
        assert_rejected([("classifier-manifest.json", b"0" * 200_000)])
        assert_rejected(
            [
                ("classifier-manifest.json", payload),
                ("classifier-manifest.json", payload),
            ]
        )

    def test_classifier_zip_extracts_only_regular_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "artifact.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
                handle.writestr("classifier-manifest.json", b'{"schema":1}\n')
            manifest = extract_single_manifest(archive, root / "unpacked")
            self.assertEqual(manifest.name, "classifier-manifest.json")
            self.assertEqual(manifest.read_bytes(), b'{"schema":1}\n')
            metadata = manifest.stat()
            self.assertEqual(metadata.st_nlink, 1)
            self.assertEqual(metadata.st_mode & 0o777, 0o600)

    def test_classifier_zip_rejects_ratio_special_and_existing_destination(self) -> None:
        payload = b"0" * 256_000
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "artifact.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
                handle.writestr("classifier-manifest.json", payload)
            with self.assertRaises(UnsafeZipError):
                extract_single_manifest(archive, root / "unpacked")

            special_archive = root / "special.zip"
            special = zipfile.ZipInfo("classifier-manifest.json")
            special.external_attr = (stat.S_IFCHR | 0o600) << 16
            with zipfile.ZipFile(special_archive, "w", compression=zipfile.ZIP_STORED) as handle:
                handle.writestr(special, b"{}")
            with self.assertRaises(UnsafeZipError):
                extract_single_manifest(special_archive, root / "special-out")

            existing = root / "existing"
            existing.mkdir()
            with zipfile.ZipFile(root / "valid.zip", "w", compression=zipfile.ZIP_STORED) as handle:
                handle.writestr("classifier-manifest.json", b"{}")
            with self.assertRaises(UnsafeZipError):
                extract_single_manifest(root / "valid.zip", existing)


if __name__ == "__main__":
    unittest.main()
