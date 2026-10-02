from __future__ import annotations

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


REPO_ROOT = Path(__file__).resolve().parents[2]
SECURITY_WORKFLOW = REPO_ROOT / ".github/workflows/platform-security.yml"
AUTO_DEPLOY_WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-autodeploy.yml"
PRODUCTION_WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
STATUS_FINALIZER_WORKFLOW = REPO_ROOT / ".github/workflows/platform-security-status-finalizer.yml"


def _workflow_step_script(workflow: str, name: str) -> str:
    step = re.search(
        rf"(?ms)^      - name: {re.escape(name)}\n(?P<body>.*?)(?=^      - name: |^  [A-Za-z0-9_-]+:|\Z)",
        workflow,
    )
    if step is None:
        raise AssertionError(f"workflow step is missing: {name}")
    run = re.search(r"(?ms)^        run: \|\n(?P<script>.*)", step.group("body"))
    if run is None:
        raise AssertionError(f"workflow step has no shell script: {name}")
    return textwrap.dedent(run.group("script"))


def _run_security_api_fixture(
    script: str,
    mode: str,
    *,
    final_status: bool = False,
) -> tuple[subprocess.CompletedProcess[str], tuple[str, ...], str]:
    fake_curl = """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
Path(os.environ["FAKE_CURL_ARGS"]).write_text("\\n".join(args), encoding="utf-8")
output = Path(args[args.index("--output") + 1])
url = next(item for item in args if item.startswith("https://api.github.com/"))
mode = os.environ["FAKE_CURL_MODE"]
if mode == "hang": time.sleep(0.05); raise SystemExit(28)
if mode == "write_failure": raise SystemExit(23)
if mode == "oversize": body = b"x" * 65537
elif mode == "http_error": body = b"SYNTHETIC_RESPONSE_SECRET"
elif mode == "malformed": body = b"not-json"
elif "/git/ref/heads/" in url:
    body = json.dumps({"ref": "refs/heads/dev", "object": {"type": "commit", "sha": "a" * 40}}).encode()
else:
    body = b"{}"
output.write_bytes(body)
raise SystemExit(22 if mode == "http_error" else 0)
"""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        runner_temp = root / "runner-temp"
        runner_temp.mkdir()
        workspace = root / "workspace"
        workspace.mkdir()
        fake = root / "curl"
        fake.write_text(fake_curl, encoding="utf-8")
        fake.chmod(0o700)
        args_path = root / "curl-args"
        environment = {
            **os.environ,
            "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_CURL_MODE": mode,
            "FAKE_CURL_ARGS": str(args_path),
            "GITHUB_REPOSITORY": "StrayForest/old_sparky",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_API_URL": "https://api.github.com",
            "GH_TOKEN": "synthetic-token",
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(root / "github-output"),
            "GITHUB_WORKSPACE": str(workspace),
            "GITHUB_RUN_ID": "12345",
            "GITHUB_RUN_ATTEMPT": "1",
            "TRUSTED_BRANCH": "dev",
            "TESTED_SHA": "a" * 40,
        }
        if final_status:
            environment.update(
                {
                    "EVENT_NAME": "push",
                    "WORKFLOW_REF": "refs/heads/dev",
                    "CLASSIFIER_RESULT": "success",
                    "ROUTE_CLASS": "docs-only",
                    "ROUTE_EVENT": "push",
                    "ROUTE_DEPLOYABLE": "false",
                    "ROUTE_FALLBACK": "false",
                    "ROUTE_RUNTIME_SENSITIVE": "false",
                    "ROUTE_TARGET_SHA": "a" * 40,
                    "ROUTE_DIGEST": "b" * 64,
                    "ROUTE_REASON": "synthetic route",
                    "EXPECTED_GATES": '["docs", "verification-contract"]',
                    "STATUS_START_RESULT": "success",
                    "DOCS_RESULT": "success",
                    "VERIFICATION_CONTRACT_RESULT": "success",
                }
            )
            for name in (
                "BACKEND_RESULT", "PYTHON_QUALITY_RESULT", "SECURITY_RESULT",
                "WEB_QUALITY_RESULT", "WEB_HERMETIC_RESULT", "MIGRATION_RESULT",
                "RELEASE_RUNTIME_RESULT", "RELEASE_RUNTIME_REAL_RESULT",
            ):
                environment[name] = "skipped"
        completed = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
        leftovers = tuple(path.name for path in runner_temp.iterdir())
        args = args_path.read_text(encoding="utf-8") if args_path.exists() else ""
        return completed, leftovers, args

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

        def assert_api_transport_contract(source: str) -> None:
            self.assertEqual(source.count("curl --fail-with-body"), 3)
            self.assertNotIn("set -x", source)
            self.assertEqual(source.count("$GH_TOKEN"), 3)
            for step_name, response, byte_count in (
                (
                    "Resolve immutable default-branch classifier source",
                    "trusted_ref",
                    "trusted_ref_bytes",
                ),
                ("Mark platform security build pending", "status_start_response", "status_start_response_bytes"),
                ("Report platform security build result", "status_final_response", "status_final_response_bytes"),
            ):
                match = re.search(
                    rf"(?ms)^      - name: {re.escape(step_name)}\n(?P<body>.*?)(?=^      - name: |^  [A-Za-z0-9_-]+:\n|\Z)",
                    source,
                )
                self.assertIsNotNone(match, step_name)
                assert match is not None
                body = match.group("body")
                self.assertIn("if ! curl", body)
                self.assertIn("--connect-timeout 5", body)
                self.assertIn("--max-time 15", body)
                self.assertIn("--retry 0", body)
                self.assertIn("--max-filesize 65536", body)
                self.assertIn(f'mktemp "$RUNNER_TEMP/', body)
                self.assertIn(f'trap \'rm -f -- "${response}"\' EXIT', body)
                self.assertIn(f'--output "${response}"', body)
                self.assertIn(f'wc -c < "${response}"', body)
                self.assertIn(f'(( {byte_count} > 65536 ))', body)
                self.assertNotIn(f'cat "${response}"', body)
                self.assertNotIn(f'echo "${response}"', body)
                self.assertIn('--header "Authorization: Bearer $GH_TOKEN"', body)

        assert_api_transport_contract(workflow)
        for label, mutated in (
            ("changed timeout", workflow.replace("--max-time 15", "--max-time 30", 1)),
            ("enabled retries", workflow.replace("--retry 0", "--retry 1", 1)),
            ("raised response cap", workflow.replace("--max-filesize 65536", "--max-filesize 65537", 1)),
            ("removed output binding", workflow.replace('--output "$trusted_ref"', '--output /dev/null', 1)),
            ("removed post-download check", workflow.replace('wc -c < "$trusted_ref"', "wc -c < /dev/null", 1)),
            ("token in endpoint", workflow.replace("${GITHUB_API_URL}", "${GITHUB_API_URL}/$GH_TOKEN", 1)),
        ):
            with self.subTest(mutation=label):
                with self.assertRaises(AssertionError):
                    assert_api_transport_contract(mutated)

        def assert_fixture_result(
            completed: subprocess.CompletedProcess[str],
            leftovers: tuple[str, ...],
            args: str,
            success: bool,
        ) -> None:
            self.assertEqual(completed.returncode == 0, success, completed.stderr)
            self.assertEqual(leftovers, ())
            self.assertNotIn("SYNTHETIC_RESPONSE_SECRET", completed.stdout + completed.stderr)
            url_args = [line for line in args.splitlines() if line.startswith("https://api.github.com/")]
            self.assertEqual(len(url_args), 1)
            self.assertNotIn("synthetic-token", url_args[0])

        trusted_script = _workflow_step_script(
            workflow,
            "Resolve immutable default-branch classifier source",
        )
        for mode in ("success", "hang", "oversize", "http_error", "malformed", "write_failure"):
            with self.subTest(trusted_fixture=mode):
                completed, leftovers, args = _run_security_api_fixture(trusted_script, mode)
                assert_fixture_result(completed, leftovers, args, mode == "success")

        for step_name, final_status in (
            ("Mark platform security build pending", False),
            ("Report platform security build result", True),
        ):
            script = _workflow_step_script(workflow, step_name)
            for mode in ("success", "hang", "oversize", "http_error", "write_failure"):
                with self.subTest(status_fixture=(step_name, mode)):
                    completed, leftovers, args = _run_security_api_fixture(
                        script,
                        mode,
                        final_status=final_status,
                    )
                    assert_fixture_result(completed, leftovers, args, mode == "success")

    def test_cancel_safe_status_finalizer_overwrites_every_terminal_conclusion(self) -> None:
        workflow = STATUS_FINALIZER_WORKFLOW.read_text(encoding="utf-8")
        security = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_run:", workflow)
        self.assertIn("workflows: [Platform security and build]", workflow)
        self.assertIn("types: [completed]", workflow)
        self.assertIn("if: ${{ always() }}", workflow)
        self.assertIn("permissions:\n      statuses: write", workflow)
        self.assertNotIn("actions/checkout", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertIn("TARGET_SHA: ${{ github.event.workflow_run.head_sha }}", workflow)
        self.assertIn("statuses/${TARGET_SHA}", workflow)
        self.assertIn("attempt_url=", workflow)
        self.assertIn("SOURCE_RUN_URL", workflow)
        self.assertIn("/attempts/{attempt}", workflow)
        self.assertNotIn("commits/${TARGET_SHA}/statuses?per_page=100", workflow)
        self.assertNotIn("preserve_success", workflow)
        self.assertNotIn("updated_at", workflow)
        self.assertNotIn("status_rows", workflow)
        self.assertNotIn("pagination", workflow)
        self.assertIn('case "$SOURCE_CONCLUSION" in', workflow)
        self.assertIn("success)", workflow)
        for conclusion in (
            "cancelled",
            "failure",
            "skipped",
            "timed_out",
            "action_required",
            "neutral",
            "stale",
            "startup_failure",
        ):
            self.assertIn(conclusion, workflow)
        self.assertIn("state=failure", workflow)
        self.assertIn('description="Platform security or build failed"', workflow)
        self.assertIn('description="Platform security and build passed"', workflow)
        self.assertIn('"context": "platform-security-build"', workflow)
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
        self.assertIn("needs: [classifier, status-start", security)

    def test_successful_reduced_routes_keep_canonical_status_and_noop_autodeploy(self) -> None:
        security = SECURITY_WORKFLOW.read_text(encoding="utf-8")
        auto = AUTO_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('description="Platform security and build passed"', security)
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
            'requires_release_runtime = runtime_sensitive or raw_fallback == "true"',
            status_final,
        )
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
                    "skipped",
                    "skipped",
                    True,
                    False,
                ),
                (
                    "non-dev manual fixture only",
                    "workflow_dispatch",
                    "refs/heads/feature",
                    "true",
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


if __name__ == "__main__":
    unittest.main()
