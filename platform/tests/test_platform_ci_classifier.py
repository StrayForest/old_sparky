from __future__ import annotations

import ast
import base64
from datetime import datetime, timezone
import hashlib
import json
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

import yaml

from tools.platform_ci_classifier import (
    CANDIDATE_PACKAGING_FILES,
    CANDIDATE_PACKAGING_REASON,
    DOCS_ONLY_GATE_IDS,
    DOCS_ONLY_REASON,
    FULL_GATE_IDS,
    OUT_OF_SCOPE_GATE_IDS,
    RECOVERY_BOOTSTRAP_FILES,
    RECOVERY_BOOTSTRAP_REASON,
    RUNTIME_SENSITIVE_FILES,
    STORAGE_OPERATIONS_FILES,
    STORAGE_OPERATIONS_REASON,
    STORAGE_OPERATIONS_TRIGGER_FILES,
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
from tools.platform_production_classifier_artifact import (
    ClassifierArtifactError,
    validate_manifest as validate_production_classifier_manifest,
)
from tools.platform_workflow_provenance import ProvenanceError, validate_security_marker
from tools.platform_deploy_baseline import (
    classify_cumulative_baseline,
    validate_cumulative_reconcile_route,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SECURITY_WORKFLOW = REPO_ROOT / ".github/workflows/platform-security.yml"
AUTO_DEPLOY_WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-autodeploy.yml"
PRODUCTION_WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
STATUS_FINALIZER_WORKFLOW = REPO_ROOT / ".github/workflows/platform-security-status-finalizer.yml"
RECOVERY_BUILD_WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-build.yml"
RECOVERY_PUBLISH_WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-publish.yml"


def _workflow_python_blocks(path: Path, step_name: str) -> list[str]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    matching = [
        step
        for job in workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
        if step.get("name") == step_name
    ]
    if len(matching) != 1 or not isinstance(matching[0].get("run"), str):
        raise AssertionError(f"expected one runnable workflow step: {step_name}")
    blocks = re.findall(r"<<'PY'\n(.*?)\n\s*PY(?:\n|$)", matching[0]["run"], re.DOTALL)
    return [textwrap.dedent(block) for block in blocks]


def _python_set_assignment(source: str, name: str) -> set[str]:
    tree = ast.parse(source)
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, set) and all(isinstance(item, str) for item in value):
                return value
    raise AssertionError(f"missing exact set assignment: {name}")

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
        self, manifest: dict[str, object], *, expect_success: bool = True
    ) -> dict[str, str]:
        """Pass a generated manifest through auto-deploy's shared validator and projection."""

        workflow = AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        marker = (
            '          /usr/bin/python3 - "$manifest_b64" "$GITHUB_OUTPUT" <<\'PY\'\n'
        )
        start = workflow.index(marker) + len(marker)
        end = workflow.index("          PY\n", start)
        contract = textwrap.dedent(workflow[start:end])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = self._write_production_classifier_archive(root, manifest)
            output_path = root / "github-output"
            validated = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(REPO_ROOT / "platform/tools/platform_production_classifier_artifact.py"),
                    "manifest",
                    str(archive_path),
                    "--target-sha",
                    str(manifest["target_sha"]),
                    "--allow-docs-only",
                    "--allow-candidate-packaging",
                    "--allow-out-of-scope",
                    "--emit-manifest-base64",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if expect_success:
                self.assertEqual(validated.returncode, 0, validated.stderr)
            else:
                self.assertNotEqual(validated.returncode, 0)
                return {}
            completed = subprocess.run(
                [sys.executable, "-", validated.stdout.strip(), str(output_path)],
                input=contract,
                cwd=REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            if expect_success:
                self.assertEqual(completed.returncode, 0, completed.stderr)
            else:
                self.assertNotEqual(completed.returncode, 0)
                return {}
            output: dict[str, str] = {}
            for line in output_path.read_text(encoding="utf-8").splitlines():
                key, value = line.split("=", 1)
                output[key] = value
            return output

    def _write_production_classifier_archive(
        self, root: Path, manifest: dict[str, object]
    ) -> Path:
        archive_path = root / "classifier.zip"
        member = zipfile.ZipInfo("classifier-manifest.json")
        member.create_system = 3
        member.external_attr = (stat.S_IFREG | 0o600) << 16
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                member,
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
        return archive_path

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
        self.assertEqual(route["route_recovery_bootstrap_only"], "false")
        self.assertEqual(route["route_fallback"], str(push_manifest["fallback"]).lower())
        self.assertEqual(route["route_digest"], push_manifest["digest"])
        auto = AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            'if [[ "$ROUTE_DEPLOYABLE" != "true"',
            auto,
        )
        self.assertIn(
            '"$ROUTE_RECOVERY_BOOTSTRAP_ONLY" == "true"',
            auto,
        )
        self.assertIn(
            'echo "deploy=false" >> "$GITHUB_OUTPUT"',
            auto,
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

        retained_abort_paths = [
            ".github/workflows/platform-production-retained-load-abort.yml",
            "platform/tools/platform_abort_retained_load.py",
            "platform/tools/platform_ci_classifier.py",
            "platform/tests/test_platform_secret_artifact_boundary.py",
            "platform/tests/test_platform_workflow_provenance.py",
        ]
        self.assertTrue(set(retained_abort_paths) <= RECOVERY_BOOTSTRAP_FILES)
        retained_abort_route = classify(
            retained_abort_paths,
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(retained_abort_route["class"], "full")
        self.assertEqual(retained_abort_route["reason"], RECOVERY_BOOTSTRAP_REASON)
        self.assertFalse(retained_abort_route["deployable"])
        self.assertFalse(retained_abort_route["fallback"])

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

        for path in (
            "platform/alembic/env.py",
            "platform/alembic/versions/20260913_0053_tournament_list_read_model_retry.py",
            "platform/tools/platform_migration_scenario.py",
            "platform/tools/platform_migration_support.py",
            "platform/tools/platform_verify.py",
            "platform/tests/test_platform_verification_contract.py",
            ".github/workflows/platform-security.yml",
        ):
            with self.subTest(migration_route=path):
                manifest = classify(
                    [path],
                    event="push",
                    target_sha=self.TARGET_SHA,
                    branch="dev",
                )
                self.assertEqual(manifest["class"], "full")
                self.assertEqual(tuple(manifest["expected_gates"]), FULL_GATE_IDS)
                self.assertFalse(manifest["fallback"])

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
        settings_path = "platform/python_packages/platform_infra/config.py"
        self.assertIn(runtime_path, RUNTIME_SENSITIVE_FILES)
        self.assertIn(settings_path, RUNTIME_SENSITIVE_FILES)
        for path in (runtime_path, settings_path):
            with self.subTest(path=path):
                runtime = classify(
                    [path],
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
        for job in ("release-runtime", "release-runtime-real"):
            match = re.search(
                rf"(?ms)^  {re.escape(job)}:\n(?P<block>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
                workflow,
            )
            self.assertIsNotNone(match)
            assert match is not None
            block = match.group("block")
            for required in (
                "github.event_name == 'push'",
                "github.ref == 'refs/heads/dev'",
                "needs.classifier.outputs.class == 'full'",
                "needs.classifier.outputs.deployable == 'true'",
            ):
                self.assertIn(required, block)
        self.assertNotIn("schedule:", workflow)

    def test_cancel_safe_status_finalizer_overwrites_every_terminal_conclusion(self) -> None:
        workflow = STATUS_FINALIZER_WORKFLOW.read_text(encoding="utf-8")
        security = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        finalizer = yaml.safe_load(workflow)
        finalize_job = finalizer["jobs"]["finalize-status"]
        self.assertIn("workflow_run:", workflow)
        self.assertIn("workflows: [Platform security and build]", workflow)
        self.assertIn("types: [completed]", workflow)
        self.assertIn("always() &&", finalize_job["if"])
        self.assertIn("github.event_name != 'workflow_run'", finalize_job["if"])
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertNotIn("--location", workflow)
        self.assertIn("permissions:\n      actions: read\n      statuses: write", workflow)
        self.assertNotIn("actions/checkout", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertIn("TARGET_SHA: ${{ github.event.workflow_run.head_sha }}", workflow)
        self.assertEqual(
            finalize_job["steps"][1]["env"]["TARGET_SHA"],
            "${{ steps.status.outputs.target_sha }}",
        )
        self.assertIn("statuses/${TARGET_SHA}", finalize_job["steps"][1]["run"])
        self.assertIn("attempt_url=", workflow)
        self.assertIn("SOURCE_RUN_URL", workflow)
        self.assertIn("/attempts/${SOURCE_RUN_ATTEMPT}", workflow)
        self.assertNotIn("commits/${TARGET_SHA}/statuses?per_page=100", workflow)
        self.assertNotIn("preserve_success", workflow)
        self.assertNotIn("updated_at", workflow)
        self.assertNotIn("status_rows", workflow)
        self.assertNotIn("pagination", workflow)
        self.assertIn('state = "success" if conclusion == "success" else "failure"', workflow)
        self.assertIn('"state": state', workflow)
        self.assertIn('"target_url": expected_base_url + "/attempts/" + attempt', workflow)
        self.assertIn('status_context = "platform-security-build"', workflow)
        self.assertIn('status_context = "platform-baseline-runtime"', workflow)
        self.assertIn('raise SystemExit("workflow-dispatch run mode is unavailable; no status will be published")', workflow)
        self.assertIn("SOURCE_RUN_URL: ${{ github.event.workflow_run.html_url }}", workflow)
        self.assertLess(
            workflow.index("name: Platform security status finalizer"),
            workflow.index("TARGET_SHA: ${{ github.event.workflow_run.head_sha }}"),
        )

        pending_at = security.index("--data", security.index("Mark platform security build pending"))
        first_gate_at = security.index("  backend-static:")
        final_at = security.index("  status-final:")
        self.assertLess(pending_at, first_gate_at)
        self.assertLess(first_gate_at, final_at)
        self.assertIn("needs: [classifier, baseline-runtime-guard, status-start", security)

    def test_internal_baseline_runtime_lane_is_closed_and_uses_a_distinct_status(self) -> None:
        workflow = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        production = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        finalizer = STATUS_FINALIZER_WORKFLOW.read_text(encoding="utf-8")
        for input_name in (
            "proof_mode", "target_sha", "source_security_run_id",
            "source_security_run_attempt", "autodeploy_run_id",
            "autodeploy_run_attempt", "production_deploy_run_id",
            "production_deploy_run_attempt",
        ):
            self.assertIn(f"      {input_name}:", workflow)
        self.assertIn("default: standard", workflow)
        self.assertIn("          - baseline-runtime", workflow)
        self.assertIn("platform-baseline-runtime-v1:", workflow)
        self.assertNotIn("curl --fail-with-body --silent --show-error --location", workflow)
        self.assertIn("|| 'Platform security and build'", workflow)
        self.assertNotIn("platform-security-standard-v1", workflow)
        self.assertNotIn("platform-security-push-v1", workflow)
        self.assertIn('test "$TARGET_SHA" = "$GITHUB_SHA"', workflow)
        self.assertIn('test "$GITHUB_REF" = "refs/heads/dev"', workflow)
        self.assertIn('"baseline proof target is not the exact current dev head"', workflow)
        self.assertIn('"Platform production deploy mode=baseline-reconcile target={target} source={security_id}.{security_attempt} auto={auto_id}.{auto_attempt}"', workflow)
        self.assertIn('"Dispatch exact-target baseline runtime proof"', workflow)
        self.assertIn('parent.get("status") != "in_progress"', workflow)
        self.assertIn('needs.classifier.outputs.class == \'full\' || (github.event_name == \'workflow_dispatch\' && inputs.proof_mode == \'baseline-runtime\')', workflow)
        self.assertIn('inputs.proof_mode == \'baseline-runtime\' && needs.baseline-runtime-guard.result == \'success\'', workflow)
        self.assertIn("Recheck exact current dev SHA immediately before runtime build", workflow)
        self.assertIn('"required_gates": [', workflow)
        self.assertIn('name: platform-baseline-runtime-receipt-${{ github.run_id }}-${{ github.run_attempt }}', workflow)
        self.assertIn('name: platform-baseline-runtime', workflow)
        self.assertIn('context=platform-baseline-runtime', workflow)
        self.assertIn('baseline-runtime) status_context=platform-baseline-runtime', workflow)
        self.assertIn('status_context = "platform-baseline-runtime"', finalizer)
        self.assertIn('expected_run_path = ".github/workflows/platform-security.yml"', finalizer)
        self.assertNotIn('platform-security.yml@', finalizer)
        self.assertIn('run.get("name") != display_title', finalizer)
        self.assertIn('run.get("name") != "Platform security and build"', finalizer)
        self.assertIn('display_title == "Platform security and build"', finalizer)
        self.assertIn("inputs.mode == 'baseline-reconcile'", production)
        self.assertIn("|| 'Platform production deploy'", production)
        self.assertIn('is_baseline_candidate = display_title.startswith("platform-baseline-runtime-v1:")', finalizer)
        self.assertIn('raise SystemExit("workflow-dispatch run mode is unavailable; no status will be published")', finalizer)
        self.assertIn('required = {', finalizer)
        self.assertIn('"Trusted dev immutable release runtime"', finalizer)
        self.assertIn('"success", "failure", "cancelled", "skipped", "timed_out"', finalizer)
        self.assertIn('if status_context == "platform-baseline-runtime" and conclusion == "success":', finalizer)
        self.assertNotIn('context = "platform-security-build"\n                  if is_baseline_candidate', finalizer)

    def test_baseline_proof_terminal_writer_is_success_only_and_separate_from_workflow_run(self) -> None:
        security = yaml.safe_load(SECURITY_WORKFLOW.read_text(encoding="utf-8"))
        finalizer = yaml.safe_load(STATUS_FINALIZER_WORKFLOW.read_text(encoding="utf-8"))
        security_jobs = security["jobs"]
        dispatch = security_jobs["dispatch-baseline-runtime-finalizer"]
        self.assertEqual(dispatch["permissions"], {"actions": "write"})
        self.assertEqual(dispatch["needs"], ["status-final"])
        self.assertIn("github.event_name == 'workflow_dispatch'", dispatch["if"])
        self.assertIn("inputs.proof_mode == 'baseline-runtime'", dispatch["if"])
        self.assertIn("needs.status-final.result == 'success'", dispatch["if"])
        dispatch_step = dispatch["steps"][0]
        self.assertEqual(dispatch_step["name"], "Dispatch the exact completed proof attempt to the terminal status finalizer")
        self.assertEqual(dispatch_step["env"]["PROOF_MODE"], "${{ inputs.proof_mode }}")
        self.assertEqual(dispatch_step["env"]["PROOF_RUN_ID"], "${{ github.run_id }}")
        self.assertEqual(dispatch_step["env"]["PROOF_RUN_ATTEMPT"], "${{ github.run_attempt }}")
        self.assertIn("test \"$GITHUB_REF\" = refs/heads/dev", dispatch_step["run"])
        self.assertIn("platform-security-status-finalizer.yml/dispatches", dispatch_step["run"])
        self.assertIn('"proof_run_id": run_id', dispatch_step["run"])
        self.assertIn('"proof_run_attempt": attempt', dispatch_step["run"])
        self.assertIn('"ref": "dev"', dispatch_step["run"])

        events = finalizer.get("on", finalizer.get(True, {}))
        self.assertEqual(events["workflow_run"]["types"], ["completed"])
        self.assertEqual(
            set(events["workflow_dispatch"]["inputs"]),
            {"proof_run_id", "proof_run_attempt"},
        )
        for input_spec in events["workflow_dispatch"]["inputs"].values():
            self.assertIs(input_spec["required"], True)
            self.assertEqual(input_spec["type"], "string")
        finalize_job = finalizer["jobs"]["finalize-status"]
        self.assertIn("startsWith(github.event.workflow_run.display_title, 'platform-baseline-runtime-v1:')", finalize_job["if"])
        self.assertIn("github.event.workflow_run.event == 'workflow_dispatch'", finalize_job["if"])
        self.assertEqual(
            finalize_job["concurrency"]["group"],
            "platform-status-finalizer-${{ github.event_name == 'workflow_run' && github.event.workflow_run.id || inputs.proof_run_id }}-${{ github.event_name == 'workflow_run' && github.event.workflow_run.run_attempt || inputs.proof_run_attempt }}",
        )
        self.assertEqual(finalize_job["permissions"], {"actions": "read", "statuses": "write"})
        validate_step = finalize_job["steps"][0]["run"]
        self.assertIn("deadline=$((SECONDS + 180))", validate_step)
        self.assertIn("while (( SECONDS < deadline ))", validate_step)
        self.assertIn('attempts/${SOURCE_RUN_ATTEMPT}', validate_step)
        self.assertIn('"${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/actions/runs/${SOURCE_RUN_ID}"', validate_step)
        self.assertIn('current.get("run_attempt") != int(attempt)', validate_step)
        self.assertIn('run.get("conclusion") != "success"', validate_step)
        publish_step = finalize_job["steps"][1]
        self.assertEqual(
            publish_step["env"]["TARGET_SHA"],
            "${{ steps.status.outputs.target_sha }}",
        )
        self.assertIn("${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/statuses/${TARGET_SHA}", publish_step["run"])

    def test_baseline_source_status_must_be_latest_for_the_exact_attempt(self) -> None:
        workflow = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        guard = workflow.split("  baseline-runtime-guard:", 1)[1].split(
            "  status-start:", 1
        )[0]
        script_match = re.search(
            r"(?ms)^\s+/usr/bin/python3 - .*?<<'PY'\n(?P<script>.*?)^\s+PY$",
            guard,
        )
        self.assertIsNotNone(script_match)
        assert script_match is not None
        guard_script = ast.parse(textwrap.dedent(script_match.group("script")))
        validator = next(
            node
            for node in guard_script.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "validate_source_security_status"
        )
        imports = [
            node
            for node in guard_script.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        test_module = ast.Module(body=[*imports, validator], type_ignores=[])
        namespace: dict[str, object] = {}
        exec(compile(ast.fix_missing_locations(test_module), "baseline-status", "exec"), namespace)
        validate = namespace["validate_source_security_status"]

        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc).replace(microsecond=0)
        expected_url = "https://github.com/StrayForest/old_sparky/actions/runs/123/attempts/2"
        creator = {"login": "github-actions[bot]", "type": "Bot", "id": 41898282}
        source_status = {
            "id": 1,
            "context": "platform-security-build",
            "state": "success",
            "target_url": expected_url,
            "creator": creator,
            "created_at": (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_at": (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        validate([source_status], expected_url)

        later_dispatch_status = {
            **source_status,
            "id": 2,
            "target_url": "https://github.com/StrayForest/old_sparky/actions/runs/456/attempts/1",
            "created_at": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_at": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        with self.assertRaisesRegex(SystemExit, "superseded"):
            validate([later_dispatch_status, source_status], expected_url)

    def test_baseline_status_start_rejects_malformed_inputs_before_posting_pending(self) -> None:
        workflow = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        status_start = workflow.split("  status-start:", 1)[1].split(
            "  backend-static:", 1
        )[0]
        script_match = re.search(
            r"(?ms)^[ ]{8}run: \|\n(?P<script>.*)$",
            status_start,
        )
        self.assertIsNotNone(script_match)
        assert script_match is not None
        script = textwrap.dedent(script_match.group("script"))

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            marker = temp_path / "status-posted"
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                "#!/bin/sh\nprintf called > \"$STATUS_WRITE_MARKER\"\nexit 0\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            base_environment = os.environ.copy()
            base_environment.update(
                {
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "STATUS_WRITE_MARKER": str(marker),
                    "EVENT_NAME": "workflow_dispatch",
                    "PROOF_MODE": "baseline-runtime",
                    "TESTED_SHA": "a" * 40,
                    "TARGET_SHA_INPUT": "a" * 40,
                    "GITHUB_REF": "refs/heads/dev",
                    "SOURCE_SECURITY_RUN_ID": "1001",
                    "SOURCE_SECURITY_RUN_ATTEMPT": "1",
                    "AUTODEPLOY_RUN_ID": "1002",
                    "AUTODEPLOY_RUN_ATTEMPT": "1",
                    "PRODUCTION_DEPLOY_RUN_ID": "1003",
                    "PRODUCTION_DEPLOY_RUN_ATTEMPT": "1",
                    "GITHUB_SERVER_URL": "https://github.com",
                    "GITHUB_REPOSITORY": "StrayForest/old_sparky",
                    "GITHUB_RUN_ID": "1004",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_API_URL": "https://api.github.com",
                    "GH_TOKEN": "test-token",
                }
            )
            invalid_cases = (
                {"TARGET_SHA_INPUT": "b" * 40},
                {"SOURCE_SECURITY_RUN_ID": "0"},
                {"AUTODEPLOY_RUN_ATTEMPT": "01"},
                {"GITHUB_REF": "refs/heads/feature"},
            )
            for overrides in invalid_cases:
                with self.subTest(overrides=overrides):
                    marker.unlink(missing_ok=True)
                    environment = {**base_environment, **overrides}
                    result = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", script],
                        check=False,
                        capture_output=True,
                        text=True,
                        env=environment,
                        timeout=5,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(marker.exists(), result.stdout + result.stderr)

    def test_baseline_status_final_requires_full_gates_and_both_runtime_proofs(self) -> None:
        workflow = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        status_final = workflow.split("  status-final:", 1)[1]
        script_match = re.search(
            r"(?ms)^\s+/usr/bin/python3 - <<'PY'\n(?P<script>.*?)^\s+PY$",
            status_final,
        )
        self.assertIsNotNone(script_match)
        assert script_match is not None
        status_script = textwrap.dedent(script_match.group("script"))
        expected_full = list(FULL_GATE_IDS)
        environment = os.environ.copy()
        environment.update(
            {
                "CLASSIFIER_RESULT": "success",
                "EVENT_NAME": "workflow_dispatch",
                "PROOF_MODE": "baseline-runtime",
                "BASELINE_GUARD_RESULT": "success",
                "HOST_TOOLS_PIN_PREFLIGHT_RESULT": "success",
                "TARGET_SHA_INPUT": self.TARGET_SHA,
                "PROOF_RUN_ID": "12345",
                "PROOF_RUN_ATTEMPT": "1",
                "ROUTE_EVENT": "workflow_dispatch",
                "ROUTE_CLASS": "docs-only",
                "ROUTE_DEPLOYABLE": "false",
                "ROUTE_FALLBACK": "false",
                "ROUTE_RUNTIME_SENSITIVE": "false",
                "ROUTE_TARGET_SHA": self.TARGET_SHA,
                "ROUTE_DIGEST": "b" * 64,
                "ROUTE_REASON": "trusted docs route",
                "EXPECTED_GATES": json.dumps(["docs", "verification-contract"]),
                "TESTED_SHA": self.TARGET_SHA,
                "STATUS_START_RESULT": "success",
                "WORKFLOW_REF": "refs/heads/dev",
                "BACKEND_RESULT": "success",
                "PYTHON_QUALITY_RESULT": "success",
                "SECURITY_RESULT": "success",
                "WEB_QUALITY_RESULT": "success",
                "WEB_HERMETIC_RESULT": "success",
                "DOCS_RESULT": "success",
                "MIGRATION_RESULT": "success",
                "VERIFICATION_CONTRACT_RESULT": "success",
                "RELEASE_RUNTIME_RESULT": "success",
                "RELEASE_RUNTIME_REAL_RESULT": "success",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            summary_path = Path(directory) / "summary.json"
            environment["SUMMARY_PATH"] = str(summary_path)
            completed = subprocess.run(
                ["/usr/bin/python3"],
                input=status_script,
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "true")
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["expected_gates"], expected_full)
            self.assertTrue(summary["requires_release_runtime"])
            self.assertTrue(summary["requires_real_release_runtime"])

            for missing_gate, gate in (
                ("release-runtime", "fixture"),
                ("release-runtime-real", "immutable"),
            ):
                with self.subTest(missing_gate=missing_gate):
                    failed_environment = dict(environment)
                    failed_environment[
                        "RELEASE_RUNTIME_RESULT"
                        if gate == "fixture"
                        else "RELEASE_RUNTIME_REAL_RESULT"
                    ] = "skipped"
                    failed_environment["SUMMARY_PATH"] = str(
                        Path(directory) / f"missing-{gate}.json"
                    )
                    failed = subprocess.run(
                        ["/usr/bin/python3"],
                        input=status_script,
                        text=True,
                        capture_output=True,
                        env=failed_environment,
                        check=False,
                    )
                    self.assertEqual(failed.returncode, 0, failed.stderr)
                    self.assertEqual(failed.stdout.strip(), "false")

            failed_preflight_environment = dict(environment)
            failed_preflight_environment["HOST_TOOLS_PIN_PREFLIGHT_RESULT"] = "failure"
            failed_preflight_environment["SUMMARY_PATH"] = str(
                Path(directory) / "failed-host-tools-preflight.json"
            )
            failed_preflight = subprocess.run(
                ["/usr/bin/python3"],
                input=status_script,
                text=True,
                capture_output=True,
                env=failed_preflight_environment,
                check=False,
            )
            self.assertEqual(failed_preflight.returncode, 0, failed_preflight.stderr)
            self.assertEqual(failed_preflight.stdout.strip(), "false")

            invalid_environment = dict(environment)
            invalid_environment["PROOF_MODE"] = "unrecognized"
            invalid_environment["SUMMARY_PATH"] = str(Path(directory) / "invalid.json")
            invalid = subprocess.run(
                ["/usr/bin/python3"],
                input=status_script,
                text=True,
                capture_output=True,
                env=invalid_environment,
                check=False,
            )
            self.assertEqual(invalid.returncode, 0, invalid.stderr)
            self.assertEqual(invalid.stdout.strip(), "false")

    def test_baseline_status_final_leaves_terminal_write_to_workflow_run_finalizer(self) -> None:
        workflow = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        status_final = workflow.split("  status-final:", 1)[1]
        script_match = re.search(
            r"(?ms)^[ ]{8}run: \|\n(?P<script>.*?)(?=^[ ]{6}- name:|\Z)",
            status_final,
        )
        self.assertIsNotNone(script_match)
        assert script_match is not None
        script = textwrap.dedent(script_match.group("script"))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            marker = root / "status-posted"
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$STATUS_WRITE_MARKER\"\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            base_environment = os.environ.copy()
            base_environment.update(
                {
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "STATUS_WRITE_MARKER": str(marker),
                    "CLASSIFIER_RESULT": "success",
                    "EVENT_NAME": "workflow_dispatch",
                    "PROOF_MODE": "baseline-runtime",
                    "BASELINE_GUARD_RESULT": "success",
                    "HOST_TOOLS_PIN_PREFLIGHT_RESULT": "success",
                    "TARGET_SHA_INPUT": self.TARGET_SHA,
                    "PROOF_RUN_ID": "12345",
                    "PROOF_RUN_ATTEMPT": "1",
                    "ROUTE_EVENT": "workflow_dispatch",
                    "ROUTE_CLASS": "docs-only",
                    "ROUTE_DEPLOYABLE": "false",
                    "ROUTE_FALLBACK": "false",
                    "ROUTE_RUNTIME_SENSITIVE": "false",
                    "ROUTE_TARGET_SHA": self.TARGET_SHA,
                    "ROUTE_DIGEST": "b" * 64,
                    "ROUTE_REASON": "trusted docs route",
                    "EXPECTED_GATES": json.dumps(["docs", "verification-contract"]),
                    "TESTED_SHA": self.TARGET_SHA,
                    "STATUS_START_RESULT": "success",
                    "WORKFLOW_REF": "refs/heads/dev",
                    "BACKEND_RESULT": "success",
                    "PYTHON_QUALITY_RESULT": "success",
                    "SECURITY_RESULT": "success",
                    "WEB_QUALITY_RESULT": "success",
                    "WEB_HERMETIC_RESULT": "success",
                    "DOCS_RESULT": "success",
                    "MIGRATION_RESULT": "success",
                    "VERIFICATION_CONTRACT_RESULT": "success",
                    "RELEASE_RUNTIME_RESULT": "success",
                    "RELEASE_RUNTIME_REAL_RESULT": "success",
                    "SUMMARY_PATH": str(root / "summary.json"),
                    "GH_TOKEN": "test-token",
                    "GITHUB_SERVER_URL": "https://github.com",
                    "GITHUB_REPOSITORY": "StrayForest/old_sparky",
                    "GITHUB_WORKSPACE": str(root),
                    "GITHUB_RUN_ID": "12345",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_API_URL": "https://api.github.com",
                }
            )
            baseline = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", script],
                check=False,
                capture_output=True,
                text=True,
                env=base_environment,
                timeout=10,
            )
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            self.assertFalse(marker.exists(), baseline.stdout + baseline.stderr)

            standard_environment = dict(base_environment)
            standard_environment.update(
                {
                    "PROOF_MODE": "standard",
                    "RELEASE_RUNTIME_RESULT": "skipped",
                    "RELEASE_RUNTIME_REAL_RESULT": "skipped",
                    "SUMMARY_PATH": str(root / "standard-summary.json"),
                }
            )
            standard = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", script],
                check=False,
                capture_output=True,
                text=True,
                env=standard_environment,
                timeout=10,
            )
            self.assertEqual(standard.returncode, 0, standard.stderr)
            self.assertTrue(marker.exists())
            self.assertEqual(marker.read_text(encoding="utf-8").count("\n"), 1)
            self.assertIn('"context": "platform-security-build"', marker.read_text(encoding="utf-8"))

    def test_successful_reduced_routes_keep_canonical_status_and_noop_autodeploy(self) -> None:
        security = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        auto = AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('description="Platform security and build passed"', security)
        self.assertNotIn("Platform security passed; route=", security)
        self.assertNotIn("fail-closed fallback route", security)
        self.assertIn('"$ROUTE_DEPLOYABLE" != "true"', auto)
        self.assertIn('echo "deploy=false" >> "$GITHUB_OUTPUT"', auto)
        self.assertIn("Dispatch production deployment or baseline reconciliation", auto)
        self.assertIn("steps.gate.outputs.deploy == 'true'", auto)
        self.assertIn("platform_production_classifier_artifact.py manifest", auto)
        self.assertIn('"$ROUTE_CLASS" == "docs-only"', auto)

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
                    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
                        "path": SECURITY_WORKFLOW_PATH,
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

    def test_autodeploy_reconciles_only_exact_full_nondeployable_families(self) -> None:
        auto = AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("platform_production_classifier_artifact.py manifest", auto)
        self.assertIn("_is_recovery_bootstrap_only", auto)
        self.assertIn("_is_candidate_packaging_only", auto)
        self.assertIn("_is_storage_operations_only", auto)

        bootstrap_path = sorted(RECOVERY_BOOTSTRAP_FILES)[0]
        special_route = classify(
            [bootstrap_path],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(special_route["class"], "full")
        self.assertFalse(special_route["deployable"])
        self.assertFalse(special_route["fallback"])
        self.assertEqual(special_route["reason"], RECOVERY_BOOTSTRAP_REASON)
        special_outputs = self._run_auto_deploy_manifest_contract(special_route)
        self.assertEqual(special_outputs["route_class"], "full")
        self.assertEqual(special_outputs["route_deployable"], "false")
        self.assertEqual(special_outputs["route_recovery_bootstrap_only"], "true")
        self.assertEqual(special_outputs["route_candidate_packaging_only"], "false")
        self.assertEqual(special_outputs["route_docs_only"], "false")

        host_tools_adr = "platform/docs/adr/production-host-tools-provisioning.md"
        adr_push = classify(
            [host_tools_adr],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(adr_push["class"], "full")
        self.assertEqual(adr_push["expected_gates"], list(FULL_GATE_IDS))
        self.assertEqual(adr_push["reason"], RECOVERY_BOOTSTRAP_REASON)
        self.assertFalse(adr_push["fallback"])
        self.assertFalse(adr_push["runtime_sensitive"])
        self.assertFalse(adr_push["deployable"])
        adr_outputs = self._run_auto_deploy_manifest_contract(adr_push)
        self.assertEqual(adr_outputs["route_class"], "full")
        self.assertEqual(adr_outputs["route_deployable"], "false")
        self.assertEqual(adr_outputs["route_recovery_bootstrap_only"], "true")
        self.assertEqual(adr_outputs["route_candidate_packaging_only"], "false")
        self.assertEqual(adr_outputs["route_docs_only"], "false")

        adr_pr = classify(
            [host_tools_adr],
            event="pull_request",
            target_sha=self.TARGET_SHA,
            branch="feature",
        )
        self.assertEqual(adr_pr["class"], "full")
        self.assertEqual(adr_pr["expected_gates"], list(FULL_GATE_IDS))
        self.assertFalse(adr_pr["fallback"])
        self.assertFalse(adr_pr["deployable"])

        overlap_route = classify(
            [".github/workflows/platform-production-autodeploy.yml"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(overlap_route["reason"], RECOVERY_BOOTSTRAP_REASON)
        overlap_outputs = self._run_auto_deploy_manifest_contract(overlap_route)
        self.assertEqual(overlap_outputs["route_recovery_bootstrap_only"], "true")
        self.assertEqual(overlap_outputs["route_candidate_packaging_only"], "false")

        candidate_route = classify(
            sorted(CANDIDATE_PACKAGING_FILES),
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(candidate_route["reason"], CANDIDATE_PACKAGING_REASON)
        self.assertFalse(candidate_route["deployable"])
        self.assertFalse(candidate_route["runtime_sensitive"])
        candidate_outputs = self._run_auto_deploy_manifest_contract(candidate_route)
        self.assertEqual(candidate_outputs["route_recovery_bootstrap_only"], "false")
        self.assertEqual(candidate_outputs["route_candidate_packaging_only"], "true")
        self.assertEqual(candidate_outputs["route_docs_only"], "false")
        malformed_candidate = dict(candidate_route)
        malformed_candidate["runtime_sensitive"] = True
        malformed_candidate["digest"] = manifest_digest(malformed_candidate)
        self._run_auto_deploy_manifest_contract(malformed_candidate, expect_success=False)
        unknown_non_deployable = dict(candidate_route)
        unknown_non_deployable["reason"] = "unrecognized non-deployable reason"
        unknown_non_deployable["digest"] = manifest_digest(unknown_non_deployable)
        self._run_auto_deploy_manifest_contract(unknown_non_deployable, expect_success=False)

        docs_only = classify(
            ["platform/docs/deployment-runbook.md"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(docs_only["class"], "docs-only")
        self.assertEqual(docs_only["expected_gates"], list(DOCS_ONLY_GATE_IDS))
        self.assertFalse(docs_only["deployable"])
        docs_outputs = self._run_auto_deploy_manifest_contract(docs_only)
        self.assertEqual(docs_outputs["route_recovery_bootstrap_only"], "false")
        self.assertEqual(docs_outputs["route_candidate_packaging_only"], "false")
        self.assertEqual(docs_outputs["route_docs_only"], "true")
        self.assertIn('"$ROUTE_DOCS_ONLY" == "true"', auto)
        self.assertIn("--allow-docs-only", PRODUCTION_WORKFLOW.read_text(encoding="utf-8"))
        self.assertIn("--allow-candidate-packaging", auto)
        recovery_build = RECOVERY_BUILD_WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(recovery_build.count("--allow-candidate-packaging"), 1)
        route_validation = next(
            step["run"]
            for step in yaml.safe_load(recovery_build)["jobs"]["build"]["steps"]
            if step.get("name") == "Download and validate exact security route artifact"
        )
        self.assertIn("--allow-candidate-packaging", route_validation)
        bundle_step = next(
            step["run"]
            for step in yaml.safe_load(recovery_build)["jobs"]["build"]["steps"]
            if step.get("name") == "Build and validate deterministic recovery bundle"
        )
        self.assertNotIn("--allow-candidate-packaging", bundle_step)
        self.assertIn("env -u GH_TOKEN -u GITHUB_TOKEN /usr/bin/python3 -I -B", auto)

        self.assertIn("route_recovery_bootstrap_only", auto)
        self.assertIn("route_candidate_packaging_only", auto)
        self.assertIn(
            '"$ROUTE_CANDIDATE_PACKAGING_ONLY" == "true"',
            auto,
        )
        production = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(
            production.count("manifest_args+=(--require-reconcile-source --allow-docs-only)"),
            1,
        )
        self.assertEqual(production.count("manifest_args+=(--require-reconcile-source)"), 1)
        self.assertIn("dispatch_mode=baseline-reconcile", auto)
        self.assertIn('case "$DISPATCH_MODE" in', auto)
        self.assertIn('mode": sys.argv[3]', auto)
        self.assertIn('autodeploy_run_id": sys.argv[4]', auto)
        self.assertIn('autodeploy_run_attempt": sys.argv[5]', auto)
        self.assertIn('AUTODEPLOY_RUN_ID: ${{ github.run_id }}', auto)
        self.assertIn('AUTODEPLOY_RUN_ATTEMPT: ${{ github.run_attempt }}', auto)
        self.assertIn('"ref": "dev"', auto)
        self.assertIn('"web_compression": "enabled"', auto)
        self.assertIn('"runtime_profile": "authenticated-read-admission-24x8"', auto)
        self.assertIn("platform_autodeploy_freshness_guard.py", auto)
        self.assertIn('--expected-sha "$TARGET_SHA"', auto)

    def test_live_qa_runtime_installer_change_is_deployable_and_runtime_sensitive(self) -> None:
        installer = "platform/tools/platform_live_qa_runtime_install.py"
        live_engine_preflight = (
            "platform/apps/platform_web/tests/support/live-engine-preflight.cjs"
        )
        live_qa_guard = "platform/tools/platform_live_qa_guard.py"
        live_qa_runtime_builder = "platform/tools/platform_build_live_qa_runtime.py"
        live_qa_guard_test = "platform/tests/test_platform_live_qa_guard.py"
        live_qa_wrapper_test = "platform/tests/test_platform_live_qa_wrappers.py"
        launch_helper = "platform/tools/platform_live_launch_trusted.sh"
        dispatcher = "platform/tools/platform_workflow_remote_dispatch.py"
        self.assertNotIn(installer, RECOVERY_BOOTSTRAP_FILES)
        self.assertNotIn(live_qa_guard, RECOVERY_BOOTSTRAP_FILES)
        self.assertNotIn(live_qa_runtime_builder, RECOVERY_BOOTSTRAP_FILES)
        self.assertNotIn(launch_helper, RECOVERY_BOOTSTRAP_FILES)
        self.assertNotIn(dispatcher, RECOVERY_BOOTSTRAP_FILES)
        self.assertIn(live_qa_guard, RUNTIME_SENSITIVE_FILES)
        self.assertIn(live_qa_runtime_builder, RUNTIME_SENSITIVE_FILES)
        self.assertIn(live_engine_preflight, RUNTIME_SENSITIVE_FILES)
        self.assertIn(launch_helper, RUNTIME_SENSITIVE_FILES)
        self.assertIn(dispatcher, RUNTIME_SENSITIVE_FILES)

        manifest = classify(
            [installer],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )

        self.assertEqual(manifest["class"], "full")
        self.assertTrue(manifest["deployable"])
        self.assertFalse(manifest["fallback"])
        self.assertTrue(manifest["runtime_sensitive"])

        helper_manifest = classify(
            [live_engine_preflight],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(helper_manifest["class"], "full")
        self.assertEqual(helper_manifest["expected_gates"], list(FULL_GATE_IDS))
        self.assertFalse(helper_manifest["fallback"])
        self.assertTrue(helper_manifest["runtime_sensitive"])
        self.assertTrue(helper_manifest["deployable"])

        helper_pr = classify(
            [live_engine_preflight],
            event="pull_request",
            target_sha=self.TARGET_SHA,
            branch=None,
        )
        self.assertEqual(helper_pr["class"], "full")
        self.assertEqual(helper_pr["expected_gates"], list(FULL_GATE_IDS))
        self.assertFalse(helper_pr["fallback"])
        self.assertTrue(helper_pr["runtime_sensitive"])
        self.assertFalse(helper_pr["deployable"])

        current_fix_paths = [
            live_qa_guard,
            live_qa_guard_test,
            live_qa_wrapper_test,
        ]
        for event, expected_deployable in (("push", True), ("pull_request", False)):
            current_fix = classify(
                current_fix_paths,
                event=event,
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            self.assertEqual(current_fix["class"], "full")
            self.assertFalse(current_fix["fallback"])
            self.assertTrue(current_fix["runtime_sensitive"])
            self.assertIs(current_fix["deployable"], expected_deployable)
            if event == "push":
                auto_route = self._run_auto_deploy_manifest_contract(current_fix)
                self.assertEqual(auto_route["route_deployable"], "true")
                self.assertEqual(auto_route["route_recovery_bootstrap_only"], "false")
                with tempfile.TemporaryDirectory() as temporary:
                    archive = self._write_production_classifier_archive(
                        Path(temporary), current_fix
                    )
                    trusted_route = validate_production_classifier_manifest(
                        archive,
                        target_sha=self.TARGET_SHA,
                        require_runtime_sensitive=True,
                    )
                self.assertEqual(trusted_route["files"], sorted(current_fix_paths))
                self.assertTrue(trusted_route["deployable"])

        helper_only = classify(
            [live_qa_guard],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertTrue(helper_only["deployable"])
        self.assertTrue(helper_only["runtime_sensitive"])
        with self.assertRaises(ProvenanceError):
            classify_cumulative_baseline(
                helper_only,
                [live_qa_guard],
                expected_target_sha=self.TARGET_SHA,
            )

        runtime_builder_only = classify(
            [live_qa_runtime_builder],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(runtime_builder_only["class"], "full")
        self.assertTrue(runtime_builder_only["deployable"])
        self.assertTrue(runtime_builder_only["runtime_sensitive"])
        with tempfile.TemporaryDirectory() as temporary:
            builder_archive = self._write_production_classifier_archive(
                Path(temporary), runtime_builder_only
            )
            trusted_builder = validate_production_classifier_manifest(
                builder_archive,
                target_sha=self.TARGET_SHA,
                require_runtime_sensitive=True,
            )
        self.assertEqual(trusted_builder["files"], [live_qa_runtime_builder])
        self.assertTrue(trusted_builder["deployable"])

        launch_helper_manifest = classify(
            [launch_helper],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(launch_helper_manifest["class"], "full")
        self.assertTrue(launch_helper_manifest["deployable"])
        self.assertFalse(launch_helper_manifest["fallback"])
        self.assertTrue(launch_helper_manifest["runtime_sensitive"])
        self.assertEqual(
            self._run_auto_deploy_manifest_contract(launch_helper_manifest)[
                "route_deployable"
            ],
            "true",
        )

        dispatcher_only = classify(
            [dispatcher],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(dispatcher_only["class"], "full")
        self.assertTrue(dispatcher_only["deployable"])
        self.assertTrue(dispatcher_only["runtime_sensitive"])
        self.assertEqual(
            self._run_auto_deploy_manifest_contract(dispatcher_only)[
                "route_recovery_bootstrap_only"
            ],
            "false",
        )

        dispatcher_and_pin = classify(
            [dispatcher, "platform/contracts/host_tools_pin.json"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(dispatcher_and_pin["class"], "full")
        self.assertTrue(dispatcher_and_pin["deployable"])
        self.assertTrue(dispatcher_and_pin["runtime_sensitive"])

        actual_current_to_candidate = [
            ".github/workflows/platform-production-autodeploy.yml",
            "platform/contracts/host_tools_pin.json",
            "platform/docs/adr/production-host-tools-provisioning.md",
            "platform/docs/test-suite-governance.md",
            "platform/tests/test_platform_host_tools_bundle.py",
            "platform/tests/test_platform_ci_classifier.py",
            "platform/tests/test_platform_recovery_bootstrap.py",
            "platform/tools/platform_test_catalog.py",
            "platform/tools/platform_ci_classifier.py",
            "platform/tools/platform_production_classifier_artifact.py",
            dispatcher,
        ]
        current_to_candidate = classify(
            actual_current_to_candidate,
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(current_to_candidate["class"], "full")
        self.assertTrue(current_to_candidate["deployable"])
        self.assertTrue(current_to_candidate["runtime_sensitive"])
        self.assertEqual(
            self._run_auto_deploy_manifest_contract(current_to_candidate)[
                "route_deployable"
            ],
            "true",
        )

        host_only = classify(
            [
                "platform/contracts/host_tools_pin.json",
                "platform/docs/adr/production-host-tools-provisioning.md",
                "platform/tests/test_platform_host_tools_bundle.py",
                "platform/tools/platform_test_catalog.py",
            ],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(host_only["class"], "full")
        self.assertFalse(host_only["deployable"])
        self.assertFalse(host_only["runtime_sensitive"])
        self.assertEqual(host_only["reason"], RECOVERY_BOOTSTRAP_REASON)
        host_only_route = classify_cumulative_baseline(
            host_only,
            host_only["files"],
            expected_target_sha=self.TARGET_SHA,
        )
        self.assertEqual(
            validate_cumulative_reconcile_route(host_only_route),
            {"no_op": True, "runtime_required": False},
        )

        supervisor = "platform/tools/platform_production_deploy_supervisor.sh"
        self.assertNotIn(supervisor, RECOVERY_BOOTSTRAP_FILES)
        self.assertIn(supervisor, RUNTIME_SENSITIVE_FILES)

        # Exact M2 -> S host-tools/runtime change set that was previously
        # misclassified as recovery-bootstrap-only. Keep this path fixture
        # explicit so an allowlist edit cannot silently restore the no-op.
        m2_to_s_paths = [
            "platform/contracts/host_tools_pin.json",
            "platform/docs/adr/production-host-tools-provisioning.md",
            "platform/docs/performance-transport-runbook.md",
            "platform/tests/test_platform_release_build_contract.py",
            supervisor,
        ]
        direct_m2_to_s = classify(
            m2_to_s_paths,
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertTrue(direct_m2_to_s["deployable"])
        self.assertTrue(direct_m2_to_s["runtime_sensitive"])
        self.assertEqual(
            self._run_auto_deploy_manifest_contract(direct_m2_to_s)[
                "route_deployable"
            ],
            "true",
        )

        # The classifier-only correction itself is a permitted bootstrap
        # increment. Reconciliation must still classify the complete active
        # M2 -> target range and notice the supervisor runtime change.
        classifier_fix_paths = [
            ".github/workflows/platform-production-autodeploy.yml",
            "platform/docs/deployment-runbook.md",
            "platform/tests/test_platform_ci_classifier.py",
            "platform/tests/test_platform_recovery_bootstrap.py",
            "platform/tools/platform_ci_classifier.py",
            "platform/tools/platform_production_classifier_artifact.py",
        ]
        incremental = classify(
            classifier_fix_paths,
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertFalse(incremental["deployable"])
        self.assertEqual(incremental["reason"], RECOVERY_BOOTSTRAP_REASON)

        cumulative = classify_cumulative_baseline(
            incremental,
            m2_to_s_paths + classifier_fix_paths + [live_qa_guard],
            expected_target_sha=self.TARGET_SHA,
        )
        decision = validate_cumulative_reconcile_route(cumulative)
        self.assertEqual(decision, {"no_op": False, "runtime_required": True})
        self.assertTrue(cumulative["manifest"]["deployable"])
        self.assertTrue(cumulative["manifest"]["runtime_sensitive"])
        self.assertFalse(cumulative["no_op"])

        incremental_auto = self._run_auto_deploy_manifest_contract(incremental)
        self.assertEqual(incremental_auto["route_recovery_bootstrap_only"], "true")
        deploy_auto = self._run_auto_deploy_manifest_contract(cumulative["manifest"])
        self.assertEqual(deploy_auto["route_deployable"], "true")
        self.assertEqual(deploy_auto["route_recovery_bootstrap_only"], "false")

        genuine_bootstrap = classify(
            ["platform/tools/platform_recovery_bootstrap.py"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertFalse(genuine_bootstrap["deployable"])
        self.assertEqual(genuine_bootstrap["reason"], RECOVERY_BOOTSTRAP_REASON)
        bootstrap_route = classify_cumulative_baseline(
            genuine_bootstrap,
            genuine_bootstrap["files"],
            expected_target_sha=self.TARGET_SHA,
        )
        self.assertEqual(
            validate_cumulative_reconcile_route(bootstrap_route),
            {"no_op": True, "runtime_required": False},
        )

    def test_service_recovery_source_and_secret_workflow_keep_full_runtime_route(self) -> None:
        paths = (
            ".github/workflows/platform-production-service-recovery.yml",
            "platform/tools/platform_api_worker_service_recovery.py",
            "platform/tests/test_platform_service_recovery_workflow.py",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertIn(path, RUNTIME_SENSITIVE_FILES)
                manifest = classify(
                    [path], event="push", target_sha=self.TARGET_SHA, branch="dev"
                )
                self.assertEqual(manifest["class"], "full")
                self.assertFalse(manifest["fallback"])
                self.assertTrue(manifest["runtime_sensitive"])
                self.assertTrue(manifest["deployable"])
        for path in (
            "platform/tools/platform_ci_classifier.py",
            "platform/tests/test_platform_ci_classifier.py",
        ):
            # The recovery runtime paths are separately classified above;
            # these classifier paths retain their non-deployable bootstrap route.
            with self.subTest(classifier_path=path):
                self.assertNotIn(path, RUNTIME_SENSITIVE_FILES)
                classifier_change = classify(
                    [path], event="push", target_sha=self.TARGET_SHA, branch="dev"
                )
                self.assertEqual(classifier_change["class"], "full")
                self.assertFalse(classifier_change["fallback"])
                self.assertFalse(classifier_change["runtime_sensitive"])
                self.assertFalse(classifier_change["deployable"])
        combined = classify(
            [*paths, "platform/tools/platform_ci_classifier.py", "platform/tests/test_platform_ci_classifier.py"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(combined["class"], "full")
        self.assertFalse(combined["fallback"])
        self.assertTrue(combined["runtime_sensitive"])
        self.assertTrue(combined["deployable"])

    def test_deploy_consumers_validate_the_exact_classifier_artifact(self) -> None:
        auto = AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        production = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        for workflow in (auto, production):
            self.assertIn("target_sha", workflow)
            self.assertIn("platform-ci-route", workflow)
            self.assertIn("digest", workflow)
        self.assertIn("runtime_sensitive", auto)
        classifier_tool = (
            REPO_ROOT / "platform/tools/platform_production_classifier_artifact.py"
        ).read_text(encoding="utf-8")
        self.assertIn("extract_single_manifest", classifier_tool)
        self.assertIn("runtime_sensitive", classifier_tool)
        self.assertIn("object_pairs_hook=_reject_duplicate_keys", classifier_tool)
        self.assertIn("type(manifest.get(\"deployable\")) is not bool", classifier_tool)
        self.assertIn("classifier_run_id", auto)
        self.assertIn("classifier_run_attempt", auto)
        self.assertIn("platform_production_classifier_artifact.py", production)
        self.assertIn("--allow-docs-only", production)

    def test_production_manifest_validator_runs_with_isolated_python(self) -> None:
        manifest = classify(
            sorted(RECOVERY_BOOTSTRAP_FILES),
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        with tempfile.TemporaryDirectory() as temporary:
            archive = self._write_production_classifier_archive(
                Path(temporary), manifest
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(
                        REPO_ROOT
                        / "platform/tools/platform_production_classifier_artifact.py"
                    ),
                    "manifest",
                    str(archive),
                    "--target-sha",
                    self.TARGET_SHA,
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "classifier manifest accepted")

        runtime_manifest = classify(
            ["platform/apps/platform_api/app/main.py"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertTrue(runtime_manifest["deployable"])
        with tempfile.TemporaryDirectory() as temporary:
            runtime_archive = self._write_production_classifier_archive(
                Path(temporary), runtime_manifest
            )
            runtime_result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(REPO_ROOT / "platform/tools/platform_production_classifier_artifact.py"),
                    "manifest",
                    str(runtime_archive),
                    "--target-sha",
                    self.TARGET_SHA,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(runtime_result.returncode, 0, runtime_result.stderr)
        self.assertEqual(runtime_result.stdout.strip(), "classifier manifest accepted")

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

    def test_production_manifest_reconcile_option_accepts_only_trusted_non_deployable_families(self) -> None:
        candidate = classify(
            sorted(CANDIDATE_PACKAGING_FILES),
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(candidate["reason"], CANDIDATE_PACKAGING_REASON)
        self.assertFalse(candidate["deployable"])
        self.assertFalse(candidate["runtime_sensitive"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_archive = self._write_production_classifier_archive(root, candidate)
            with self.assertRaises(ClassifierArtifactError):
                validate_production_classifier_manifest(
                    candidate_archive, target_sha=self.TARGET_SHA
                )
            accepted_candidate = validate_production_classifier_manifest(
                candidate_archive,
                target_sha=self.TARGET_SHA,
                require_reconcile_source=True,
            )
            self.assertEqual(accepted_candidate["reason"], CANDIDATE_PACKAGING_REASON)
            with self.assertRaises(ClassifierArtifactError):
                validate_production_classifier_manifest(
                    candidate_archive,
                    target_sha=self.TARGET_SHA,
                    allow_docs_only=True,
                )
            accepted_auto_candidate = validate_production_classifier_manifest(
                candidate_archive,
                target_sha=self.TARGET_SHA,
                allow_candidate_packaging=True,
            )
            self.assertEqual(
                accepted_auto_candidate["reason"], CANDIDATE_PACKAGING_REASON
            )
            accepted_candidate_skip = validate_production_classifier_manifest(
                candidate_archive,
                target_sha=self.TARGET_SHA,
                require_candidate_packaging=True,
            )
            self.assertEqual(
                accepted_candidate_skip["reason"], CANDIDATE_PACKAGING_REASON
            )
            candidate_skip_cli = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(REPO_ROOT / "platform/tools/platform_production_classifier_artifact.py"),
                    "manifest",
                    str(candidate_archive),
                    "--target-sha",
                    self.TARGET_SHA,
                    "--require-candidate-packaging",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                candidate_skip_cli.returncode, 0, candidate_skip_cli.stderr
            )
            with self.assertRaises(ClassifierArtifactError):
                validate_production_classifier_manifest(
                    candidate_archive,
                    target_sha=self.TARGET_SHA,
                    allow_candidate_packaging=True,
                    require_candidate_packaging=True,
                )
            with self.assertRaises(ClassifierArtifactError):
                validate_production_classifier_manifest(
                    candidate_archive,
                    target_sha=self.TARGET_SHA,
                    allow_out_of_scope=True,
                    require_candidate_packaging=True,
                )
            with self.assertRaises(ClassifierArtifactError):
                validate_production_classifier_manifest(
                    candidate_archive,
                    target_sha=self.TARGET_SHA,
                    require_recovery_bootstrap=True,
                )

            recovery_path = sorted(RECOVERY_BOOTSTRAP_FILES)[0]
            recovery = classify(
                [recovery_path],
                event="push",
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            recovery_archive = self._write_production_classifier_archive(root, recovery)
            self.assertEqual(
                validate_production_classifier_manifest(
                    recovery_archive,
                    target_sha=self.TARGET_SHA,
                    require_reconcile_source=True,
                )["reason"],
                RECOVERY_BOOTSTRAP_REASON,
            )
            self.assertEqual(
                validate_production_classifier_manifest(
                    recovery_archive,
                    target_sha=self.TARGET_SHA,
                    allow_docs_only=True,
                    require_reconcile_source=True,
                )["reason"],
                RECOVERY_BOOTSTRAP_REASON,
            )
            validate_production_classifier_manifest(
                recovery_archive,
                target_sha=self.TARGET_SHA,
                require_recovery_bootstrap=True,
            )

            host_tools_adr = "platform/docs/adr/production-host-tools-provisioning.md"
            adr_route = classify(
                [host_tools_adr],
                event="push",
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            adr_archive = self._write_production_classifier_archive(root, adr_route)
            accepted_adr = validate_production_classifier_manifest(
                adr_archive,
                target_sha=self.TARGET_SHA,
                require_recovery_bootstrap=True,
            )
            self.assertEqual(accepted_adr["reason"], RECOVERY_BOOTSTRAP_REASON)
            self.assertFalse(accepted_adr["deployable"])

            recovery_caller = "platform/tests/test_platform_recovery_workflow_caller.py"
            caller_route = classify(
                [recovery_caller],
                event="push",
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            self.assertFalse(caller_route["deployable"])
            self.assertTrue(caller_route["runtime_sensitive"])
            caller_archive = self._write_production_classifier_archive(root, caller_route)
            caller_authority = validate_production_classifier_manifest(
                caller_archive,
                target_sha=self.TARGET_SHA,
                require_reconcile_source=True,
                require_runtime_sensitive=True,
            )
            self.assertEqual(caller_authority["files"], [recovery_caller])

            deployable = classify(
                ["platform/apps/platform_api/app/main.py"],
                event="push",
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            deployable_archive = self._write_production_classifier_archive(root, deployable)
            with self.assertRaises(ClassifierArtifactError):
                validate_production_classifier_manifest(
                    deployable_archive,
                    target_sha=self.TARGET_SHA,
                    require_reconcile_source=True,
                )

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
            "path": SECURITY_WORKFLOW_PATH,
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
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        status_final = workflow.split("  status-final:", 1)[1]
        for event in ("pull_request", "push", "merge_group", "workflow_dispatch"):
            self.assertRegex(workflow, rf"(?m)^  {event}:", msg=f"missing {event} trigger")
        self.assertGreaterEqual(workflow.count("TESTED_SHA: ${{ github.sha }}"), 3)
        self.assertIn('--target-sha "$TESTED_SHA"', workflow)
        self.assertIn('source_head = (pull.get("head") or {}).get("sha")', workflow)
        self.assertIn(
            '["git", "diff", "--name-only", "-z", base, source_head, "--"]',
            workflow,
        )
        self.assertIn("route_target_sha != tested_sha", status_final)
        self.assertIn('"tested_sha": os.environ.get("TESTED_SHA", "")', status_final)
        self.assertIn('statuses/${TESTED_SHA}', workflow)
        self.assertNotIn('statuses/${GITHUB_SHA}', workflow)
        self.assertIn(
            'published_event = os.environ.get("EVENT_NAME") in {"push", "workflow_dispatch"}',
            status_final,
        )
        self.assertIn(
            'if [[ "$EVENT_NAME" == "push" || "$EVENT_NAME" == "workflow_dispatch" ]]; then',
            status_final,
        )
        self.assertIn("passed=$(", status_final)
        self.assertIn('if [[ "$passed" == "true" ]]; then', status_final)
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
        self.assertIn("published_event", workflow)
        self.assertIn("expected_by_class", workflow)
        self.assertIn("A full route can be CI-only", workflow)
        self.assertIn("ROUTE_EVENT", workflow)
        self.assertIn("classifier event does not match the workflow event", workflow)
        self.assertIn("classifier expected gates do not match its class", workflow)
        self.assertIn("classifier digest is missing", workflow)
        self.assertIn("status-start did not succeed before status publication", workflow)
        self.assertIn(
            'raw_runtime_sensitive = os.environ.get("ROUTE_RUNTIME_SENSITIVE", "")',
            status_final,
        )
        self.assertIn(
            'raw_runtime_sensitive not in {"true", "false"}',
            status_final,
        )
        self.assertIn(
            "classifier runtime-sensitive output is missing or malformed",
            status_final,
        )
        self.assertIn(
            'deployable_full_dev_push = (',
            status_final,
        )
        self.assertIn("or deployable_full_dev_push", status_final)
        self.assertIn('release_runtime_result != "success"', status_final)
        self.assertIn('release_runtime_result != "skipped"', status_final)
        self.assertIn("RELEASE_RUNTIME_REAL_RESULT", status_final)
        self.assertIn("needs['release-runtime-real']", workflow)
        self.assertIn("requires_real_release_runtime", status_final)

        script_match = re.search(
            r"(?ms)^\s+/usr/bin/python3 - <<'PY'\n(?P<script>.*?)^\s+PY$",
            status_final,
        )
        self.assertIsNotNone(script_match)
        assert script_match is not None
        status_script = textwrap.dedent(script_match.group("script"))
        base_environment = {
            "CLASSIFIER_RESULT": "success",
            "PROOF_MODE": "standard",
            "BASELINE_GUARD_RESULT": "skipped",
            "HOST_TOOLS_PIN_PREFLIGHT_RESULT": "success",
            "EVENT_NAME": "pull_request",
            "ROUTE_EVENT": "pull_request",
            "ROUTE_CLASS": "docs-only",
            "ROUTE_DEPLOYABLE": "false",
            "ROUTE_FALLBACK": "false",
            "ROUTE_REASON": "trusted docs route",
            "ROUTE_DIGEST": "a" * 64,
            "ROUTE_TARGET_SHA": self.TARGET_SHA,
            "TESTED_SHA": self.TARGET_SHA,
            "EXPECTED_GATES": '["docs", "verification-contract"]',
            "DOCS_RESULT": "success",
            "VERIFICATION_CONTRACT_RESULT": "success",
            "STATUS_START_RESULT": "skipped",
            "WORKFLOW_REF": "refs/heads/feature",
            "RELEASE_RUNTIME_REAL_RESULT": "skipped",
        }
        status_cases = (
            ("missing", None, "false", "skipped", False),
            ("empty", "", "false", "skipped", False),
            ("uppercase", "TRUE", "false", "skipped", False),
            ("sensitive success", "true", "false", "success", True),
            ("sensitive skipped", "true", "false", "skipped", False),
            ("fallback success", "false", "true", "success", True),
            ("fallback skipped", "false", "true", "skipped", False),
            ("ordinary skipped", "false", "false", "skipped", True),
            ("ordinary success", "false", "false", "success", False),
        )
        with tempfile.TemporaryDirectory() as directory:
            for label, raw_runtime, raw_fallback, release_result, expected_passed in status_cases:
                with self.subTest(status_case=label):
                    environment = os.environ.copy()
                    environment.update(
                        {
                            **base_environment,
                            "ROUTE_FALLBACK": raw_fallback,
                            "RELEASE_RUNTIME_RESULT": release_result,
                            "SUMMARY_PATH": str(Path(directory) / f"{label}.json"),
                        }
                    )
                    if raw_runtime is not None:
                        environment["ROUTE_RUNTIME_SENSITIVE"] = raw_runtime
                    else:
                        environment.pop("ROUTE_RUNTIME_SENSITIVE", None)
                    completed = subprocess.run(
                        ["/usr/bin/python3"],
                        input=status_script,
                        text=True,
                        capture_output=True,
                        env=environment,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(completed.stdout.strip(), str(expected_passed).lower())
                    summary = json.loads(
                        Path(environment["SUMMARY_PATH"]).read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        summary["requires_release_runtime"],
                        raw_runtime == "true" or raw_fallback == "true",
                    )

            for preflight_result in (None, "failure", "skipped"):
                with self.subTest(host_tools_pin_preflight=preflight_result):
                    environment = os.environ.copy()
                    environment.update(base_environment)
                    if preflight_result is None:
                        environment.pop("HOST_TOOLS_PIN_PREFLIGHT_RESULT", None)
                    else:
                        environment["HOST_TOOLS_PIN_PREFLIGHT_RESULT"] = preflight_result
                    environment["SUMMARY_PATH"] = str(
                        Path(directory) / f"preflight-{preflight_result or 'missing'}.json"
                    )
                    completed = subprocess.run(
                        ["/usr/bin/python3"],
                        input=status_script,
                        text=True,
                        capture_output=True,
                        env=environment,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(completed.stdout.strip(), "false")

            trusted_base = {
                **base_environment,
                "EVENT_NAME": "push",
                "ROUTE_EVENT": "push",
                "ROUTE_CLASS": "full",
                "ROUTE_FALLBACK": "false",
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
                "BACKEND_RESULT": "success",
                "PYTHON_QUALITY_RESULT": "success",
                "SECURITY_RESULT": "success",
                "MIGRATION_RESULT": "success",
                "WEB_QUALITY_RESULT": "success",
                "WEB_HERMETIC_RESULT": "success",
                "STATUS_START_RESULT": "success",
                "RELEASE_RUNTIME_RESULT": "success",
            }
            trusted_cases = (
                (
                    "dev push real",
                    "push",
                    "refs/heads/dev",
                    "true",
                    "false",
                    "true",
                    "success",
                    "success",
                    True,
                    True,
                ),
                (
                    "dev push real skipped",
                    "push",
                    "refs/heads/dev",
                    "true",
                    "false",
                    "true",
                    "success",
                    "skipped",
                    False,
                    True,
                ),
                (
                    "dev push real failure",
                    "push",
                    "refs/heads/dev",
                    "true",
                    "false",
                    "true",
                    "success",
                    "failure",
                    False,
                    True,
                ),
                (
                    "dev manual real",
                    "workflow_dispatch",
                    "refs/heads/dev",
                    "true",
                    "false",
                    "false",
                    "success",
                    "success",
                    True,
                    True,
                ),
                (
                    "fallback dev real",
                    "push",
                    "refs/heads/dev",
                    "true",
                    "true",
                    "false",
                    "success",
                    "success",
                    True,
                    True,
                ),
                (
                    "ordinary dev both skipped",
                    "push",
                    "refs/heads/dev",
                    "false",
                    "false",
                    "false",
                    "skipped",
                    "skipped",
                    True,
                    False,
                ),
                # Candidate packaging keeps the full gate class but is a
                # successful CI-only push when deployable=false.
                (
                    "candidate packaging dev no-op",
                    "push",
                    "refs/heads/dev",
                    "false",
                    "false",
                    "false",
                    "skipped",
                    "skipped",
                    True,
                    False,
                ),
                (
                    "deployable dev push both skipped",
                    "push",
                    "refs/heads/dev",
                    "false",
                    "false",
                    "true",
                    "skipped",
                    "skipped",
                    False,
                    True,
                ),
                (
                    "deployable dev push fixture missing",
                    "push",
                    "refs/heads/dev",
                    "false",
                    "false",
                    "true",
                    "missing",
                    "success",
                    False,
                    True,
                ),
                (
                    "deployable dev push both success",
                    "push",
                    "refs/heads/dev",
                    "false",
                    "false",
                    "true",
                    "success",
                    "success",
                    True,
                    True,
                ),
                (
                    "non-dev manual fixture only",
                    "workflow_dispatch",
                    "refs/heads/feature",
                    "true",
                    "false",
                    "false",
                    "success",
                    "skipped",
                    True,
                    False,
                ),
                (
                    "merge group fixture only",
                    "merge_group",
                    "refs/heads/gh-readonly-queue/main/pr-1-abc",
                    "true",
                    "false",
                    "false",
                    "success",
                    "skipped",
                    True,
                    False,
                ),
            )
            for (
                label,
                event_name,
                workflow_ref,
                raw_runtime_sensitive,
                raw_fallback,
                raw_deployable,
                fixture_result,
                real_result,
                expected_passed,
                expected_requires_real,
            ) in trusted_cases:
                with self.subTest(trusted_status_case=label):
                    environment = os.environ.copy()
                    environment.update(
                        {
                            **trusted_base,
                            "EVENT_NAME": event_name,
                            "ROUTE_EVENT": event_name,
                            "WORKFLOW_REF": workflow_ref,
                            "ROUTE_RUNTIME_SENSITIVE": raw_runtime_sensitive,
                            "ROUTE_FALLBACK": raw_fallback,
                            "ROUTE_DEPLOYABLE": raw_deployable,
                            "RELEASE_RUNTIME_RESULT": fixture_result,
                            "RELEASE_RUNTIME_REAL_RESULT": real_result,
                            "SUMMARY_PATH": str(Path(directory) / f"trusted-{label}.json"),
                        }
                    )
                    completed = subprocess.run(
                        ["/usr/bin/python3"],
                        input=status_script,
                        text=True,
                        capture_output=True,
                        env=environment,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(completed.stdout.strip(), str(expected_passed).lower())
                    summary = json.loads(
                        Path(environment["SUMMARY_PATH"]).read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        summary["requires_real_release_runtime"], expected_requires_real
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

    def test_production_artifact_validator_accepts_only_exact_docs_only_mode(self) -> None:
        files = ["platform/docs/operations-runbook.md"]
        manifest = classify(
            files,
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(manifest["class"], "docs-only")
        self.assertEqual(manifest["reason"], DOCS_ONLY_REASON)
        self.assertEqual(manifest["expected_gates"], list(DOCS_ONLY_GATE_IDS))
        self.assertIs(manifest["runtime_sensitive"], False)
        self.assertIs(manifest["deployable"], False)
        self.assertIs(manifest["fallback"], False)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = self._write_production_classifier_archive(root, manifest)
            with self.assertRaises(ClassifierArtifactError):
                validate_production_classifier_manifest(
                    archive, target_sha=self.TARGET_SHA
                )
            exact = validate_production_classifier_manifest(
                archive,
                target_sha=self.TARGET_SHA,
                require_docs_only=True,
            )
            self.assertEqual(exact, manifest)
            self.assertEqual(
                validate_production_classifier_manifest(
                    archive,
                    target_sha=self.TARGET_SHA,
                    allow_docs_only=True,
                ),
                manifest,
            )
            self.assertEqual(
                validate_production_classifier_manifest(
                    archive,
                    target_sha=self.TARGET_SHA,
                    allow_docs_only=True,
                    require_reconcile_source=True,
                ),
                manifest,
            )
            cli = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(REPO_ROOT / "platform/tools/platform_production_classifier_artifact.py"),
                    "manifest",
                    str(archive),
                    "--target-sha",
                    self.TARGET_SHA,
                    "--require-docs-only",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(cli.returncode, 0, cli.stderr)

            malformed_docs = [
                dict(manifest, reason="full verification required"),
                dict(manifest, expected_gates=list(FULL_GATE_IDS)),
                dict(manifest, runtime_sensitive=True),
                dict(manifest, deployable=True),
                dict(manifest, fallback=True),
                dict(manifest, target_sha="b" * 40),
            ]
            malformed_docs.extend(
                [
                    dict(manifest, files=["platform/docs/../tools/runtime.py"]),
                    dict(manifest, files=["platform/docs/.."]),
                    dict(manifest, files=["platform/docs/."]),
                    dict(manifest, files=["platform/docs/a/.."]),
                    dict(manifest, files=["platform/docs\\runtime.md"]),
                    dict(manifest, files=["platform/docs/a.md", "platform/docs/a.md"]),
                ]
            )
            for changed in malformed_docs:
                changed["digest"] = manifest_digest(changed)
                changed_archive = self._write_production_classifier_archive(
                    root, changed
                )
                with self.subTest(changed=changed):
                    with self.assertRaises(ClassifierArtifactError):
                        validate_production_classifier_manifest(
                            changed_archive,
                            target_sha=self.TARGET_SHA,
                            require_docs_only=True,
                        )
                    with self.assertRaises(ClassifierArtifactError):
                        validate_production_classifier_manifest(
                            changed_archive,
                            target_sha=self.TARGET_SHA,
                            allow_docs_only=True,
                        )

            mixed = classify(
                ["platform/docs/operations-runbook.md", "platform/tools/runtime.py"],
                event="push",
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            mixed_archive = self._write_production_classifier_archive(root, mixed)
            with self.assertRaises(ClassifierArtifactError):
                validate_production_classifier_manifest(
                    mixed_archive,
                    target_sha=self.TARGET_SHA,
                    require_docs_only=True,
                )
            # The producer's allow mode permits normal full routes; its caller
            # must still branch on the validated class before deciding to skip.
            self.assertEqual(
                validate_production_classifier_manifest(
                    mixed_archive,
                    target_sha=self.TARGET_SHA,
                    allow_docs_only=True,
                )["class"],
                "full",
            )
            with self.assertRaises(ClassifierArtifactError):
                validate_production_classifier_manifest(
                    mixed_archive,
                    target_sha=self.TARGET_SHA,
                    allow_docs_only=True,
                    require_reconcile_source=True,
                )

            unknown = dict(manifest, **{"class": "unknown"})
            unknown["digest"] = manifest_digest(unknown)
            unknown_archive = self._write_production_classifier_archive(root, unknown)
            with self.assertRaises(ClassifierArtifactError):
                validate_production_classifier_manifest(
                    unknown_archive,
                    target_sha=self.TARGET_SHA,
                    allow_docs_only=True,
                )

            with self.assertRaises(ClassifierArtifactError):
                validate_production_classifier_manifest(
                    archive,
                    target_sha=self.TARGET_SHA,
                    allow_docs_only=True,
                    require_docs_only=True,
                )
            with self.assertRaises(ClassifierArtifactError):
                validate_production_classifier_manifest(
                    archive,
                    target_sha=self.TARGET_SHA,
                    allow_docs_only=1,
                )

    def test_storage_operations_classifier_and_artifact_route_are_exact(self) -> None:
        files = sorted(
            STORAGE_OPERATIONS_FILES | {"platform/docs/deployment-runbook.md"}
        )
        manifest = classify(
            files,
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(manifest["class"], "full")
        self.assertEqual(tuple(manifest["expected_gates"]), FULL_GATE_IDS)
        self.assertTrue(manifest["runtime_sensitive"])
        self.assertFalse(manifest["deployable"])
        self.assertFalse(manifest["fallback"])
        self.assertEqual(manifest["reason"], STORAGE_OPERATIONS_REASON)
        validate_manifest(manifest, expected_target_sha=self.TARGET_SHA)

        skill_only = classify(
            [".agents/skills/platform-storage-retention/SKILL.md"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(skill_only["reason"], STORAGE_OPERATIONS_REASON)
        self.assertEqual(tuple(skill_only["expected_gates"]), FULL_GATE_IDS)
        self.assertTrue(skill_only["runtime_sensitive"])
        self.assertFalse(skill_only["deployable"])
        self.assertFalse(skill_only["fallback"])

        skill_with_application = classify(
            [
                ".agents/skills/platform-storage-retention/SKILL.md",
                "platform/apps/platform_api/app/main.py",
            ],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertNotEqual(skill_with_application["reason"], STORAGE_OPERATIONS_REASON)
        self.assertTrue(skill_with_application["deployable"])
        self.assertFalse(skill_with_application["fallback"])

        skill_with_unknown = classify(
            [
                ".agents/skills/platform-storage-retention/SKILL.md",
                "unknown-root-config.toml",
            ],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertNotEqual(skill_with_unknown["reason"], STORAGE_OPERATIONS_REASON)
        self.assertTrue(skill_with_unknown["fallback"])
        self.assertFalse(skill_with_unknown["deployable"])

        unrelated_mixed = classify(
            [
                ".github/workflows/platform-production-storage-diagnostics.yml",
                "platform/tools/unreviewed_storage_helper.py",
            ],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertNotEqual(unrelated_mixed["reason"], STORAGE_OPERATIONS_REASON)
        self.assertTrue(unrelated_mixed["deployable"])
        self.assertFalse(unrelated_mixed["fallback"])
        self.assertTrue(unrelated_mixed["runtime_sensitive"])

        global_mixed = classify(
            [
                ".github/workflows/platform-production-storage-diagnostics.yml",
                "unknown-root-config.toml",
            ],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertNotEqual(global_mixed["reason"], STORAGE_OPERATIONS_REASON)
        self.assertTrue(global_mixed["fallback"])
        self.assertFalse(global_mixed["deployable"])

        shared_controls_only = classify(
            [
                ".github/workflows/platform-production-autodeploy.yml",
                "platform/tests/test_platform_ci_classifier.py",
                "platform/tools/platform_ci_classifier.py",
                "platform/tools/platform_production_classifier_artifact.py",
                "platform/tools/platform_test_catalog.py",
            ],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        self.assertEqual(shared_controls_only["reason"], RECOVERY_BOOTSTRAP_REASON)
        self.assertFalse(shared_controls_only["deployable"])
        self.assertFalse(shared_controls_only["fallback"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = self._write_production_classifier_archive(root, manifest)
            accepted = validate_production_classifier_manifest(
                archive, target_sha=self.TARGET_SHA
            )
            self.assertEqual(accepted["reason"], STORAGE_OPERATIONS_REASON)
            self.assertEqual(
                validate_production_classifier_manifest(
                    archive,
                    target_sha=self.TARGET_SHA,
                    allow_docs_only=True,
                )["reason"],
                STORAGE_OPERATIONS_REASON,
            )
            with self.assertRaises(ClassifierArtifactError):
                validate_production_classifier_manifest(
                    archive,
                    target_sha=self.TARGET_SHA,
                    require_reconcile_source=True,
                )

            inconsistent = dict(manifest)
            inconsistent["runtime_sensitive"] = False
            inconsistent["digest"] = manifest_digest(inconsistent)
            with self.assertRaises(ClassifierError):
                validate_manifest(inconsistent, expected_target_sha=self.TARGET_SHA)

            out_of_scope = classify(
                ["README.md", "tools/maintenance.py"],
                event="push",
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            self.assertEqual(out_of_scope["class"], "out-of-scope")
            self.assertEqual(out_of_scope["expected_gates"], list(OUT_OF_SCOPE_GATE_IDS))
            self.assertFalse(out_of_scope["runtime_sensitive"])
            self.assertFalse(out_of_scope["deployable"])
            self.assertFalse(out_of_scope["fallback"])
            out_of_scope_archive = self._write_production_classifier_archive(
                root, out_of_scope
            )
            for options in ({}, {"allow_docs_only": True}, {"allow_candidate_packaging": True}):
                with self.subTest(out_of_scope_options=options):
                    with self.assertRaises(ClassifierArtifactError):
                        validate_production_classifier_manifest(
                            out_of_scope_archive,
                            target_sha=self.TARGET_SHA,
                            **options,
                        )
            accepted_out_of_scope = validate_production_classifier_manifest(
                out_of_scope_archive,
                target_sha=self.TARGET_SHA,
                allow_out_of_scope=True,
            )
            self.assertEqual(accepted_out_of_scope["digest"], out_of_scope["digest"])
            self.assertEqual(
                validate_production_classifier_manifest(
                    out_of_scope_archive,
                    target_sha=self.TARGET_SHA,
                    require_out_of_scope=True,
                )["digest"],
                out_of_scope["digest"],
            )
            required_out_of_scope_cli = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(REPO_ROOT / "platform/tools/platform_production_classifier_artifact.py"),
                    "manifest",
                    str(out_of_scope_archive),
                    "--target-sha",
                    self.TARGET_SHA,
                    "--require-out-of-scope",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                required_out_of_scope_cli.returncode,
                0,
                required_out_of_scope_cli.stderr,
            )
            for options in (
                {"allow_out_of_scope": True, "require_out_of_scope": True},
                {"allow_out_of_scope": True, "require_reconcile_source": True},
                {"allow_out_of_scope": True, "require_runtime_sensitive": True},
                {"allow_out_of_scope": True, "require_recovery_bootstrap": True},
            ):
                with self.subTest(out_of_scope_conflicting_options=options):
                    with self.assertRaises(ClassifierArtifactError):
                        validate_production_classifier_manifest(
                            out_of_scope_archive,
                            target_sha=self.TARGET_SHA,
                            **options,
                        )
            for key, value in (
                ("runtime_sensitive", True),
                ("deployable", True),
                ("fallback", True),
                ("expected_gates", list(FULL_GATE_IDS)),
                ("reason", "strict platform/docs-only change"),
                ("files", ["tools/../README.md"]),
                (
                    "files",
                    [*out_of_scope["files"], "platform/tools/runtime.py"],
                ),
            ):
                tampered = dict(out_of_scope, **{key: value})
                tampered["digest"] = manifest_digest(tampered)
                tampered_archive = self._write_production_classifier_archive(
                    root, tampered
                )
                with self.subTest(out_of_scope_tamper=key):
                    with self.assertRaises(ClassifierArtifactError):
                        validate_production_classifier_manifest(
                            tampered_archive,
                            target_sha=self.TARGET_SHA,
                            allow_out_of_scope=True,
                        )

    def test_storage_operations_autodeploy_validates_then_skips_dispatch(self) -> None:
        workflow = AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("_is_storage_operations_only", workflow)
        self.assertIn("STORAGE_OPERATIONS_REASON", workflow)
        self.assertIn("--allow-out-of-scope", workflow)

        route = classify(
            sorted(STORAGE_OPERATIONS_FILES),
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        outputs = self._run_auto_deploy_manifest_contract(route)
        self.assertEqual(outputs["route_storage_operations_only"], "true")
        self.assertEqual(outputs["route_recovery_bootstrap_only"], "false")
        self.assertEqual(outputs["route_candidate_packaging_only"], "false")
        self.assertEqual(outputs["route_deployable"], "false")
        out_of_scope_route = classify(
            ["README.md", "tools/maintenance.py"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        out_of_scope_outputs = self._run_auto_deploy_manifest_contract(
            out_of_scope_route
        )
        self.assertEqual(out_of_scope_outputs["route_class"], "out-of-scope")
        self.assertEqual(out_of_scope_outputs["route_out_of_scope_only"], "true")
        self.assertEqual(out_of_scope_outputs["route_deployable"], "false")

        step_start = workflow.index(
            "      - name: Verify tested SHA is current dev head\n"
        )
        run_marker = "        run: |\n"
        run_start = workflow.index(run_marker, step_start) + len(run_marker)
        run_end = workflow.index("\n      - name: ", run_start)
        gate_script = textwrap.dedent(workflow[run_start:run_end])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_CURL_CALLS\"\n"
                "case \"$*\" in\n"
                "  *'/branches/dev'*) printf '{\"commit\":{\"sha\":\"%s\"}}\\n' \"$TARGET_SHA\" ;;\n"
                "  *) exit 91 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o700)
            output = root / "github-output"
            output.touch()
            curl_calls = root / "curl-calls"
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.defpath}",
                "GITHUB_OUTPUT": str(output),
                "GITHUB_API_URL": "https://api.github.com",
                "GITHUB_REPOSITORY": "StrayForest/old_sparky",
                "TARGET_SHA": self.TARGET_SHA,
                "GH_TOKEN": "fixture-token",
                "RUNNER_TEMP": str(runner_temp),
                "FAKE_CURL_CALLS": str(curl_calls),
                "ROUTE_CLASS": "full",
                "ROUTE_DEPLOYABLE": "false",
                "ROUTE_RECOVERY_BOOTSTRAP_ONLY": "false",
                "ROUTE_CANDIDATE_PACKAGING_ONLY": "false",
                "ROUTE_STORAGE_OPERATIONS_ONLY": "true",
                "ROUTE_OUT_OF_SCOPE_ONLY": "false",
            }
            completed = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", gate_script],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(
                "no application deployment or recovery action required",
                completed.stdout,
            )
            self.assertEqual(output.read_text(encoding="utf-8").strip(), "deploy=false")
            calls = curl_calls.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(calls), 1)
            self.assertIn("/branches/dev", calls[0])
            self.assertNotIn("/workflows/platform-production-deploy.yml/dispatches", calls[0])

            output.write_text("", encoding="utf-8")
            curl_calls.write_text("", encoding="utf-8")
            environment.update(
                {
                    "ROUTE_CLASS": "out-of-scope",
                    "ROUTE_STORAGE_OPERATIONS_ONLY": "false",
                    "ROUTE_OUT_OF_SCOPE_ONLY": "true",
                }
            )
            out_of_scope_gate = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", gate_script],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(out_of_scope_gate.returncode, 0, out_of_scope_gate.stderr)
            self.assertIn("no application deployment or baseline reconciliation", out_of_scope_gate.stdout)
            self.assertEqual(output.read_text(encoding="utf-8").strip(), "deploy=false")
            self.assertEqual(len(curl_calls.read_text(encoding="utf-8").splitlines()), 1)

    def test_recovery_producer_storage_route_emits_bound_receipt_inputs(self) -> None:
        blocks = _workflow_python_blocks(
            RECOVERY_BUILD_WORKFLOW,
            "Download and validate exact security route artifact",
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(
            _python_set_assignment(blocks[0], "storage_files"),
            set(STORAGE_OPERATIONS_FILES),
        )
        self.assertEqual(
            _python_set_assignment(blocks[0], "triggers"),
            set(STORAGE_OPERATIONS_TRIGGER_FILES),
        )
        manifest = classify(
            sorted(STORAGE_OPERATIONS_FILES),
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        encoded = base64.b64encode(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).decode("ascii")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            completed = subprocess.run(
                [sys.executable, "-I", "-", encoded, str(output), self.TARGET_SHA],
                input=blocks[0],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(values["route_skip"], "true")
            self.assertEqual(values["route_family"], "storage-operations")
            self.assertEqual(values["classifier_digest"], manifest["digest"])
            expected_files_digest = hashlib.sha256(
                json.dumps(sorted(manifest["files"]), separators=(",", ":")).encode()
            ).hexdigest()
            self.assertEqual(values["route_files_sha256"], expected_files_digest)

            receipt_blocks = _workflow_python_blocks(
                RECOVERY_BUILD_WORKFLOW, "Write exact non-deployable route skip receipt"
            )
            self.assertEqual(len(receipt_blocks), 1)
            receipt_path = Path(temporary) / "receipt.json"
            receipt_environment = {
                **os.environ,
                "SOURCE_SHA": self.TARGET_SHA,
                "SECURITY_RUN_ID": "77",
                "SECURITY_RUN_ATTEMPT": "3",
                "RECOVERY_BUILD_RUN_ID": "401",
                "RECOVERY_BUILD_RUN_ATTEMPT": "2",
                "ROUTE_FAMILY": values["route_family"],
                "CLASSIFIER_DIGEST": values["classifier_digest"],
                "ROUTE_FILES_SHA256": values["route_files_sha256"],
            }
            receipt_created = subprocess.run(
                [sys.executable, "-I", "-", str(receipt_path)],
                input=receipt_blocks[0], text=True, capture_output=True,
                check=False, env=receipt_environment,
            )
            self.assertEqual(receipt_created.returncode, 0, receipt_created.stderr)
            emitted_receipt = json.loads(receipt_path.read_text(encoding="ascii"))
            self.assertEqual(emitted_receipt["schema"], 2)
            self.assertEqual(emitted_receipt["kind"], "platform-recovery-bootstrap-skip")
            self.assertEqual(emitted_receipt["route_family"], "storage-operations")
            self.assertEqual(emitted_receipt["security_run_id"], "77")
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)

            changed = dict(manifest)
            changed["files"] = [*manifest["files"], "platform/tools/unreviewed.py"]
            changed["digest"] = manifest_digest(changed)
            encoded_changed = base64.b64encode(
                json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()
            ).decode("ascii")
            rejected = subprocess.run(
                [sys.executable, "-I", "-", encoded_changed, str(output), self.TARGET_SHA],
                input=blocks[0],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)

    def test_recovery_producer_docs_only_emits_authenticated_noop_not_bundle(self) -> None:
        recovery_workflow = RECOVERY_BUILD_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("--allow-out-of-scope", recovery_workflow)
        self.assertIn("--allow-candidate-packaging", recovery_workflow)
        route_blocks = _workflow_python_blocks(
            RECOVERY_BUILD_WORKFLOW,
            "Download and validate exact security route artifact",
        )
        self.assertEqual(len(route_blocks), 1)
        docs_manifest = classify(
            ["platform/docs/operations-runbook.md"],
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        docs_encoded = base64.b64encode(
            json.dumps(docs_manifest, sort_keys=True, separators=(",", ":")).encode()
        ).decode("ascii")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "docs-output"
            docs_run = subprocess.run(
                [sys.executable, "-I", "-", docs_encoded, str(output), self.TARGET_SHA],
                input=route_blocks[0], text=True, capture_output=True, check=False,
            )
            self.assertEqual(docs_run.returncode, 0, docs_run.stderr)
            docs_outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(docs_outputs["route_skip"], "true")
            self.assertEqual(docs_outputs["route_family"], "docs-only")
            self.assertEqual(docs_outputs["classifier_digest"], docs_manifest["digest"])

            receipt_blocks = _workflow_python_blocks(
                RECOVERY_BUILD_WORKFLOW,
                "Write exact non-deployable route skip receipt",
            )
            receipt_path = root / "docs-receipt.json"
            receipt_env = {
                **os.environ,
                "SOURCE_SHA": self.TARGET_SHA,
                "SECURITY_RUN_ID": "77",
                "SECURITY_RUN_ATTEMPT": "3",
                "RECOVERY_BUILD_RUN_ID": "401",
                "RECOVERY_BUILD_RUN_ATTEMPT": "2",
                "ROUTE_FAMILY": "docs-only",
                "CLASSIFIER_DIGEST": docs_outputs["classifier_digest"],
                "ROUTE_FILES_SHA256": docs_outputs["route_files_sha256"],
            }
            receipt_run = subprocess.run(
                [sys.executable, "-I", "-", str(receipt_path)],
                input=receipt_blocks[0], text=True, capture_output=True,
                check=False, env=receipt_env,
            )
            self.assertEqual(receipt_run.returncode, 0, receipt_run.stderr)
            receipt = json.loads(receipt_path.read_text(encoding="ascii"))
            self.assertEqual(receipt["route_family"], "docs-only")
            self.assertEqual(receipt["classifier_digest"], docs_manifest["digest"])
            self.assertEqual(receipt["route_files_sha256"], docs_outputs["route_files_sha256"])
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)

            out_of_scope_manifest = classify(
                ["README.md", "tools/maintenance.py"],
                event="push",
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            out_of_scope_encoded = base64.b64encode(
                json.dumps(
                    out_of_scope_manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).decode("ascii")
            out_of_scope_output = root / "out-of-scope-output"
            out_of_scope_run = subprocess.run(
                [sys.executable, "-I", "-", out_of_scope_encoded, str(out_of_scope_output), self.TARGET_SHA],
                input=route_blocks[0],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(out_of_scope_run.returncode, 0, out_of_scope_run.stderr)
            out_of_scope_values = dict(
                line.split("=", 1) for line in out_of_scope_output.read_text().splitlines()
            )
            self.assertEqual(out_of_scope_values["route_skip"], "true")
            self.assertEqual(out_of_scope_values["route_family"], "out-of-scope")
            self.assertEqual(
                out_of_scope_values["classifier_digest"], out_of_scope_manifest["digest"]
            )
            out_of_scope_receipt_path = root / "out-of-scope-receipt.json"
            out_of_scope_env = {
                **os.environ,
                "SOURCE_SHA": self.TARGET_SHA,
                "SECURITY_RUN_ID": "77",
                "SECURITY_RUN_ATTEMPT": "3",
                "RECOVERY_BUILD_RUN_ID": "401",
                "RECOVERY_BUILD_RUN_ATTEMPT": "2",
                "ROUTE_FAMILY": "out-of-scope",
                "CLASSIFIER_DIGEST": out_of_scope_values["classifier_digest"],
                "ROUTE_FILES_SHA256": out_of_scope_values["route_files_sha256"],
            }
            out_of_scope_receipt_created = subprocess.run(
                [sys.executable, "-I", "-", str(out_of_scope_receipt_path)],
                input=receipt_blocks[0],
                text=True,
                capture_output=True,
                check=False,
                env=out_of_scope_env,
            )
            self.assertEqual(
                out_of_scope_receipt_created.returncode,
                0,
                out_of_scope_receipt_created.stderr,
            )
            out_of_scope_receipt = json.loads(
                out_of_scope_receipt_path.read_text(encoding="ascii")
            )
            self.assertEqual(out_of_scope_receipt["route_family"], "out-of-scope")
            self.assertEqual(
                out_of_scope_receipt["classifier_digest"], out_of_scope_manifest["digest"]
            )
            self.assertEqual(
                stat.S_IMODE(out_of_scope_receipt_path.stat().st_mode), 0o600
            )

            candidate_manifest = classify(
                sorted(CANDIDATE_PACKAGING_FILES),
                event="push",
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            candidate_encoded = base64.b64encode(
                json.dumps(
                    candidate_manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).decode("ascii")
            candidate_output = root / "candidate-output"
            candidate_run = subprocess.run(
                [sys.executable, "-I", "-", candidate_encoded, str(candidate_output), self.TARGET_SHA],
                input=route_blocks[0],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(candidate_run.returncode, 0, candidate_run.stderr)
            candidate_values = dict(
                line.split("=", 1)
                for line in candidate_output.read_text(encoding="ascii").splitlines()
            )
            self.assertEqual(candidate_values["route_skip"], "true")
            self.assertEqual(candidate_values["route_family"], "candidate-packaging")
            self.assertEqual(candidate_values["classifier_digest"], candidate_manifest["digest"])
            candidate_receipt_path = root / "candidate-receipt.json"
            candidate_env = {
                **os.environ,
                "SOURCE_SHA": self.TARGET_SHA,
                "SECURITY_RUN_ID": "77",
                "SECURITY_RUN_ATTEMPT": "3",
                "RECOVERY_BUILD_RUN_ID": "401",
                "RECOVERY_BUILD_RUN_ATTEMPT": "2",
                "ROUTE_FAMILY": "candidate-packaging",
                "CLASSIFIER_DIGEST": candidate_values["classifier_digest"],
                "ROUTE_FILES_SHA256": candidate_values["route_files_sha256"],
            }
            candidate_receipt_run = subprocess.run(
                [sys.executable, "-I", "-", str(candidate_receipt_path)],
                input=receipt_blocks[0],
                text=True,
                capture_output=True,
                check=False,
                env=candidate_env,
            )
            self.assertEqual(
                candidate_receipt_run.returncode, 0, candidate_receipt_run.stderr
            )
            candidate_receipt = json.loads(
                candidate_receipt_path.read_text(encoding="ascii")
            )
            self.assertEqual(candidate_receipt["route_family"], "candidate-packaging")
            self.assertEqual(
                candidate_receipt["classifier_digest"], candidate_manifest["digest"]
            )
            self.assertEqual(
                candidate_receipt["route_files_sha256"],
                candidate_values["route_files_sha256"],
            )
            self.assertEqual(stat.S_IMODE(candidate_receipt_path.stat().st_mode), 0o600)

            workflow = yaml.safe_load(RECOVERY_BUILD_WORKFLOW.read_text(encoding="utf-8"))
            steps = workflow["jobs"]["build"]["steps"]
            for name in (
                "Build and validate deterministic recovery bundle",
                "Attest immutable recovery bundle (producer B)",
                "Upload immutable recovery bundle",
            ):
                step = next(item for item in steps if item.get("name") == name)
                self.assertEqual(step["if"], "steps.route.outputs.route_skip != 'true'")
            for name in (
                "Write exact non-deployable route skip receipt",
                "Attest exact non-deployable route skip receipt",
                "Upload exact non-deployable route skip receipt",
            ):
                step = next(item for item in steps if item.get("name") == name)
                self.assertEqual(step["if"], "steps.route.outputs.route_skip == 'true'")
            self.assertIn(
                "--allow-candidate-packaging",
                next(
                    step["run"]
                    for step in steps
                    if step.get("name") == "Download and validate exact security route artifact"
                ),
            )

            mixed = classify(
                ["platform/docs/operations-runbook.md", "platform/tools/runtime.py"],
                event="push",
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            mixed_output = root / "mixed-output"
            mixed_encoded = base64.b64encode(
                json.dumps(mixed, sort_keys=True, separators=(",", ":")).encode()
            ).decode("ascii")
            mixed_run = subprocess.run(
                [sys.executable, "-I", "-", mixed_encoded, str(mixed_output), self.TARGET_SHA],
                input=route_blocks[0], text=True, capture_output=True, check=False,
            )
            self.assertEqual(mixed_run.returncode, 0, mixed_run.stderr)
            mixed_values = dict(line.split("=", 1) for line in mixed_output.read_text().splitlines())
            self.assertEqual(mixed_values["route_skip"], "false")

    def test_recovery_publisher_receipt_selection_is_closed(self) -> None:
        blocks = _workflow_python_blocks(
            RECOVERY_PUBLISH_WORKFLOW,
            "Fetch exact producer and publisher attempt metadata",
        )
        self.assertGreaterEqual(len(blocks), 2)
        selector = blocks[0]
        producer_id = 401
        attempt = 2
        source_sha = self.TARGET_SHA
        producer_sha = "b" * 40
        repository_id = 11223344
        receipt_name = (
            f"platform-recovery-bootstrap-skip-storage-operations-{source_sha}-77-3-"
            f"{producer_id}-{attempt}.json"
        )
        base_run = {
            "id": producer_id,
            "run_attempt": attempt,
            "status": "completed",
            "conclusion": "success",
            "event": "workflow_run",
            "head_branch": "dev",
            "head_sha": producer_sha,
            "name": "Platform production recovery bootstrap build",
            "path": ".github/workflows/platform-production-recovery-bootstrap-build.yml",
            "repository": {"id": repository_id, "full_name": "StrayForest/old_sparky"},
        }
        receipt_row = {
            "id": 9001,
            "name": receipt_name,
            "expired": False,
            "digest": "sha256:" + "b" * 64,
            "workflow_run": {
                "id": producer_id,
                "head_sha": producer_sha,
                "repository_id": repository_id,
                "head_repository_id": repository_id,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.touch()
            (root / "producer-run.json").write_text(json.dumps(base_run))
            (root / "producer-jobs.json").write_text(json.dumps({
                "total_count": 1,
                "jobs": [{"id": 5, "name": "Build retained-release recovery bootstrap evidence",
                          "run_id": producer_id, "run_attempt": attempt, "head_sha": producer_sha,
                          "status": "completed", "conclusion": "success"}],
            }))
            def select(rows: list[dict[str, object]]) -> subprocess.CompletedProcess[str]:
                (root / "producer-artifacts.json").write_text(
                    json.dumps({"total_count": len(rows), "artifacts": rows})
                )
                return subprocess.run(
                    [sys.executable, "-I", "-", str(root), str(output)],
                    input=selector,
                    text=True,
                    capture_output=True,
                    check=False,
                    env={
                        **os.environ,
                        "PRODUCER_RUN_ID": str(producer_id),
                        "PRODUCER_RUN_ATTEMPT": str(attempt),
                        "PRODUCER_WORKFLOW": "Platform production recovery bootstrap build",
                        "PRODUCER_WORKFLOW_PATH": ".github/workflows/platform-production-recovery-bootstrap-build.yml",
                        "REPOSITORY": "StrayForest/old_sparky",
                    },
                )
            selected = select([receipt_row])
            self.assertEqual(selected.returncode, 0, selected.stderr)
            selected_outputs = output.read_text(encoding="ascii")
            self.assertIn("route_skip=true", selected_outputs)
            self.assertIn("route_family=storage-operations", selected_outputs)
            self.assertIn("route_receipt_artifact_id=9001", selected_outputs)
            docs_receipt_row = json.loads(json.dumps(receipt_row))
            docs_receipt_row["name"] = receipt_name.replace(
                "skip-storage-operations-", "skip-docs-only-"
            )
            output.write_text("", encoding="ascii")
            docs_selected = select([docs_receipt_row])
            self.assertEqual(docs_selected.returncode, 0, docs_selected.stderr)
            self.assertIn("route_family=docs-only", output.read_text(encoding="ascii"))
            out_of_scope_receipt_row = json.loads(json.dumps(receipt_row))
            out_of_scope_receipt_row["name"] = receipt_name.replace(
                "skip-storage-operations-", "skip-out-of-scope-"
            )
            output.write_text("", encoding="ascii")
            out_of_scope_selected = select([out_of_scope_receipt_row])
            self.assertEqual(
                out_of_scope_selected.returncode, 0, out_of_scope_selected.stderr
            )
            self.assertIn(
                "route_family=out-of-scope", output.read_text(encoding="ascii")
            )
            candidate_receipt_row = json.loads(json.dumps(receipt_row))
            candidate_receipt_row["name"] = receipt_name.replace(
                "skip-storage-operations-", "skip-candidate-packaging-"
            )
            output.write_text("", encoding="ascii")
            candidate_selected = select([candidate_receipt_row])
            self.assertEqual(candidate_selected.returncode, 0, candidate_selected.stderr)
            self.assertIn(
                "route_family=candidate-packaging", output.read_text(encoding="ascii")
            )
            wrong_attempt_name = json.loads(json.dumps(receipt_row))
            wrong_attempt_name["name"] = wrong_attempt_name["name"].rsplit("-", 1)[0] + "-3.json"
            self.assertNotEqual(select([wrong_attempt_name]).returncode, 0)
            base_run["run_attempt"] = attempt + 1
            (root / "producer-run.json").write_text(json.dumps(base_run))
            self.assertNotEqual(select([receipt_row]).returncode, 0)
            base_run["run_attempt"] = attempt
            (root / "producer-run.json").write_text(json.dumps(base_run))

            output.write_text("", encoding="ascii")
            bundle_row = {
                "id": 9003,
                "name": f"platform-recovery-bootstrap-{source_sha}-77-3-{producer_id}-{attempt}.zip",
                "expired": False,
                "digest": "sha256:" + "c" * 64,
                "workflow_run": receipt_row["workflow_run"],
            }
            ordinary = select([bundle_row])
            self.assertEqual(ordinary.returncode, 0, ordinary.stderr)
            self.assertIn("route_skip=false", output.read_text(encoding="ascii"))

            stale_receipt = {
                **receipt_row,
                "name": f"platform-recovery-bootstrap-skip-storage-operations-{source_sha}-76-2-{producer_id}-1.json",
            "workflow_run": {
                "id": producer_id,
                "head_sha": producer_sha,
                "repository_id": repository_id,
                "head_repository_id": repository_id,
                },
            }
            output.write_text("", encoding="ascii")
            prior_receipt_with_bundle = select([stale_receipt, bundle_row])
            self.assertEqual(prior_receipt_with_bundle.returncode, 0, prior_receipt_with_bundle.stderr)
            self.assertIn("route_skip=false", output.read_text(encoding="ascii"))
            output.write_text("", encoding="ascii")
            prior_and_current_receipt = select([stale_receipt, receipt_row])
            self.assertEqual(prior_and_current_receipt.returncode, 0, prior_and_current_receipt.stderr)
            self.assertIn("route_skip=true", output.read_text(encoding="ascii"))

            for invalid_rows in ([receipt_row, receipt_row], [receipt_row, {
                "id": 9002,
                "name": f"platform-recovery-bootstrap-{source_sha}-77-3-{producer_id}-{attempt}.zip",
            }]):
                rejected = select(invalid_rows)
                self.assertNotEqual(rejected.returncode, 0)

            for misbound_field, misbound_value in (
                ("repository_id", repository_id + 1),
                ("head_repository_id", repository_id + 1),
                ("run_attempt", attempt + 1),
            ):
                bad_receipt = json.loads(json.dumps(receipt_row))
                bad_receipt["workflow_run"][misbound_field] = misbound_value
                self.assertNotEqual(select([bad_receipt]).returncode, 0)
            missing_repository = json.loads(json.dumps(receipt_row))
            del missing_repository["workflow_run"]["repository_id"]
            self.assertNotEqual(select([missing_repository]).returncode, 0)
            missing_head_repository = json.loads(json.dumps(receipt_row))
            del missing_head_repository["workflow_run"]["head_repository_id"]
            self.assertNotEqual(select([missing_head_repository]).returncode, 0)

            missing = select([])
            self.assertNotEqual(missing.returncode, 0)

    def test_recovery_publisher_receipt_and_classifier_proof_reject_tampering(self) -> None:
        recovery_publisher_workflow = RECOVERY_PUBLISH_WORKFLOW.read_text(
            encoding="utf-8"
        )
        self.assertIn("--require-out-of-scope", recovery_publisher_workflow)
        self.assertIn("--require-candidate-packaging", recovery_publisher_workflow)
        blocks = _workflow_python_blocks(
            RECOVERY_PUBLISH_WORKFLOW,
            "Authenticate route skip receipt and parent classifier proof",
        )
        self.assertEqual(len(blocks), 3)
        self.assertEqual(
            _python_set_assignment(blocks[2], "allowed"),
            set(STORAGE_OPERATIONS_FILES),
        )
        self.assertEqual(
            _python_set_assignment(blocks[2], "triggers"),
            set(STORAGE_OPERATIONS_TRIGGER_FILES),
        )
        manifest = classify(
            sorted(STORAGE_OPERATIONS_FILES),
            event="push",
            target_sha=self.TARGET_SHA,
            branch="dev",
        )
        receipt = {
            "schema": 2,
            "kind": "platform-recovery-bootstrap-skip",
            "route_family": "storage-operations",
            "source_sha": self.TARGET_SHA,
            "security_run_id": "77",
            "security_run_attempt": "3",
            "producer_run_id": "401",
            "producer_run_attempt": "2",
            "classifier_digest": manifest["digest"],
            "route_files_sha256": hashlib.sha256(
                json.dumps(sorted(manifest["files"]), separators=(",", ":")).encode()
            ).hexdigest(),
        }
        receipt_name = f"platform-recovery-bootstrap-skip-storage-operations-{self.TARGET_SHA}-77-3-401-2.json"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.touch()
            def receipt_check(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
                zip_path = root / "receipt.zip"
                info = zipfile.ZipInfo(receipt_name)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
                    archive.writestr(info, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
                return subprocess.run(
                    [sys.executable, "-I", "-", str(zip_path), receipt_name, "401", "2", "storage-operations", str(output)],
                    input=blocks[0], text=True, capture_output=True, check=False,
                )
            accepted = receipt_check(receipt)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            tampered_receipt = dict(receipt, route_family="docs-only")
            self.assertNotEqual(receipt_check(tampered_receipt).returncode, 0)

            encoded = base64.b64encode(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            ).decode("ascii")
            environment = {
                **os.environ,
                "SOURCE_SHA": self.TARGET_SHA,
                "ROUTE_FAMILY": "storage-operations",
                "CLASSIFIER_DIGEST": str(receipt["classifier_digest"]),
                "ROUTE_FILES_SHA256": str(receipt["route_files_sha256"]),
            }
            proof = subprocess.run(
                [sys.executable, "-I", "-", encoded, str(output), "storage-operations"],
                input=blocks[2], text=True, capture_output=True, check=False, env=environment,
            )
            self.assertEqual(proof.returncode, 0, proof.stderr)
            tampered = dict(manifest, runtime_sensitive=False)
            tampered["digest"] = manifest_digest(tampered)
            tampered_encoded = base64.b64encode(
                json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()
            ).decode("ascii")
            rejected = subprocess.run(
                [sys.executable, "-I", "-", tampered_encoded, str(output), "storage-operations"],
                input=blocks[2], text=True, capture_output=True, check=False, env=environment,
            )
            self.assertNotEqual(rejected.returncode, 0)

            docs_manifest = classify(
                ["platform/docs/operations-runbook.md"],
                event="push",
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            docs_receipt = {
                **receipt,
                "route_family": "docs-only",
                "classifier_digest": docs_manifest["digest"],
                "route_files_sha256": hashlib.sha256(
                    json.dumps(sorted(docs_manifest["files"]), separators=(",", ":")).encode()
                ).hexdigest(),
            }
            docs_receipt_name = f"platform-recovery-bootstrap-skip-docs-only-{self.TARGET_SHA}-77-3-401-2.json"
            docs_selected = subprocess.run(
                [sys.executable, "-I", "-", str(root / "docs-receipt.zip"), docs_receipt_name, "401", "2", "docs-only", str(output)],
                input=blocks[0], text=True, capture_output=True, check=False,
            )
            # The independently exercised parser must reject a missing archive;
            # now supply the exact one-member docs receipt and check it accepts.
            self.assertNotEqual(docs_selected.returncode, 0)
            docs_info = zipfile.ZipInfo(docs_receipt_name)
            docs_info.create_system = 3
            docs_info.external_attr = (stat.S_IFREG | 0o600) << 16
            with zipfile.ZipFile(root / "docs-receipt.zip", "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr(docs_info, json.dumps(docs_receipt, sort_keys=True, separators=(",", ":")).encode())
            docs_selected = subprocess.run(
                [sys.executable, "-I", "-", str(root / "docs-receipt.zip"), docs_receipt_name, "401", "2", "docs-only", str(output)],
                input=blocks[0], text=True, capture_output=True, check=False,
            )
            self.assertEqual(docs_selected.returncode, 0, docs_selected.stderr)

            docs_encoded = base64.b64encode(
                json.dumps(docs_manifest, sort_keys=True, separators=(",", ":")).encode()
            ).decode("ascii")
            docs_env = {
                **environment,
                "ROUTE_FAMILY": "docs-only",
                "CLASSIFIER_DIGEST": str(docs_manifest["digest"]),
                "ROUTE_FILES_SHA256": str(docs_receipt["route_files_sha256"]),
            }
            docs_proof = subprocess.run(
                [sys.executable, "-I", "-", docs_encoded, str(output), "docs-only"],
                input=blocks[2], text=True, capture_output=True, check=False, env=docs_env,
            )
            self.assertEqual(docs_proof.returncode, 0, docs_proof.stderr)

            out_of_scope_manifest = classify(
                ["README.md", "tools/maintenance.py"],
                event="push",
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            out_of_scope_encoded = base64.b64encode(
                json.dumps(
                    out_of_scope_manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).decode("ascii")
            out_of_scope_receipt = {
                **receipt,
                "route_family": "out-of-scope",
                "classifier_digest": out_of_scope_manifest["digest"],
                "route_files_sha256": hashlib.sha256(
                    json.dumps(
                        sorted(out_of_scope_manifest["files"]),
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            }
            out_of_scope_receipt_name = (
                f"platform-recovery-bootstrap-skip-out-of-scope-"
                f"{self.TARGET_SHA}-77-3-401-2.json"
            )
            out_of_scope_receipt_zip = root / "out-of-scope-receipt.zip"
            out_of_scope_info = zipfile.ZipInfo(out_of_scope_receipt_name)
            out_of_scope_info.create_system = 3
            out_of_scope_info.external_attr = (stat.S_IFREG | 0o600) << 16
            with zipfile.ZipFile(
                out_of_scope_receipt_zip, "w", compression=zipfile.ZIP_STORED
            ) as archive:
                archive.writestr(
                    out_of_scope_info,
                    json.dumps(
                        out_of_scope_receipt,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode(),
                )
            out_of_scope_receipt_check = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-",
                    str(out_of_scope_receipt_zip),
                    out_of_scope_receipt_name,
                    "401",
                    "2",
                    "out-of-scope",
                    str(output),
                ],
                input=blocks[0],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                out_of_scope_receipt_check.returncode,
                0,
                out_of_scope_receipt_check.stderr,
            )
            out_of_scope_env = {
                **environment,
                "ROUTE_FAMILY": "out-of-scope",
                "CLASSIFIER_DIGEST": out_of_scope_manifest["digest"],
                "ROUTE_FILES_SHA256": out_of_scope_receipt[
                    "route_files_sha256"
                ],
            }
            out_of_scope_proof = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-",
                    out_of_scope_encoded,
                    str(output),
                    "out-of-scope",
                ],
                input=blocks[2],
                text=True,
                capture_output=True,
                check=False,
                env=out_of_scope_env,
            )
            self.assertEqual(
                out_of_scope_proof.returncode, 0, out_of_scope_proof.stderr
            )
            self.assertIn(
                "route_authenticated=true", output.read_text(encoding="ascii")
            )
            candidate_manifest = classify(
                sorted(CANDIDATE_PACKAGING_FILES),
                event="push",
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            candidate_encoded = base64.b64encode(
                json.dumps(
                    candidate_manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).decode("ascii")
            candidate_receipt = {
                **receipt,
                "route_family": "candidate-packaging",
                "classifier_digest": candidate_manifest["digest"],
                "route_files_sha256": hashlib.sha256(
                    json.dumps(
                        sorted(candidate_manifest["files"]), separators=(",", ":")
                    ).encode()
                ).hexdigest(),
            }
            candidate_receipt_name = (
                f"platform-recovery-bootstrap-skip-candidate-packaging-"
                f"{self.TARGET_SHA}-77-3-401-2.json"
            )
            candidate_receipt_zip = root / "candidate-receipt.zip"
            candidate_info = zipfile.ZipInfo(candidate_receipt_name)
            candidate_info.create_system = 3
            candidate_info.external_attr = (stat.S_IFREG | 0o600) << 16
            with zipfile.ZipFile(
                candidate_receipt_zip, "w", compression=zipfile.ZIP_STORED
            ) as archive:
                archive.writestr(
                    candidate_info,
                    json.dumps(
                        candidate_receipt,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode(),
                )
            candidate_receipt_check = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-",
                    str(candidate_receipt_zip),
                    candidate_receipt_name,
                    "401",
                    "2",
                    "candidate-packaging",
                    str(output),
                ],
                input=blocks[0],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                candidate_receipt_check.returncode,
                0,
                candidate_receipt_check.stderr,
            )
            candidate_env = {
                **environment,
                "ROUTE_FAMILY": "candidate-packaging",
                "CLASSIFIER_DIGEST": candidate_manifest["digest"],
                "ROUTE_FILES_SHA256": candidate_receipt["route_files_sha256"],
            }
            candidate_proof = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-",
                    candidate_encoded,
                    str(output),
                    "candidate-packaging",
                ],
                input=blocks[2],
                text=True,
                capture_output=True,
                check=False,
                env=candidate_env,
            )
            self.assertEqual(candidate_proof.returncode, 0, candidate_proof.stderr)
            self.assertIn("route_authenticated=true", output.read_text(encoding="ascii"))
            self.assertIn("--require-out-of-scope", RECOVERY_PUBLISH_WORKFLOW.read_text(encoding="utf-8"))
            bad_docs_digest = subprocess.run(
                [sys.executable, "-I", "-", docs_encoded, str(output), "docs-only"],
                input=blocks[2], text=True, capture_output=True, check=False,
                env={**docs_env, "CLASSIFIER_DIGEST": "0" * 64},
            )
            self.assertNotEqual(bad_docs_digest.returncode, 0)
            bad_docs_files = subprocess.run(
                [sys.executable, "-I", "-", docs_encoded, str(output), "docs-only"],
                input=blocks[2], text=True, capture_output=True, check=False,
                env={**docs_env, "ROUTE_FILES_SHA256": "0" * 64},
            )
            self.assertNotEqual(bad_docs_files.returncode, 0)
            mixed_manifest = classify(
                ["platform/docs/operations-runbook.md", "platform/tools/runtime.py"],
                event="push",
                target_sha=self.TARGET_SHA,
                branch="dev",
            )
            mixed_encoded = base64.b64encode(
                json.dumps(mixed_manifest, sort_keys=True, separators=(",", ":")).encode()
            ).decode("ascii")
            self.assertNotEqual(
                subprocess.run(
                    [sys.executable, "-I", "-", mixed_encoded, str(output), "docs-only"],
                    input=blocks[2], text=True, capture_output=True, check=False, env=docs_env,
                ).returncode,
                0,
            )

            parent_run = {
                "id": 77,
                "run_attempt": 3,
                "head_sha": self.TARGET_SHA,
                "head_branch": "dev",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "name": "Platform security and build",
                "path": ".github/workflows/platform-security.yml",
                "repository": {"id": 11223344, "full_name": "StrayForest/old_sparky"},
            }
            parent_job = {
                "id": 900,
                "name": "Verification contract",
                "run_id": 77,
                "run_attempt": 3,
                "head_sha": self.TARGET_SHA,
                "status": "completed",
                "conclusion": "success",
            }
            parent_artifact = {
                "id": 901,
                "name": "platform-ci-route-77-3",
                "expired": False,
                "digest": "sha256:" + "d" * 64,
                "workflow_run": {
                    "id": 77,
                    "head_sha": self.TARGET_SHA,
                    "repository_id": 11223344,
                    "head_repository_id": 11223344,
                },
            }
            (root / "route-security-run.json").write_text(json.dumps(parent_run))
            (root / "route-security-jobs.json").write_text(json.dumps({"total_count": 1, "jobs": [parent_job]}))
            (root / "route-security-artifacts.json").write_text(json.dumps({"total_count": 1, "artifacts": [parent_artifact]}))
            parent_check = subprocess.run(
                [sys.executable, "-I", "-", str(root), str(output), "StrayForest/old_sparky", "77", "3", self.TARGET_SHA],
                input=blocks[1], text=True, capture_output=True, check=False,
            )
            self.assertEqual(parent_check.returncode, 0, parent_check.stderr)
            self.assertIn("route_artifact_id=901", output.read_text(encoding="ascii"))
            parent_artifact["name"] = "platform-ci-route-77-4"
            (root / "route-security-artifacts.json").write_text(json.dumps({"total_count": 1, "artifacts": [parent_artifact]}))
            parent_name_rejected = subprocess.run(
                [sys.executable, "-I", "-", str(root), str(output), "StrayForest/old_sparky", "77", "3", self.TARGET_SHA],
                input=blocks[1], text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(parent_name_rejected.returncode, 0)
            parent_artifact["name"] = "platform-ci-route-77-3"
            (root / "route-security-artifacts.json").write_text(json.dumps({"total_count": 1, "artifacts": [parent_artifact]}))
            parent_run["run_attempt"] = 4
            (root / "route-security-run.json").write_text(json.dumps(parent_run))
            parent_api_attempt_rejected = subprocess.run(
                [sys.executable, "-I", "-", str(root), str(output), "StrayForest/old_sparky", "77", "3", self.TARGET_SHA],
                input=blocks[1], text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(parent_api_attempt_rejected.returncode, 0)
            parent_run["run_attempt"] = 3
            (root / "route-security-run.json").write_text(json.dumps(parent_run))
            parent_artifact["workflow_run"]["repository_id"] = 11223345
            (root / "route-security-artifacts.json").write_text(json.dumps({"total_count": 1, "artifacts": [parent_artifact]}))
            parent_repo_rejected = subprocess.run(
                [sys.executable, "-I", "-", str(root), str(output), "StrayForest/old_sparky", "77", "3", self.TARGET_SHA],
                input=blocks[1], text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(parent_repo_rejected.returncode, 0)
            parent_artifact["workflow_run"]["repository_id"] = 11223344
            (root / "route-security-artifacts.json").write_text(json.dumps({"total_count": 1, "artifacts": [parent_artifact]}))
            parent_artifact["workflow_run"]["run_attempt"] = 4
            (root / "route-security-artifacts.json").write_text(json.dumps({"total_count": 1, "artifacts": [parent_artifact]}))
            parent_attempt_rejected = subprocess.run(
                [sys.executable, "-I", "-", str(root), str(output), "StrayForest/old_sparky", "77", "3", self.TARGET_SHA],
                input=blocks[1], text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(parent_attempt_rejected.returncode, 0)
            del parent_artifact["workflow_run"]["run_attempt"]
            (root / "route-security-artifacts.json").write_text(json.dumps({"total_count": 1, "artifacts": [parent_artifact]}))
            parent_run["conclusion"] = "failure"
            (root / "route-security-run.json").write_text(json.dumps(parent_run))
            parent_rejected = subprocess.run(
                [sys.executable, "-I", "-", str(root), str(output), "StrayForest/old_sparky", "77", "3", self.TARGET_SHA],
                input=blocks[1], text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(parent_rejected.returncode, 0)
            parent_run["conclusion"] = "success"
            parent_run["head_sha"] = "c" * 40
            (root / "route-security-run.json").write_text(json.dumps(parent_run))
            misbound = subprocess.run(
                [sys.executable, "-I", "-", str(root), str(output), "StrayForest/old_sparky", "77", "3", self.TARGET_SHA],
                input=blocks[1], text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(misbound.returncode, 0)


if __name__ == "__main__":
    unittest.main()
