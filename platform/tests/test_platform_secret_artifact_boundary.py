"""Security contracts for credential-bearing owned workflows."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = REPO_ROOT / ".github/workflows"

OWNED_SECRET_WORKFLOWS = {
    "platform-cloudflare-readonly-audit.yml": ("audit",),
    "platform-draft-cloudflare.yml": ("release",),
    "platform-live-user-qa.yml": ("live-user-qa",),
    "platform-production-release-abort.yml": ("abort",),
    "platform-production-release-recover.yml": ("recover",),
    "platform-production-retained-load-abort.yml": ("abort",),
    "platform-production-retained-load-cleanup.yml": ("cleanup",),
    "platform-production-service-recovery.yml": ("recover-services",),
    "platform-production-storage-diagnostics.yml": ("collect",),
    "platform-production-storage-maintenance.yml": ("maintenance",),
}


def _job_blocks(source: str) -> dict[str, str]:
    matches = re.finditer(
        r"^  (?P<name>[A-Za-z0-9_-]+):\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        source,
        re.MULTILINE | re.DOTALL,
    )
    return {match.group("name"): match.group("body") for match in matches}


def _draft_job_steps(job: str) -> list[dict[str, object]]:
    document = yaml.safe_load(
        (WORKFLOW_ROOT / "platform-draft-cloudflare.yml").read_text(encoding="utf-8")
    )
    return document["jobs"][job]["steps"]


def _named_step(steps: list[dict[str, object]], name: str) -> dict[str, object]:
    return next(step for step in steps if step.get("name") == name)


class DraftReleaseSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="draft-selector-test-")
        self.addCleanup(self.scratch.cleanup)
        root = Path(self.scratch.name)
        self.repo = root / "working"
        self.remote = root / "remote.git"
        self._git("init", "-b", "dev", str(self.repo), cwd=root)
        self._git("config", "user.email", "draft-test@example.invalid")
        self._git("config", "user.name", "Draft Test")
        draft_source = self.repo / "platform/apps/platform_draft/worker.js"
        draft_source.parent.mkdir(parents=True)
        draft_source.write_text("// pending feature\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "Draft feature whose security run failed")
        self.feature_sha = self._git("rev-parse", "HEAD").stdout.strip()
        docs_source = self.repo / "platform/docs/unrelated.md"
        docs_source.parent.mkdir(parents=True)
        docs_source.write_text("later repair\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "Later non-Draft security repair")
        self.repaired_sha = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("init", "--bare", str(self.remote), cwd=root)
        self._git("remote", "add", "origin", str(self.remote))
        self._git("push", "origin", "dev")
        steps = _draft_job_steps("detect-release")
        self.selector = str(_named_step(steps, "Validate current Draft release source")["run"])

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=cwd or self.repo, check=True,
            capture_output=True, text=True,
        )

    def _select(self, **overrides: str) -> tuple[subprocess.CompletedProcess[str], str]:
        output = Path(self.scratch.name) / "selector-output"
        output.unlink(missing_ok=True)
        variables = {
            "GITHUB_EVENT_NAME": "workflow_run",
            "GITHUB_REF": "refs/heads/dev",
            "GITHUB_REPOSITORY": "StrayForest/old_sparky",
            "GITHUB_OUTPUT": str(output),
            "WORKFLOW_TARGET_SHA": self.repaired_sha,
            "SOURCE_RUN_STATUS": "completed",
            "SOURCE_RUN_CONCLUSION": "success",
            "SOURCE_RUN_EVENT": "push",
            "SOURCE_RUN_BRANCH": "dev",
            "SOURCE_RUN_REPOSITORY": "StrayForest/old_sparky",
            "SOURCE_RUN_EVENT_REPOSITORY": "StrayForest/old_sparky",
            "SOURCE_RUN_NAME": "Platform security and build",
            "SOURCE_RUN_WORKFLOW": ".github/workflows/platform-security.yml",
        }
        variables.update(overrides)
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", self.selector],
            cwd=self.repo, env={**os.environ, **variables},
            capture_output=True, text=True, check=False,
        )
        return result, output.read_text(encoding="utf-8") if output.exists() else ""

    def test_later_green_non_draft_source_publishes_pending_feature(self) -> None:
        # The feature's failed CI and a later overall-success/skipped Draft run
        # cannot establish what Worker bytes were published. The current source
        # must still reach the build and publish jobs.
        result, output = self._select()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, f"target_sha={self.repaired_sha}\nchanged=true\n")
        jobs = yaml.safe_load((WORKFLOW_ROOT / "platform-draft-cloudflare.yml").read_text(encoding="utf-8"))["jobs"]
        self.assertIn("needs.detect-release.outputs.changed == 'true'", jobs["build-release"]["if"])
        self.assertIn("needs.detect-release.outputs.changed == 'true'", jobs["release"]["if"])

    def test_stale_or_invalid_security_provenance_never_selects_release(self) -> None:
        invalid = (
            {"WORKFLOW_TARGET_SHA": "missing"},
            {"SOURCE_RUN_CONCLUSION": "failure"},
            {"SOURCE_RUN_STATUS": "in_progress"},
            {"SOURCE_RUN_EVENT": "pull_request"},
            {"SOURCE_RUN_BRANCH": "feature"},
            {"SOURCE_RUN_REPOSITORY": "other/repo"},
            {"SOURCE_RUN_EVENT_REPOSITORY": ""},
            {"SOURCE_RUN_NAME": "Other workflow"},
            {"SOURCE_RUN_WORKFLOW": ".github/workflows/other.yml"},
            {"GITHUB_REPOSITORY": "other/repo"},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                result, output = self._select(**changed)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output, "")

        (self.repo / "platform/docs/unrelated.md").write_text("new head\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "Advance dev after Security")
        self._git("push", "origin", "dev")
        self._git("checkout", "--detach", self.repaired_sha)
        result, output = self._select()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")

    def test_manual_force_uses_current_dev_only(self) -> None:
        manual = {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "WORKFLOW_TARGET_SHA": "",
            "SOURCE_RUN_STATUS": "",
            "SOURCE_RUN_CONCLUSION": "",
            "SOURCE_RUN_EVENT": "",
            "SOURCE_RUN_BRANCH": "",
            "SOURCE_RUN_REPOSITORY": "",
            "SOURCE_RUN_EVENT_REPOSITORY": "",
            "SOURCE_RUN_NAME": "",
            "SOURCE_RUN_WORKFLOW": "",
        }
        result, output = self._select(**manual)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, f"target_sha={self.repaired_sha}\nchanged=true\n")
        result, output = self._select(**manual, GITHUB_REF="refs/heads/feature")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")

    def test_bundle_sync_returns_to_repository_root_and_release_rechecks_dev(self) -> None:
        steps = _draft_job_steps("build-release")
        bundle = str(_named_step(steps, "Generate immutable Draft bundle")["run"])
        lines = bundle.splitlines()
        copy_start = next(i for i, line in enumerate(lines) if line.startswith('bundle="$RUNNER_TEMP/'))
        copy_end = next(i for i in range(copy_start, len(lines)) if lines[i].startswith("python -m venv"))
        isolated = Path(self.scratch.name) / "bundle-closure-fixture"
        draft = isolated / "platform/apps/platform_draft"
        (draft / "public").mkdir(parents=True)
        (draft / "dist/draft").mkdir(parents=True)
        (draft / "dist/draft/index.html").write_text("<!doctype html>\n", encoding="utf-8")
        for name in ("worker.js", "wrangler.jsonc"):
            shutil.copy2(REPO_ROOT / "platform/apps/platform_draft" / name, draft / name)
        for name in ("draft-core.js", "heroes.js"):
            shutil.copy2(REPO_ROOT / "platform/apps/platform_draft/public" / name, draft / "public" / name)
        runner_temp = isolated / "runner-temp"
        runner_temp.mkdir()
        copied = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", "set -euo pipefail\n" + "\n".join(lines[copy_start:copy_end])],
            cwd=isolated, env={**os.environ, "RUNNER_TEMP": str(runner_temp)},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(copied.returncode, 0, copied.stderr)
        staged = runner_temp / "draft-release-bundle"
        worker_load = ["node", "--input-type=module", "-e", "await import('./worker.js')"]
        resolved = subprocess.run(worker_load, cwd=staged, capture_output=True, text=True, check=False)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        (staged / "public/draft-core.js").unlink()
        rejected = subprocess.run(worker_load, cwd=staged, capture_output=True, text=True, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("draft-core.js", rejected.stderr)

        cd_line = next(i for i, line in enumerate(lines) if line.strip() == "cd platform")
        start = cd_line - (lines[cd_line - 1].strip() == "(")
        end = next(i for i in range(cd_line + 1, len(lines)) if lines[i].startswith('/usr/bin/python3 - "$bundle"'))
        snippet = "\n".join(lines[start:end])
        fixture = Path(self.scratch.name) / "bundle-fixture"
        interpreter = fixture / "platform/.venv_draft_assets/bin/python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        interpreter.chmod(0o755)
        lockfile = fixture / "platform/apps/platform_draft/package-lock.json"
        lockfile.parent.mkdir(parents=True)
        lockfile.write_text("{}\n", encoding="utf-8")
        (lockfile.parent / "node_modules").mkdir()
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", "set -euo pipefail\nbundle=/tmp\n" + snippet + "\ntest -f platform/apps/platform_draft/package-lock.json\ntest -d platform/apps/platform_draft/node_modules\n"],
            cwd=fixture, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        release_steps = _draft_job_steps("release")
        names = [step.get("name") for step in release_steps]
        self.assertLess(names.index("Refuse a stale Draft release before Cloudflare writes"), names.index("Require Cloudflare deployment credentials"))
        self.assertLess(names.index("Refuse a stale Draft release before Cloudflare writes"), names.index("Upload immutable hero thumbnails to existing R2 bucket"))
        self.assertIn("git ls-remote", str(_named_step(release_steps, "Deploy Worker, Static Assets and Durable Object")["run"]))


class SecretArtifactBoundaryTests(unittest.TestCase):
    def test_wrangler_runtime_size_exception_is_exact_in_builder_and_publisher(self) -> None:
        bundle_run = str(_named_step(_draft_job_steps("build-release"), "Generate immutable Draft bundle")["run"])
        builder_blocks = re.findall(r"<<'PY'\n(.*?)\nPY", bundle_run, re.DOTALL)
        self.assertEqual(len(builder_blocks), 2)
        publisher_run = str(_named_step(_draft_job_steps("release"), "Validate immutable Wrangler runtime")["run"])
        publisher_blocks = re.findall(r"<<'PY'\n(.*?)\nPY", publisher_run, re.DOTALL)
        self.assertEqual(len(publisher_blocks), 1)
        upload = _named_step(_draft_job_steps("build-release"), "Upload digest-bound Wrangler runtime")
        self.assertIs(upload["with"]["include-hidden-files"], True)
        draft_upload = _named_step(_draft_job_steps("build-release"), "Upload digest-bound Draft bundle")
        self.assertNotIn("include-hidden-files", draft_upload["with"])
        reviewed_digest = "c27608e7efe705b963aee2f3b2d5ef15ac7d7e51816f086836aac071ea59ad6a"
        self.assertEqual(builder_blocks[1].count(reviewed_digest), 1)

        with tempfile.TemporaryDirectory(prefix="draft-wrangler-cap-") as temporary:
            parent = Path(temporary)
            root = parent / "runtime"
            root.mkdir()
            cli = root / "node_modules/wrangler/wrangler-dist/cli.js"
            cli.parent.mkdir(parents=True)
            cli.write_bytes(b"pinned cli\n")
            (cli.parent / ".gitkeep").write_bytes(b"hidden manifest member\n")
            workerd = root / "node_modules/@cloudflare/workerd-linux-64/bin/workerd"
            workerd.parent.mkdir(parents=True)
            with workerd.open("wb") as output:
                output.truncate(65 * 1024 * 1024)
            executable_paths = (
                workerd,
                root / "node_modules/workerd/bin/workerd",
                root / "node_modules/@esbuild/linux-x64/bin/esbuild",
                root / "node_modules/esbuild/bin/esbuild",
            )
            for path in executable_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path != workerd:
                    path.write_bytes(b"pinned executable\n")
                path.chmod(0o755)
            for number in range(98):
                (root / f"member-{number:03d}").write_bytes(b"fixture\n")
            lockfile = parent / "package-lock.json"
            lockfile.write_bytes(b"{}\n")
            lock_digest = hashlib.sha256(lockfile.read_bytes()).hexdigest()

            def manifest() -> dict[str, object]:
                files = {}
                for path in sorted(root.rglob("*")):
                    if not path.is_file() or path.name == "wrangler-tool-manifest.json":
                        continue
                    digest = hashlib.sha256()
                    with path.open("rb") as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(chunk)
                    files[path.relative_to(root).as_posix()] = digest.hexdigest()
                payload = {
                    "schema": 1,
                    "package": "wrangler",
                    "version": "4.129.0",
                    "lockfile_sha256": lock_digest,
                    "files": files,
                }
                payload["digest"] = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                return payload

            def run_python(source: str, *args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, "-c", source, *args],
                    capture_output=True, text=True, check=False,
                )

            expected = manifest()
            self.assertIn("node_modules/wrangler/wrangler-dist/.gitkeep", expected["files"])
            builder = builder_blocks[1].replace(reviewed_digest, str(expected["digest"]))
            result = run_python(builder, str(root), str(lockfile), lock_digest)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads((root / "wrangler-tool-manifest.json").read_text(encoding="ascii")),
                expected,
            )
            for path in executable_paths:
                path.chmod(0o644)
            result = run_python(
                publisher_blocks[0], str(root), lock_digest, str(expected["digest"])
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for path in executable_paths:
                self.assertEqual(path.stat().st_mode & 0o777, 0o755)
            self.assertEqual((cli.parent / ".gitkeep").stat().st_mode & 0o777, 0o644)

            (root / "wrangler-tool-manifest.json").unlink()
            workerd.rename(workerd.with_name("workerd-copy"))
            workerd.write_bytes(b"pinned path now small\n")
            workerd.chmod(0o755)
            rejected = manifest()
            result = run_python(builder, str(root), str(lockfile), lock_digest)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("size bound", result.stderr)
            (root / "wrangler-tool-manifest.json").write_text(
                json.dumps(rejected, sort_keys=True) + "\n", encoding="ascii"
            )
            result = run_python(
                publisher_blocks[0], str(root), lock_digest, str(rejected["digest"])
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("size bound", result.stderr)

    def test_owned_secret_jobs_are_fresh_and_do_not_load_candidate_code(self) -> None:
        for workflow_name, expected_jobs in OWNED_SECRET_WORKFLOWS.items():
            source = (WORKFLOW_ROOT / workflow_name).read_text(encoding="utf-8")
            jobs = _job_blocks(source)
            for job_name in expected_jobs:
                with self.subTest(workflow=workflow_name, job=job_name):
                    body = jobs[job_name]
                    self.assertIn("environment: production", body)
                    self.assertNotIn("actions/checkout@", body)
                    self.assertNotIn("/root/old_sparky", body)
                    self.assertNotIn("platform/tools/", body)
                    self.assertNotRegex(
                        body,
                        r"platform_(?:build_release|cloudflare_readonly_audit|"
                        r"live_user_qa(?:\.sh|_mailbox_helper)|live_qa_mailbox_helper)",
                    )

    def test_retained_abort_uses_only_digest_bound_helper_in_root_stage(self) -> None:
        workflow = yaml.safe_load(
            (WORKFLOW_ROOT / "platform-production-retained-load-abort.yml").read_text(
                encoding="utf-8"
            )
        )
        prepare = workflow["jobs"]["prepare-abort-helper"]
        abort = workflow["jobs"]["abort"]
        prepare_text = json.dumps(prepare, sort_keys=True)
        abort_text = json.dumps(abort, sort_keys=True)
        self.assertEqual(workflow["permissions"]["actions"], "read")
        self.assertIn("security_run_id", workflow[True]["workflow_dispatch"]["inputs"])
        self.assertIn("security_run_attempt", workflow[True]["workflow_dispatch"]["inputs"])
        self.assertNotIn("environment", prepare)
        self.assertIn("actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803", prepare_text)
        self.assertIn("persist-credentials", prepare_text)
        self.assertIn("false", prepare_text)
        self.assertIn("sha256", prepare_text)
        self.assertEqual(abort.get("needs"), "prepare-abort-helper")
        self.assertNotIn("actions/checkout@", abort_text)
        self.assertIn("actions/download-artifact@018cc2cf5baa6db3ef3c5f8a56943fffe632ef53", abort_text)
        self.assertIn("EXPECTED_HELPER_SHA256", abort_text)
        self.assertIn("/run/oldsparky-retained-abort-", abort_text)
        self.assertIn("/usr/bin/python3.12 -I -B", abort_text)
        self.assertIn("cleanup_recovery_helper", abort_text)
        self.assertIn("supervisor_already_absent", abort_text)
        step_names = [step.get("name") for step in prepare["steps"]]
        self.assertLess(
            step_names.index("Validate exact successful Security gates and trusted route"),
            step_names.index("Pin the exact helper bytes"),
        )
        source_gate = next(
            step for step in prepare["steps"]
            if step.get("name") == "Validate exact successful Security gates and trusted route"
        )
        source_gate_text = json.dumps(source_gate, sort_keys=True)
        self.assertIn("validate_recovery_source_security_gates", source_gate_text)
        self.assertIn("latest-run.json", source_gate_text)
        self.assertIn("platform-ci-route-", source_gate_text)
        self.assertIn("platform_production_classifier_artifact.py manifest", source_gate_text)
        self.assertIn('--target-sha "$SOURCE_SHA"', source_gate["run"])
        self.assertIn("--emit-manifest-base64", source_gate["run"])
        self.assertIn("decode_classifier_manifest_base64_output", source_gate["run"])
        self.assertIn('(metadata / "route-manifest.b64").read_bytes()', source_gate["run"])
        self.assertIn("route_manifest=route_manifest", source_gate["run"])
        self.assertLess(
            source_gate["run"].index("--emit-manifest-base64"),
            source_gate["run"].index("validate_recovery_source_security_gates("),
        )
        self.assertNotIn("--allow-docs-only", source_gate_text)
        self.assertNotIn("--allow-out-of-scope", source_gate_text)
        self.assertEqual(source_gate["env"]["SOURCE_SHA"], "${{ github.sha }}")
        self.assertIn("security_run_id", source_gate_text)
        self.assertIn("security_run_attempt", source_gate_text)
        self.assertEqual(abort["env"]["TARGET_SHA"], "${{ inputs.target_sha }}")

    def test_draft_release_is_a_digest_bound_data_only_consumer(self) -> None:
        source = (WORKFLOW_ROOT / "platform-draft-cloudflare.yml").read_text(
            encoding="utf-8"
        )
        jobs = _job_blocks(source)
        self.assertIn("actions/checkout@", jobs["build-release"])
        self.assertNotRegex(jobs["build-release"], r"CLOUDFLARE_(?:API_TOKEN|ACCOUNT_ID)")
        self.assertIn("npm ci --ignore-scripts --no-audit --no-fund --omit=dev", jobs["build-release"])
        self.assertIn("package-lock.json", jobs["build-release"])
        self.assertIn("WRANGLER_LOCK_SHA256", jobs["build-release"])
        lock_check = jobs["build-release"].index("sha256sum --check")
        npm_install = jobs["build-release"].index("npm ci --ignore-scripts")
        self.assertLess(lock_check, npm_install)
        self.assertIn("c27608e7efe705b963aee2f3b2d5ef15ac7d7e51816f086836aac071ea59ad6a", jobs["build-release"])
        release = jobs["release"]
        self.assertNotIn("actions/checkout@", release)
        self.assertIn("actions/download-artifact@", release)
        self.assertIn("bundle-manifest.json", release)
        self.assertIn("manifest digest mismatch", release)
        self.assertIn(
            "platform-draft-wrangler-${{ github.run_id }}-${{ github.run_attempt }}",
            jobs["build-release"],
        )
        self.assertIn("wrangler-tool-manifest.json", release)
        self.assertIn("EXPECTED_WRANGLER_MANIFEST_SHA256", release)
        self.assertIn('node "$WRANGLER_CLI"', release)
        self.assertNotIn("npx", release)

        workflow = yaml.safe_load(
            (WORKFLOW_ROOT / "platform-draft-cloudflare.yml").read_text(encoding="utf-8")
        )
        build_job = workflow["jobs"]["build-release"]
        self.assertEqual(
            build_job["outputs"]["draft_bundle_artifact_id"],
            "${{ steps.upload-draft-bundle.outputs.artifact-id }}",
        )
        self.assertEqual(
            build_job["outputs"]["wrangler_runtime_artifact_id"],
            "${{ steps.upload-wrangler-runtime.outputs.artifact-id }}",
        )
        self.assertEqual(build_job["outputs"]["build_run_id"], "${{ github.run_id }}")
        self.assertEqual(build_job["outputs"]["build_attempt"], "${{ github.run_attempt }}")
        upload_steps = {step.get("id"): step for step in build_job["steps"]}
        self.assertEqual(upload_steps["upload-draft-bundle"]["uses"].split("@")[0], "actions/upload-artifact")
        self.assertEqual(upload_steps["upload-wrangler-runtime"]["uses"].split("@")[0], "actions/upload-artifact")
        release_steps = workflow["jobs"]["release"]["steps"]
        bundle_download = _named_step(release_steps, "Download digest-bound Draft bundle")
        runtime_download = _named_step(release_steps, "Download digest-bound Wrangler runtime")
        self.assertEqual(
            bundle_download["with"]["artifact-ids"],
            "${{ needs.build-release.outputs.draft_bundle_artifact_id }}",
        )
        self.assertEqual(
            runtime_download["with"]["artifact-ids"],
            "${{ needs.build-release.outputs.wrangler_runtime_artifact_id }}",
        )
        self.assertNotIn("github.run_attempt", str(bundle_download["with"]))
        self.assertNotIn("github.run_attempt", str(runtime_download["with"]))
        self.assertIn('[[ "$BUILD_RUN_ID" == "$GITHUB_RUN_ID" ]]', release)

        bundle_step = _named_step(build_job["steps"], "Generate immutable Draft bundle")
        bundle_builder = re.search(r"<<'PY'\n(.*?)\nPY", str(bundle_step["run"]), re.DOTALL)
        validator_step = _named_step(release_steps, "Validate data-only Draft handoff")
        bundle_validator = re.search(r"<<'PY'\n(.*?)\nPY", str(validator_step["run"]), re.DOTALL)
        self.assertIsNotNone(bundle_builder)
        self.assertIsNotNone(bundle_validator)
        with tempfile.TemporaryDirectory(prefix="draft-artifact-identity-") as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "app.js").write_text("source-bound app\n", encoding="ascii")
            target_sha = "a" * 40
            build_run_id = "39979966537"
            build_attempt = "1"
            generated = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    bundle_builder.group(1),
                    str(bundle),
                    target_sha,
                    build_run_id,
                    build_attempt,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            valid = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    bundle_validator.group(1),
                    str(bundle / "bundle-manifest.json"),
                    str(bundle),
                    target_sha,
                    build_run_id,
                    build_attempt,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            wrong_attempt = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    bundle_validator.group(1),
                    str(bundle / "bundle-manifest.json"),
                    str(bundle),
                    target_sha,
                    build_run_id,
                    "2",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(wrong_attempt.returncode, 0)
            self.assertIn("Draft handoff schema or SHA is invalid", wrong_attempt.stderr)

        smoke_run = str(
            _named_step(
                _draft_job_steps("release"),
                "Smoke-check Draft page, assets, CDN and room creation",
            )["run"]
        )
        lines = smoke_run.splitlines()
        helper_start = next(
            index for index, line in enumerate(lines) if line.strip() == "fetch_response() {"
        )
        helper_end = next(
            index
            for index in range(helper_start + 1, len(lines))
            if lines[index].strip().startswith("assert_live_asset ")
        )
        smoke_helpers = "\n".join(lines[helper_start:helper_end])
        current_asset = b"current candidate asset\n"

        with tempfile.TemporaryDirectory(prefix="draft-live-smoke-") as temporary:
            root = Path(temporary)
            expected = root / "expected.js"
            expected.write_bytes(current_asset)
            expected_hash = hashlib.sha256(current_asset).hexdigest()

            def run_readiness_case(mode: str) -> tuple[subprocess.CompletedProcess[str], int, int]:
                call_count = root / f"{mode}.calls"
                sleep_count = root / f"{mode}.sleeps"
                call_count.write_text("0\n", encoding="ascii")
                sleep_count.write_text("0\n", encoding="ascii")
                output = root / f"{mode}.js"
                harness = r"""
set -euo pipefail
@HELPERS@
curl() {
  local output="" headers=""
  while (($#)); do
    case "$1" in
      --output) output="$2"; shift 2 ;;
      --dump-header) headers="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  local calls
  calls="$(cat "$CALL_COUNT_FILE")"
  calls=$((calls + 1))
  printf '%s\n' "$calls" > "$CALL_COUNT_FILE"
  local status="200"
  if [[ "$MODE" == "status-then-converges" ]]; then
    cat "$EXPECTED_FILE" > "$output"
    if [[ "$calls" -eq 1 ]]; then
      status="503"
    fi
  elif [[ "$MODE" == "converges" && "$calls" -gt 1 ]]; then
    cat "$EXPECTED_FILE" > "$output"
  else
    printf 'previous candidate asset\n' > "$output"
  fi
  printf 'HTTP/2 %s\r\nX-Old-Sparky-Draft-Asset-Cache-Status: HIT\r\nAge: 3\r\nETag: "previous"\r\n\r\n' "$status" > "$headers"
  printf '%s' "$status"
}
sleep() {
  local sleeps
  sleeps="$(cat "$SLEEP_COUNT_FILE")"
  printf '%s\n' "$((sleeps + 1))" > "$SLEEP_COUNT_FILE"
}
if assert_live_asset "Draft heroes.js" "https://old-sparky.com/draft/heroes.js" "$OUTPUT_FILE" "$EXPECTED_FILE"; then
  result=0
else
  result=$?
fi
printf 'RESULT=%s\n' "$result"
""".replace("@HELPERS@", smoke_helpers)
                result = subprocess.run(
                    ["bash", "--noprofile", "--norc", "-c", harness],
                    capture_output=True,
                    text=True,
                    check=False,
                    env={
                        **os.environ,
                        "MODE": mode,
                        "EXPECTED_FILE": str(expected),
                        "OUTPUT_FILE": str(output),
                        "CALL_COUNT_FILE": str(call_count),
                        "SLEEP_COUNT_FILE": str(sleep_count),
                    },
                )
                return result, int(call_count.read_text(encoding="ascii")), int(
                    sleep_count.read_text(encoding="ascii")
                )

            converged, calls, sleeps = run_readiness_case("converges")
            self.assertEqual(converged.returncode, 0, converged.stderr)
            self.assertIn("RESULT=0", converged.stdout)
            self.assertEqual(calls, 2)
            self.assertEqual(sleeps, 1)
            self.assertIn(f"expected sha256={expected_hash}", converged.stderr)
            self.assertIn("Age: 3", converged.stderr)
            self.assertIn(f"live body matches {expected_hash} (attempt 2/16)", converged.stdout)

            status_recovered, calls, sleeps = run_readiness_case("status-then-converges")
            self.assertEqual(status_recovered.returncode, 0, status_recovered.stderr)
            self.assertIn("RESULT=0", status_recovered.stdout)
            self.assertEqual(calls, 2)
            self.assertEqual(sleeps, 1)
            self.assertIn(f"returned HTTP 503; expected sha256={expected_hash}, received sha256={expected_hash}", status_recovered.stderr)
            self.assertIn(f"live body matches {expected_hash} (attempt 2/16)", status_recovered.stdout)

            rejected, calls, sleeps = run_readiness_case("never-converges")
            self.assertEqual(rejected.returncode, 0, rejected.stderr)
            self.assertIn("RESULT=1", rejected.stdout)
            self.assertEqual(calls, 16)
            self.assertEqual(sleeps, 15)
            self.assertIn(f"expected sha256={expected_hash}", rejected.stderr)
            self.assertIn("did not converge to the just-built response after 16 bounded attempts", rejected.stderr)
            self.assertIn("Age: 3", rejected.stderr)

    def test_secret_provenance_jobs_pin_validators_to_trusted_default_source(self) -> None:
        for workflow_name, job_marker in (
            ("platform-production-deploy.yml", "validate-security-provenance"),
            ("platform-production-content-diagnostics.yml", "validate-content-provenance"),
            ("platform-patch-translation-qa.yml", "validate-translation-inputs"),
        ):
            source = (WORKFLOW_ROOT / workflow_name).read_text(encoding="utf-8")
            jobs = _job_blocks(source)
            body = jobs[job_marker]
            with self.subTest(workflow=workflow_name):
                self.assertIn("Resolve immutable default-branch", body)
                self.assertIn("TRUSTED_BRANCH: dev", body)
                self.assertIn("git/ref/heads/${TRUSTED_BRANCH}", body)
                self.assertIn("refs/heads/{branch}", body)
                self.assertIn("obj.get(\"type\") != \"commit\"", body)
                self.assertIn("ref: ${{ steps.trusted_default.outputs.sha }}", body)
                self.assertNotIn("ref: ${{ env.TARGET_SHA }}", body)
                self.assertIn("TRUSTED_SOURCE_SHA", body)
                self.assertIn("git rev-parse HEAD", body)

    def test_security_classifier_executes_only_trusted_route_logic(self) -> None:
        source = (WORKFLOW_ROOT / "platform-security.yml").read_text(encoding="utf-8")
        jobs = _job_blocks(source)
        classifier = jobs["classifier"]
        self.assertIn("Checkout candidate history as data only", classifier)
        self.assertIn("Capture candidate changed files without executing candidate code", classifier)
        self.assertIn("Resolve immutable default-branch classifier source", classifier)
        self.assertIn("Checkout trusted classifier source", classifier)
        self.assertIn("--files-file", classifier)
        self.assertNotIn("--repo-root \"$GITHUB_WORKSPACE\"", classifier)

    def test_all_retained_artifact_names_are_attempt_bound(self) -> None:
        for path in sorted(WORKFLOW_ROOT.glob("*.y*ml")):
            source = path.read_text(encoding="utf-8")
            for line in source.splitlines():
                if "name:" in line and "${{ github.run_id }}" in line:
                    with self.subTest(workflow=path.name, line=line.strip()):
                        self.assertIn("${{ github.run_attempt }}", line)

    def test_security_web_reports_are_attempt_bound_and_playwright_install_is_hermetic(self) -> None:
        security = (WORKFLOW_ROOT / "platform-security.yml").read_text(encoding="utf-8")
        self.assertIn("platform-web-playwright-report-${{ github.run_id }}-${{ github.run_attempt }}", security)
        self.assertNotIn("playwright install chromium", security)

    def test_live_user_qa_uses_only_the_fixed_host_dispatch_boundary(self) -> None:
        source = (WORKFLOW_ROOT / "platform-live-user-qa.yml").read_text(
            encoding="utf-8"
        )
        job = _job_blocks(source)["live-user-qa"]
        self.assertNotIn("actions/checkout@", job)
        self.assertNotIn("actions/download-artifact@", job)
        self.assertIn("contents: none", job)
        self.assertIn('run = api(f"/actions/runs/{run_id}")', job)
        self.assertIn(
            'listing = api(f"/actions/runs/{run_id}/artifacts?per_page=100&page=1")',
            job,
        )
        self.assertIn('metadata = api(f"/actions/artifacts/{expected_id}")', job)
        self.assertIn("metadata.get(\"digest\")", job)
        self.assertIn("metadata_run.get(\"head_sha\")", job)
        self.assertIn('f"/actions/artifacts/{expected_id}/zip"', job)
        self.assertIn(
            'hashlib.sha256(archive_bytes).hexdigest() != digest.removeprefix("sha256:")',
            job,
        )
        self.assertIn("if len(entries) != 1", job)
        self.assertIn("with archive.open(info) as member:", job)
        self.assertIn("member.read(MAX_MEMBER + 1)", job)
        self.assertIn(
            "/root/.oldsparky/liveqa/platform_workflow_remote_dispatch.py",
            job,
        )
        self.assertIn('HANDOFF_MEMBER_NAME: live-user-qa-input.json', job)
        self.assertIn('HANDOFF_ARTIFACT_ID: ${{ needs.validate-live-user-inputs.outputs.handoff_artifact_id }}', job)
        self.assertIn('live-user-qa < "$input_path"', job)
        self.assertNotIn("/opt/oldsparky/platform/current/tools/", job)
        self.assertIn("live-user-qa", job)
        self.assertNotIn("bash -s", job)
        self.assertNotIn("platform_live_user_qa.sh", job)
        self.assertNotIn("platform_live_qa_mailbox_helper.py", job)

    def test_cloudflare_audit_has_no_candidate_import_or_checkout(self) -> None:
        source = (WORKFLOW_ROOT / "platform-cloudflare-readonly-audit.yml").read_text(
            encoding="utf-8"
        )
        job = _job_blocks(source)["audit"]
        self.assertNotIn("actions/checkout@", job)
        self.assertNotIn("cloudflare_readonly_audit.py", job)
        self.assertIn("Fixed, value-free Cloudflare API read-only projection", job)

    def test_autodeploy_archive_path_is_bounded_and_token_free_for_candidate_code(self) -> None:
        source = (WORKFLOW_ROOT / "platform-production-autodeploy.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("unzip -q", source)
        self.assertIn(
            "env -u GH_TOKEN -u GITHUB_TOKEN /usr/bin/python3 -I -B",
            source,
        )
        self.assertIn("platform_production_classifier_artifact.py manifest", source)
        self.assertIn("--max-filesize 4194304", source)
        validator = (
            REPO_ROOT / "platform/tools/platform_production_classifier_artifact.py"
        ).read_text(encoding="utf-8")
        self.assertIn("extract_single_manifest", validator)
        self.assertIn("platform_safe_zip", validator)

    def test_production_secret_classifier_extractor_is_bounded_and_no_candidate(self) -> None:
        source = (WORKFLOW_ROOT / "platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        production = _job_blocks(source)["production"]
        prerequisite = _job_blocks(source)["validate-classifier"]
        validator = (
            REPO_ROOT / "platform/tools/platform_production_classifier_artifact.py"
        ).read_text(encoding="utf-8")
        self.assertIn("O_NOFOLLOW", validator)
        self.assertIn("object_pairs_hook=_reject_duplicate_keys", validator)
        self.assertIn("type(manifest.get(\"runtime_sensitive\")) is not bool", validator)
        for job in (prerequisite, production):
            with self.subTest(job="production" if job is production else "validate-classifier"):
                self.assertIn("platform_production_classifier_artifact.py", job)
                self.assertIn("trusted_classifier_default.outputs.sha", job)
                self.assertIn("actions/checkout@", job)
                self.assertNotIn("env.TARGET_SHA", job)
                self.assertIn("--max-filesize 4194304", job)
                self.assertIn("rm -rf -- \"$route_dir\"", job)
        self.assertIn("- validate-classifier", _job_blocks(source)["build-release"])
        self.assertNotIn("/usr/bin/python3 \"$artifacts_metadata\"", production)
        self.assertNotIn("bundle.namelist()", production)


class FixtureCredentialTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(REPO_ROOT / "platform"))
        from tools import platform_fixture_credential_transport as transport

        cls.transport = transport
        cls.tool_root = Path(tempfile.mkdtemp(prefix="fixture-age-tools-"))
        os.chmod(cls.tool_root, 0o700)
        cls.age = transport.ensure_age_binary(cls.tool_root / "age-tools")
        cls.age_keygen = cls.tool_root / "age-tools" / "age-keygen"
        cls.key_dir = cls.tool_root / "keys"
        cls.key_dir.mkdir(mode=0o700)
        cls.identity = cls.key_dir / "test-identity.txt"
        generated = subprocess.run(
            [str(cls.age_keygen), "-o", str(cls.identity)],
            capture_output=True, text=True, check=False,
        )
        if generated.returncode != 0:
            raise AssertionError("pinned age-keygen could not create a synthetic test identity")
        cls.identity.chmod(0o600)
        recipient = subprocess.run(
            [str(cls.age_keygen), "-y", str(cls.identity)],
            capture_output=True, text=True, check=False,
        )
        if recipient.returncode != 0 or re.fullmatch(r"age1[0-9a-z]{58}\n?", recipient.stdout) is None:
            raise AssertionError("pinned age-keygen returned an invalid synthetic recipient")
        cls.original_recipient = transport.AGE_RECIPIENT
        transport.AGE_RECIPIENT = recipient.stdout.strip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.transport.AGE_RECIPIENT = cls.original_recipient
        shutil.rmtree(cls.tool_root)

    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="fixture-private-transport-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        os.chmod(self.root, 0o700)

    @staticmethod
    def _binding(runner_sha: str, app_sha: str) -> dict[str, object]:
        return {
            "schema": 1,
            "binding_mode": "verified-noop",
            "runner_sha": runner_sha,
            "app_target_sha": app_sha,
            "baseline_identity": {
                "schema": 1,
                "source_sha": app_sha,
                "release_slug": "gha-123456-1-" + app_sha[:12],
                "release_json_sha256": "c" * 64,
                "current_link_dev": 100,
                "current_link_ino": 101,
                "release_dev": 100,
                "release_ino": 102,
                "pending_operation": False,
            },
            "receipt_document_sha256": "e" * 64,
            "receipt_artifact_id": "456789",
            "receipt_artifact_name": "platform-production-noop-source-receipt-123456-2",
            "receipt_artifact_digest": "sha256:" + "d" * 64,
            "receipt_archive_sha256": "d" * 64,
            "cumulative_manifest_sha256": "f" * 64,
            "source_security_run_id": "234567",
            "source_security_run_attempt": "1",
            "autodeploy_run_id": "345678",
            "autodeploy_run_attempt": "3",
            "production_deploy_run_id": "123456",
            "production_deploy_run_attempt": "2",
        }

    def _files(self, *, runner_sha: str, app_sha: str) -> tuple[Path, Path, dict[str, str]]:
        binding = self._binding(runner_sha, app_sha) if runner_sha != app_sha else None
        input_payload: dict[str, object] = {
            "schema": "2" if binding is not None else "1",
            "confirmation": "RUN-PRODUCTION-SSR-CAPACITY-DIAGNOSTIC" if binding is not None else "RUN-PRODUCTION-EXTERNAL-LOAD",
            "target_sha": runner_sha,
            "control_email": "fixture-control@example.invalid",
            "setup_concurrency": "8",
            "run_id": "567890",
            "profile": "external-vote",
            "tournament_count": "1",
            "users_per_tournament": "14",
            "timeout_diagnostics": "false",
            "fixture_kind": "ssr_html_capacity_composite_diagnostic_v1" if binding is not None else "external_load",
        }
        if binding is not None:
            input_payload.update({
                "source_binding": binding,
                "ssr_capacity_concurrency": "32",
                "ssr_capacity_workload": "authenticated_html_ssr_capacity_120s_plus_cpu_profile_20s_v1",
            })
        canonical_binding = json.dumps(binding, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("ascii") if binding is not None else None
        users = [
            {
                "user_id": f"synthetic-user-{index:02d}",
                "tournament_slug": "private-fixture-slug",
                "session_token": f"SYNTHETIC_SESSION_TOKEN_{index:02d}_not_a_real_credential",
                "csrf_token": f"SYNTHETIC_CSRF_TOKEN_{index:02d}_not_a_real_credential",
            }
            for index in range(14)
        ]
        manifest = {
            "schema": 2,
            "purpose": "external_ready_vote",
            "origin": "https://old-sparky.com",
            "session_cookie_name": "__Host-session",
            "csrf_cookie_name": "__Host-session_csrf",
            "marker": "preprod202611020000abcd",
            "created_at": "2026-11-02T00:00:00Z",
            "tournaments": [{"id": "synthetic-tournament-id", "slug": "private-fixture-slug", "user_count": 14}],
            "users": users,
            "runner_sha": runner_sha,
            "app_target_sha": app_sha,
            "source_binding": binding,
            "source_binding_sha256": hashlib.sha256(canonical_binding).hexdigest() if canonical_binding is not None else None,
        }
        input_path = self.root / "input.json"
        manifest_path = self.root / "manifest.json"
        input_path.write_text(json.dumps(input_payload, separators=(",", ":")) + "\n", encoding="ascii")
        manifest_path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="ascii")
        input_path.chmod(0o600)
        manifest_path.chmod(0o600)
        secret_values = {
            str(input_payload["control_email"]),
            *(row["session_token"] for row in users),
            *(row["csrf_token"] for row in users),
            "private-fixture-slug", "synthetic-user-00", "synthetic-tournament-id",
        }
        return input_path, manifest_path, {key: value for key, value in enumerate(secret_values)}

    def test_real_age_ciphertext_and_descriptor_round_trip_same_source_and_noop(self) -> None:
        for runner_sha, app_sha in (("a" * 40, "a" * 40), ("b" * 40, "a" * 40)):
            with self.subTest(source_binding=runner_sha != app_sha):
                input_path, manifest_path, secrets = self._files(runner_sha=runner_sha, app_sha=app_sha)
                input_cipher = self.root / f"input-{runner_sha[:1]}.json.age"
                manifest_cipher = self.root / f"manifest-{runner_sha[:1]}.json.age"
                input_plain = self.root / f"input-{runner_sha[:1]}.decoded.json"
                manifest_plain = self.root / f"manifest-{runner_sha[:1]}.decoded.json"
                descriptor_path = self.root / f"descriptor-{runner_sha[:1]}.json"
                self.transport.encrypt_file(input_path, input_cipher, self.age)
                self.transport.encrypt_file(manifest_path, manifest_cipher, self.age)
                self.transport.decrypt_file(input_cipher, input_plain, self.identity, self.age)
                self.transport.decrypt_file(manifest_cipher, manifest_plain, self.identity, self.age)
                self.assertEqual(input_plain.read_bytes(), input_path.read_bytes())
                self.assertEqual(manifest_plain.read_bytes(), manifest_path.read_bytes())
                self.transport.build_descriptor(
                    input_path, manifest_path, descriptor_path,
                    "567890", "1", runner_sha, app_sha,
                )
                descriptor = self.transport.validate_descriptor(
                    descriptor_path, "567890", "1", runner_sha, app_sha,
                )
                self.assertEqual(descriptor["runner_sha"], runner_sha)
                self.assertEqual(descriptor["app_target_sha"], app_sha)
                self.assertEqual(descriptor["manifest"]["user_count"], 14)
                for value in secrets.values():
                    encoded = value.encode("ascii")
                    self.assertNotIn(encoded, descriptor_path.read_bytes())
                    self.assertNotIn(encoded, input_cipher.read_bytes())
                    self.assertNotIn(encoded, manifest_cipher.read_bytes())
                artifact = self.root / f"ciphertext-only-{runner_sha[:1]}.zip"
                with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_STORED) as archive:
                    archive.write(input_cipher, "private-input.age")
                    archive.write(manifest_cipher, "private-manifest.age")
                    archive.write(descriptor_path, "fixture-descriptor.json")
                for value in secrets.values():
                    self.assertNotIn(value.encode("ascii"), artifact.read_bytes())
                self.assertFalse(any(value.encode("ascii") in artifact.read_bytes() for value in ("fixture-control@example.invalid",)))

    def test_wrong_identity_and_tampered_ciphertext_fail_without_plaintext_output(self) -> None:
        input_path, _manifest_path, _secrets = self._files(runner_sha="a" * 40, app_sha="a" * 40)
        ciphertext = self.root / "input.json.age"
        self.transport.encrypt_file(input_path, ciphertext, self.age)
        wrong_identity = self.root / "wrong-identity.txt"
        generated = subprocess.run(
            [str(self.age_keygen), "-o", str(wrong_identity)], capture_output=True, text=True, check=False,
        )
        self.assertEqual(generated.returncode, 0)
        wrong_identity.chmod(0o600)
        rejected = self.root / "wrong-key-output.json"
        with self.assertRaises(SystemExit):
            self.transport.decrypt_file(ciphertext, rejected, wrong_identity, self.age)
        self.assertFalse(rejected.exists())
        tampered = self.root / "tampered.age"
        raw = ciphertext.read_bytes()
        tampered.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        tampered.chmod(0o600)
        rejected = self.root / "tamper-output.json"
        with self.assertRaises(SystemExit):
            self.transport.decrypt_file(tampered, rejected, self.identity, self.age)
        self.assertFalse(rejected.exists())

    def test_descriptor_rejects_private_extra_mismatch_and_unknown_fields(self) -> None:
        input_path, manifest_path, _secrets = self._files(runner_sha="b" * 40, app_sha="a" * 40)
        descriptor_path = self.root / "descriptor.json"
        self.transport.build_descriptor(input_path, manifest_path, descriptor_path, "567890", "1", "b" * 40, "a" * 40)
        valid = json.loads(descriptor_path.read_text(encoding="ascii"))
        invalid_cases = (
            {**valid, "surprise": "value"},
            {**valid, "input": {**valid["input"], "control_email": "fixture-control@example.invalid"}},
            {**valid, "manifest": {**valid["manifest"], "source_binding_sha256": "0" * 64}},
        )
        for payload in invalid_cases:
            with self.subTest(keys=sorted(payload)):
                descriptor_path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="ascii")
                descriptor_path.chmod(0o600)
                with self.assertRaises(SystemExit):
                    self.transport.validate_descriptor(descriptor_path, "567890", "1", "b" * 40, "a" * 40)

    def test_decrypted_manifest_must_match_safe_descriptor(self) -> None:
        from tools.platform_load import source_binding_environment_from_handoff

        input_path, manifest_path, _secrets = self._files(runner_sha="b" * 40, app_sha="a" * 40)
        descriptor_path = self.root / "descriptor.json"
        self.transport.build_descriptor(input_path, manifest_path, descriptor_path, "567890", "1", "b" * 40, "a" * 40)
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "567890", "GITHUB_RUN_ATTEMPT": "1"}):
            values = source_binding_environment_from_handoff(
                descriptor_path, manifest_path, runner_sha="b" * 40, app_target_sha="a" * 40,
            )
            self.assertTrue(values["SOURCE_BINDING_BASE64"])
            self.assertRegex(values["SOURCE_BINDING_SHA256"], r"^[0-9a-f]{64}$")
            manifest_path.write_text(manifest_path.read_text(encoding="utf-8").replace("SYNTHETIC_SESSION_TOKEN_00", "SYNTHETIC_SESSION_TOKEN_XX"), encoding="utf-8")
            manifest_path.chmod(0o600)
            from tools.platform_load import LoadProfileError
            with self.assertRaises(LoadProfileError):
                source_binding_environment_from_handoff(
                    descriptor_path, manifest_path, runner_sha="b" * 40, app_target_sha="a" * 40,
                )

    def test_profile_export_to_diagnostic_serializer_uses_fixed_fixture_cohort(self) -> None:
        workflow = yaml.safe_load(
            (WORKFLOW_ROOT / "platform-production-external-load.yml").read_text(encoding="utf-8")
        )
        validator_steps = workflow["jobs"]["validate-external-inputs"]["steps"]
        resolver_steps = workflow["jobs"]["resolve-load-source-and-profile"]["steps"]
        scrub_step = validator_steps[0]
        dispatch_step = next(
            step for step in validator_steps
            if step.get("name") == "Reject invalid external-load dispatch before checkout"
        )
        serializer_step = next(
            step for step in validator_steps
            if step.get("name") == "Serialize the private external-load input"
        )
        profile_step = next(
            step for step in resolver_steps
            if step.get("name") == "Validate and export the exact load profile"
        )
        target_sha = "a" * 40
        run_id = "567890"

        def run_case(*, diagnostic: bool) -> dict[str, object]:
            case_root = self.root / ("diagnostic" if diagnostic else "ordinary")
            case_root.mkdir(mode=0o700)
            resolver_temp = case_root / "resolver-temp"
            resolver_temp.mkdir(mode=0o700)
            email = "synthetic-fixture@example.invalid"
            confirmation = (
                "RUN-PRODUCTION-SSR-CAPACITY-DIAGNOSTIC"
                if diagnostic else "RUN-PRODUCTION-EXTERNAL-LOAD"
            )
            capacity_only = "true" if diagnostic else "false"
            concurrency = "32" if diagnostic else "8"
            workload = (
                "authenticated_html_ssr_capacity_120s_plus_cpu_profile_20s_v1"
                if diagnostic else "authenticated_workspace_read_capacity_v1"
            )
            input_values = {
                "confirmation": confirmation,
                "control_email": email,
                "profile_id": "authenticated-page-load-v1",
                "timeout_diagnostics": "false",
                "cpu_diagnostic_pair": "false",
                "cpu_diagnostic_only": "false",
                "ssr_capacity_diagnostic_only": capacity_only,
                "ssr_capacity_concurrency": concurrency,
                "ssr_capacity_workload": workload,
            }
            resolver_event = case_root / "resolver-event.json"
            resolver_event.write_text(
                json.dumps({"inputs": input_values, "repository": {"description": "synthetic contract"}}),
                encoding="utf-8",
            )
            resolver_event.chmod(0o600)
            resolver_env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(case_root),
                "GITHUB_EVENT_PATH": str(resolver_event),
                "RUNNER_TEMP": str(resolver_temp),
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/dev",
                "TARGET_SHA": target_sha,
                "PROFILE_ID": "authenticated-page-load-v1",
                "TIMEOUT_DIAGNOSTICS": "false",
                "CPU_DIAGNOSTIC_ONLY": "false",
                "SSR_CAPACITY_DIAGNOSTIC_ONLY": capacity_only,
            }
            resolver_scrub = next(
                step for step in resolver_steps
                if step.get("name") == "Mask and remove control identity before candidate checkout"
            )
            scrubbed = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", resolver_scrub["run"]],
                env=resolver_env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(scrubbed.returncode, 0, scrubbed.stderr)
            self.assertNotIn(email, resolver_event.read_text(encoding="utf-8"))
            scrubbed_event = json.loads(resolver_event.read_text(encoding="utf-8"))
            self.assertNotIn("control_email", scrubbed_event["inputs"])

            resolver_guard = next(
                step for step in resolver_steps
                if step.get("name") == "Require exact workflow dispatch inputs"
            )
            resolver_checked = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", resolver_guard["run"]],
                env=resolver_env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(resolver_checked.returncode, 0, resolver_checked.stderr)

            profile_outputs = case_root / "profile-outputs.txt"
            profile_outputs.touch(mode=0o600)
            exported = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", profile_step["run"]],
                cwd=REPO_ROOT,
                env={
                    **resolver_env,
                    "GITHUB_OUTPUT": str(profile_outputs),
                    "PROFILE_ID": "authenticated-page-load-v1",
                    "TARGET_SHA": target_sha,
                    "APP_TARGET_SHA": target_sha,
                    "GITHUB_RUN_ID": run_id,
                    "SSR_CAPACITY_CONCURRENCY": concurrency,
                    "SSR_CAPACITY_WORKLOAD": workload,
                },
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertFalse((resolver_temp / "platform-production-control-email").exists())
            output_values = dict(
                line.split("=", 1) for line in profile_outputs.read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(output_values["tournament_count"], "40")
            self.assertEqual(output_values["users_per_tournament"], "500")

            dispatch_script = dispatch_step["run"]
            for input_name, value in {
                "cpu_diagnostic_pair": "false",
                "ssr_capacity_diagnostic_only": capacity_only,
                "ssr_capacity_concurrency": concurrency,
                "ssr_capacity_workload": workload,
            }.items():
                dispatch_script = dispatch_script.replace(
                    f"${{{{ inputs.{input_name} }}}}", value,
                )
            validation_temp = case_root / "validator-temp"
            validation_temp.mkdir(mode=0o700)
            validation_event = case_root / "validator-event.json"
            validation_event.write_text(
                json.dumps({"inputs": input_values, "repository": {"description": "synthetic contract"}}),
                encoding="utf-8",
            )
            validation_event.chmod(0o600)
            minimal_env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(case_root),
                "GITHUB_EVENT_PATH": str(validation_event),
                "RUNNER_TEMP": str(validation_temp),
            }
            scrubbed = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", scrub_step["run"]],
                env=minimal_env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(scrubbed.returncode, 0, scrubbed.stderr)
            self.assertIn(f"::add-mask::{email}", scrubbed.stdout)
            scrubbed_event = json.loads(validation_event.read_text(encoding="utf-8"))
            self.assertNotIn("control_email", scrubbed_event["inputs"])
            self.assertNotIn(email, validation_event.read_text(encoding="utf-8"))
            email_path = validation_temp / "platform-production-control-email"
            self.assertEqual(email_path.read_text(encoding="ascii"), f"{email.lower()}\n")

            dispatch_env = {
                **minimal_env,
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/dev",
                "TARGET_SHA": target_sha,
                "QA_CONFIRMATION": confirmation,
                "PROFILE_ID": "authenticated-page-load-v1",
                "TIMEOUT_DIAGNOSTICS": "false",
                "CPU_DIAGNOSTIC_ONLY": "false",
                "SSR_CAPACITY_CONCURRENCY": concurrency,
                "SSR_CAPACITY_WORKLOAD": workload,
            }
            dispatched = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", dispatch_script],
                env=dispatch_env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(dispatched.returncode, 0, dispatched.stderr)

            private_input = validation_temp / "platform-production-external-load-input.json"
            serialized = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", serializer_step["run"]],
                env={
                    **minimal_env,
                    "QA_CONFIRMATION": confirmation,
                    "TARGET_SHA": target_sha,
                    "APP_TARGET_SHA": target_sha,
                    "PROFILE_SETUP_CONCURRENCY": output_values["setup_concurrency"],
                    "PROFILE_TOURNAMENT_COUNT": output_values["tournament_count"],
                    "PROFILE_USERS_PER_TOURNAMENT": output_values["users_per_tournament"],
                    "GITHUB_RUN_ID": run_id,
                    "TIMEOUT_DIAGNOSTICS": "false",
                    "CPU_DIAGNOSTIC_ONLY": "false",
                    "SSR_CAPACITY_DIAGNOSTIC_ONLY": capacity_only,
                    "SSR_CAPACITY_CONCURRENCY": concurrency,
                    "SSR_CAPACITY_WORKLOAD": workload,
                    "SOURCE_BINDING_BASE64": "",
                    "SOURCE_BINDING_SHA256": "",
                },
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(serialized.returncode, 0, serialized.stderr)
            self.assertFalse(email_path.exists())
            self.assertEqual(private_input.stat().st_mode & 0o777, 0o600)
            return json.loads(private_input.read_text(encoding="ascii"))

        ordinary = run_case(diagnostic=False)
        self.assertEqual(ordinary["fixture_kind"], "external_load")
        self.assertEqual(ordinary["tournament_count"], "40")
        self.assertEqual(ordinary["users_per_tournament"], "500")

        diagnostic = run_case(diagnostic=True)
        self.assertEqual(diagnostic["fixture_kind"], "ssr_html_capacity_composite_diagnostic_v1")
        self.assertEqual(diagnostic["tournament_count"], "1")
        self.assertEqual(diagnostic["users_per_tournament"], "14")
        self.assertEqual(diagnostic["ssr_capacity_concurrency"], "32")
        self.assertEqual(
            diagnostic["ssr_capacity_workload"],
            "authenticated_html_ssr_capacity_120s_plus_cpu_profile_20s_v1",
        )
        diagnostic_input = self.root / "diagnostic" / "validator-temp" / "platform-production-external-load-input.json"
        encrypted = self.root / "diagnostic-input.json.age"
        decrypted = self.root / "diagnostic-input.decrypted.json"
        self.transport.encrypt_file(diagnostic_input, encrypted, self.age)
        self.transport.decrypt_file(encrypted, decrypted, self.identity, self.age)
        self.assertEqual(json.loads(decrypted.read_text(encoding="ascii")), diagnostic)
        self.assertNotIn(b"synthetic-fixture@example.invalid", encrypted.read_bytes())


class FixtureCredentialWorkflowBoundaryTests(unittest.TestCase):
    def test_workflow_age_install_encrypt_consumer_decrypt_uses_real_archive_layout(self) -> None:
        from tools import platform_fixture_credential_transport as transport

        document = yaml.safe_load(
            (WORKFLOW_ROOT / "platform-production-external-load.yml").read_text(
                encoding="utf-8"
            )
        )
        jobs = document["jobs"]
        encrypt_step = _named_step(
            jobs["validate-external-inputs"]["steps"],
            "Encrypt the private external-load handoff",
        )
        fixture_steps = jobs["fixture-setup"]["steps"]
        stage_fixture_identity = _named_step(
            fixture_steps, "Stage the fixture-only decrypt identity"
        )
        decrypt_input_step = _named_step(
            fixture_steps,
            "Decrypt the verified private input handoff",
        )
        validate_manifest_step = _named_step(fixture_steps, "Validate closed fixture manifest")
        encrypt_manifest_step = _named_step(
            fixture_steps, "Encrypt fixture manifest and write its nonsecret descriptor"
        )
        fixture_cleanup_step = _named_step(fixture_steps, "Remove fixture-setup SSH material")
        load_steps = jobs["load-client"]["steps"]
        stage_load_identity = _named_step(load_steps, "Stage the fixture-only decrypt identity")
        decrypt_manifest_step = _named_step(
            load_steps, "Decrypt fixture manifest for the trusted load client"
        )
        validate_descriptor_step = _named_step(
            load_steps, "Validate exact fixture descriptor and decrypted manifest"
        )
        load_cleanup_step = _named_step(load_steps, "Remove decrypted fixture material")
        extraction_blocks = [
            step["run"]
            for job in jobs.values()
            for step in job.get("steps", [])
            if "tar --extract --gzip --file" in str(step.get("run", ""))
            and "--directory \"$age_dir\" age/age" in str(step.get("run", ""))
        ]
        self.assertEqual(len(extraction_blocks), 6)
        for script in extraction_blocks:
            self.assertIn('age_bin="$age_dir/age/age"', script)
            self.assertIn('test -f "$age_bin" && test ! -L "$age_bin" && test -x "$age_bin"', script)
            self.assertNotIn('"$age_dir/age"', script)

        scratch = tempfile.TemporaryDirectory(prefix="workflow-age-layout-")
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        os.chmod(root, 0o700)

        # The workflow's pinned archive digest is the trust boundary. Use that
        # exact archive and let the parsed workflow steps perform extraction.
        import urllib.request

        with urllib.request.urlopen(transport.AGE_ARCHIVE_URL, timeout=30) as response:
            archive_bytes = response.read(25 * 1024 * 1024 + 1)
        self.assertLessEqual(len(archive_bytes), 25 * 1024 * 1024)
        self.assertEqual(hashlib.sha256(archive_bytes).hexdigest(), transport.AGE_ARCHIVE_SHA256)
        archive_source = root / "pinned-age.tar.gz"
        archive_source.write_bytes(archive_bytes)
        archive_source.chmod(0o600)

        # Generate an isolated recipient so the exact workflow encrypt and
        # consumer-decrypt scripts can run end to end without any real key.
        key_tools = root / "key-tools"
        key_tools.mkdir(mode=0o700)
        extracted_keygen = subprocess.run(
            ["tar", "--extract", "--gzip", "--file", str(archive_source), "--directory", str(key_tools), "age/age-keygen"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(extracted_keygen.returncode, 0, "pinned age-keygen archive member extraction failed")
        age_keygen = key_tools / "age/age-keygen"
        self.assertTrue(age_keygen.is_file() and os.access(age_keygen, os.X_OK))
        identity = root / "identity.txt"
        generated = subprocess.run(
            [str(age_keygen), "-o", str(identity)], capture_output=True, text=True, check=False
        )
        self.assertEqual(generated.returncode, 0, "synthetic age identity generation failed")
        identity.chmod(0o600)
        recipient_result = subprocess.run(
            [str(age_keygen), "-y", str(identity)], capture_output=True, text=True, check=False
        )
        self.assertEqual(recipient_result.returncode, 0, "synthetic age recipient derivation failed")
        recipient = recipient_result.stdout.strip()
        self.assertRegex(recipient, r"age1[0-9a-z]{58}")

        curl_dir = root / "curl-bin"
        curl_dir.mkdir(mode=0o700)
        curl = curl_dir / "curl"
        curl.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "output=\n"
            "while [ $# -gt 0 ]; do\n"
            "  if [ \"$1\" = --output ]; then shift; output=$1; fi\n"
            "  shift\n"
            "done\n"
            "test -n \"$output\"\n"
            "cp -- \"$WORKFLOW_TEST_AGE_ARCHIVE\" \"$output\"\n",
            encoding="ascii",
        )
        curl.chmod(0o700)

        runner_sha = "a" * 40
        run_id = "567890"
        attempt = "1"
        input_value = {
            "schema": "1",
            "confirmation": "RUN-PRODUCTION-EXTERNAL-LOAD",
            "target_sha": runner_sha,
            "control_email": "fixture-control@example.invalid",
            "setup_concurrency": "8",
            "run_id": run_id,
            "profile": "external-vote",
            "tournament_count": "1",
            "users_per_tournament": "14",
            "timeout_diagnostics": "false",
            "fixture_kind": "external_load",
        }
        input_bytes = (
            json.dumps(input_value, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        users = [
            {
                "user_id": f"synthetic-user-{index:02d}",
                "tournament_slug": "synthetic-fixture-slug",
                "session_token": f"SYNTHETIC_SESSION_{index:02d}_not_a_credential",
                "csrf_token": f"SYNTHETIC_CSRF_{index:02d}_not_a_credential",
            }
            for index in range(14)
        ]
        manifest_value = {
            "schema": 2,
            "purpose": "external_ready_vote",
            "origin": "https://old-sparky.com",
            "session_cookie_name": "__Host-session",
            "csrf_cookie_name": "__Host-session_csrf",
            "marker": "preprod202611020000abcd",
            "created_at": "2026-11-02T00:00:00Z",
            "tournaments": [
                {"id": "synthetic-tournament-id", "slug": "synthetic-fixture-slug", "user_count": 14}
            ],
            "users": users,
            "runner_sha": runner_sha,
            "app_target_sha": runner_sha,
            "source_binding": None,
            "source_binding_sha256": None,
        }
        manifest_bytes = (
            json.dumps(manifest_value, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        synthetic_credentials = {
            input_value["control_email"],
            *(row["user_id"] for row in users),
            *(row["tournament_slug"] for row in users),
            *(row["session_token"] for row in users),
            *(row["csrf_token"] for row in users),
        }

        def run_yaml_step(
            step: dict[str, object],
            runner_temp: Path,
            *,
            extra_env: dict[str, str] | None = None,
            cwd: Path | None = None,
        ) -> subprocess.CompletedProcess[str]:
            env = {
                "RUNNER_TEMP": str(runner_temp),
                "WORKFLOW_TEST_AGE_ARCHIVE": str(archive_source),
                "PATH": f"{curl_dir}:{os.defpath}",
                "LC_ALL": "C",
            }
            if extra_env:
                env.update(extra_env)
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", str(step["run"])],
                env=env,
                cwd=cwd or REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"workflow step failed: {step.get('name', '<unnamed>')}",
            )
            return result

        encrypt_temp = root / "encrypt-runner-temp"
        encrypt_temp.mkdir(mode=0o700)
        plain_input = encrypt_temp / "platform-production-external-load-input.json"
        plain_input.write_bytes(input_bytes)
        plain_input.chmod(0o600)
        self.assertEqual(str(encrypt_step["run"]).count(transport.AGE_RECIPIENT), 1)
        run_yaml_step(
            {**encrypt_step, "run": str(encrypt_step["run"]).replace(
                transport.AGE_RECIPIENT, recipient
            )},
            encrypt_temp,
        )
        encrypted_input = encrypt_temp / "fixture-private-transport/private-input.json.age"
        self.assertTrue(encrypted_input.is_file())
        self.assertEqual(encrypted_input.stat().st_mode & 0o777, 0o600)
        encrypted_input_bytes = encrypted_input.read_bytes()
        self.assertTrue(
            all(value.encode("ascii") not in encrypted_input_bytes for value in synthetic_credentials),
            "synthetic private input values appeared in the ciphertext artifact",
        )
        installed = encrypt_temp / "fixture-age-tools/age/age"
        self.assertTrue(installed.is_file() and os.access(installed, os.X_OK))
        self.assertTrue((encrypt_temp / "fixture-age-tools/age").is_dir())

        setup_temp = root / "fixture-setup-runner-temp"
        setup_temp.mkdir(mode=0o700)
        handoff = setup_temp / "external-input-ciphertext/private-input.json.age"
        handoff.parent.mkdir(mode=0o700)
        shutil.copyfile(encrypted_input, handoff)
        handoff.chmod(0o600)
        identity_line = next(
            line for line in identity.read_text(encoding="ascii").splitlines()
            if line.startswith("AGE-SECRET-KEY-1")
        )
        run_yaml_step(
            stage_fixture_identity,
            setup_temp,
            extra_env={"FIXTURE_MANIFEST_AGE_IDENTITY": identity_line},
        )
        run_yaml_step(decrypt_input_step, setup_temp)
        staged_input = setup_temp / "external-input/platform-production-external-load-input.json"
        self.assertEqual(staged_input.read_bytes(), input_bytes)
        self.assertEqual(staged_input.stat().st_mode & 0o777, 0o600)
        self.assertFalse((setup_temp / "fixture-private-identity/identity.txt").exists())

        external_fixture = setup_temp / "external-fixture"
        external_fixture.mkdir(mode=0o700)
        manifest_path = external_fixture / "manifest.json"
        manifest_path.write_bytes(manifest_bytes)
        manifest_path.chmod(0o600)
        run_yaml_step(
            validate_manifest_step,
            setup_temp,
            extra_env={"TARGET_SHA": runner_sha, "APP_TARGET_SHA": runner_sha},
        )
        encrypted_manifest_step = {
            **encrypt_manifest_step,
            "run": str(encrypt_manifest_step["run"]).replace(
                transport.AGE_RECIPIENT, recipient
            ),
        }
        run_yaml_step(
            encrypted_manifest_step,
            setup_temp,
            extra_env={
                "GITHUB_RUN_ID": run_id,
                "GITHUB_RUN_ATTEMPT": attempt,
                "TARGET_SHA": runner_sha,
                "APP_TARGET_SHA": runner_sha,
            },
        )
        manifest_cipher = external_fixture / "manifest.json.age"
        descriptor = external_fixture / "fixture-descriptor.json"
        self.assertTrue(manifest_cipher.is_file() and descriptor.is_file())
        self.assertEqual(manifest_cipher.stat().st_mode & 0o777, 0o600)
        self.assertEqual(descriptor.stat().st_mode & 0o777, 0o600)
        public_descriptor = json.loads(descriptor.read_text(encoding="ascii"))
        self.assertNotIn("control_email", public_descriptor["input"])
        self.assertNotIn("session_token", json.dumps(public_descriptor, sort_keys=True))
        self.assertNotIn("csrf_token", json.dumps(public_descriptor, sort_keys=True))
        manifest_cipher_bytes = manifest_cipher.read_bytes()
        descriptor_bytes = descriptor.read_bytes()
        for public_payload in (manifest_cipher_bytes, descriptor_bytes):
            self.assertTrue(
                all(value.encode("ascii") not in public_payload for value in synthetic_credentials),
                "synthetic fixture credentials appeared in a public handoff",
            )

        load_temp = root / "load-client-runner-temp"
        load_temp.mkdir(mode=0o700)
        load_descriptor = load_temp / "external-input/fixture-descriptor.json"
        load_descriptor.parent.mkdir(mode=0o700)
        shutil.copyfile(descriptor, load_descriptor)
        load_descriptor.chmod(0o600)
        load_cipher = load_temp / "external-manifest-ciphertext/manifest.json.age"
        load_cipher.parent.mkdir(mode=0o700)
        shutil.copyfile(manifest_cipher, load_cipher)
        load_cipher.chmod(0o600)
        run_yaml_step(
            stage_load_identity,
            load_temp,
            extra_env={"FIXTURE_MANIFEST_AGE_IDENTITY": identity_line},
        )
        run_yaml_step(decrypt_manifest_step, load_temp)
        load_manifest = load_temp / "external-manifest/manifest.json"
        self.assertEqual(load_manifest.read_bytes(), manifest_bytes)
        self.assertEqual(load_manifest.stat().st_mode & 0o777, 0o600)
        run_yaml_step(
            validate_descriptor_step,
            load_temp,
            extra_env={
                "GITHUB_RUN_ID": run_id,
                "GITHUB_RUN_ATTEMPT": attempt,
                "TARGET_SHA": runner_sha,
                "APP_TARGET_SHA": runner_sha,
                "PYTHONPATH": str(REPO_ROOT / "platform"),
            },
        )

        setup_github_env = setup_temp / "github-env"
        setup_github_output = setup_temp / "github-output"
        setup_github_env.touch(mode=0o600)
        setup_github_output.touch(mode=0o600)
        run_yaml_step(
            fixture_cleanup_step,
            setup_temp,
            extra_env={
                "GITHUB_ENV": str(setup_github_env),
                "GITHUB_OUTPUT": str(setup_github_output),
            },
        )
        self.assertFalse(staged_input.exists())
        self.assertFalse(manifest_path.exists())
        self.assertFalse(handoff.exists())
        self.assertFalse((setup_temp / "fixture-private-identity").exists())
        self.assertFalse((setup_temp / "fixture-age-tools").exists())
        self.assertFalse((setup_temp / "age-v1.3.2.tar.gz").exists())
        self.assertFalse((setup_temp / "external-input-ciphertext").exists())
        self.assertTrue(manifest_cipher.is_file())
        self.assertTrue(descriptor.is_file())
        self.assertIn("ssh_cleanup_status=0", setup_github_output.read_text(encoding="ascii"))

        run_yaml_step(load_cleanup_step, load_temp)
        self.assertFalse(load_manifest.exists())
        self.assertFalse(load_cipher.exists())
        self.assertFalse((load_temp / "fixture-private-identity").exists())
        self.assertFalse((load_temp / "fixture-age-tools").exists())
        self.assertFalse((load_temp / "age-v1.3.2.tar.gz").exists())
        self.assertTrue(load_descriptor.is_file())

    def test_public_artifacts_are_ciphertext_and_fixture_key_is_scoped(self) -> None:
        document = yaml.safe_load((WORKFLOW_ROOT / "platform-production-external-load.yml").read_text(encoding="utf-8"))
        jobs = document["jobs"]
        validator = jobs["validate-external-inputs"]
        resolver = jobs["resolve-load-source-and-profile"]
        resolver_steps = resolver["steps"]
        self.assertLess(
            next(i for i, step in enumerate(resolver_steps) if "Mask and remove control identity" in step.get("name", "")),
            next(i for i, step in enumerate(resolver_steps) if step.get("uses", "").startswith("actions/checkout@")),
        )
        self.assertNotIn("control_email", str(resolver.get("env", {})))
        self.assertNotIn("${{ inputs.control_email }}", str(resolver_steps[1:]))
        self.assertNotIn("FIXTURE_MANIFEST_AGE_IDENTITY", str(resolver))
        self.assertNotIn("actions/checkout@", str(validator))
        self.assertNotIn("platform_load.py", str(validator))
        self.assertNotIn("CONTROL_EMAIL", str(validator.get("env", {})))
        for workflow_name in ("platform-production-external-load.yml", "platform-production-retained-load-cleanup.yml"):
            workflow_doc = yaml.safe_load((WORKFLOW_ROOT / workflow_name).read_text(encoding="utf-8"))
            for job_name, job in workflow_doc["jobs"].items():
                steps = job.get("steps", [])
                checkout_indexes = [i for i, step in enumerate(steps) if str(step.get("uses", "")).startswith("actions/checkout@")]
                helper_indexes = [i for i, step in enumerate(steps) if "run" in step and "platform/tools/" in step["run"]]
                if helper_indexes:
                    self.assertTrue(checkout_indexes, f"{workflow_name}:{job_name} invokes repository helpers without checkout")
                    self.assertLess(min(checkout_indexes), min(helper_indexes), f"{workflow_name}:{job_name} invokes a helper before checkout")
                if checkout_indexes:
                    scrub_indexes = [i for i, step in enumerate(steps) if "run" in step and "GITHUB_EVENT_PATH" in step["run"] and "::add-mask::" in step["run"] and "os.replace" in step["run"]]
                    self.assertTrue(scrub_indexes, f"{workflow_name}:{job_name} checks out source without a mask-and-scrub event step")
                    self.assertLess(min(scrub_indexes), min(checkout_indexes), f"{workflow_name}:{job_name} checks out source before event scrub")
        self.assertIn("platform-production-external-load-private-input-", str(validator))
        self.assertIn("age" , str(validator))
        self.assertIn("--encrypt --recipient", str(validator))
        self.assertNotIn("actions/checkout@", str(validator))
        self.assertNotIn("platform_load.py", str(validator))
        self.assertNotIn("FIXTURE_MANIFEST_AGE_IDENTITY", str(validator))
        self.assertIn("fixture-manifest", jobs["load-client"].get("environment"))
        self.assertNotIn("PROD_SSH_KEY", str(jobs["load-client"]))
        for job_name in ("fixture-setup", "cpu-diagnostic-pair", "fixture-finalize"):
            self.assertEqual(jobs[job_name].get("environment"), "production")
            self.assertIn("FIXTURE_MANIFEST_AGE_IDENTITY", str(jobs[job_name]))
            self.assertNotIn("FIXTURE_MANIFEST_AGE_IDENTITY", str(jobs[job_name].get("env", {})))
            identity_steps = [step for step in jobs[job_name]["steps"] if "FIXTURE_MANIFEST_AGE_IDENTITY" in str(step.get("env", {}))]
            self.assertEqual(len(identity_steps), 1)
            self.assertIn("Stage", identity_steps[0].get("name", ""))
            self.assertIn("AGE-SECRET-KEY-1", identity_steps[0].get("run", ""))
            self.assertIn("os.O_EXCL", identity_steps[0].get("run", ""))
            self.assertNotIn("curl ", identity_steps[0].get("run", ""))
            self.assertNotIn("--decrypt", identity_steps[0].get("run", ""))
            decrypt_steps = [step for step in jobs[job_name]["steps"] if "--decrypt --identity" in str(step.get("run", ""))]
            self.assertEqual(len(decrypt_steps), 1)
            self.assertNotIn("FIXTURE_MANIFEST_AGE_IDENTITY", str(decrypt_steps[0].get("env", {})))
            steps = jobs[job_name]["steps"]
            stage_index = steps.index(identity_steps[0])
            decrypt_index = steps.index(decrypt_steps[0])
            self.assertLess(stage_index, decrypt_index)
            checkout_indexes = [i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/checkout@")]
            if checkout_indexes:
                self.assertLess(decrypt_index, min(checkout_indexes))
        for job_name in ("derive-diagnostic-source-binding", "evaluate-load"):
            self.assertNotIn("FIXTURE_MANIFEST_AGE_IDENTITY", str(jobs[job_name]))
            self.assertIn("fixture-descriptor", str(jobs[job_name]))
        workflow_text = (WORKFLOW_ROOT / "platform-production-external-load.yml").read_text(encoding="utf-8")
        self.assertNotIn("${{ inputs.control_email }}", workflow_text)
        self.assertIn("--recipient age1egptr8zhhvfe0ggmyu4cr9azhfsjpvvn3p5eyppznnzx8c5yedjsyeaasa", workflow_text)
        self.assertIn("cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10", workflow_text)
        self.assertNotIn("platform-production-external-load-manifest-${{ github.run_id }}-${{ github.run_attempt }}\n          path: ${{ runner.temp }}/external-fixture/manifest.json", workflow_text)
        for job_name in ("validate-external-inputs", "fixture-setup"):
            uploads = [
                step for step in jobs[job_name]["steps"]
                if isinstance(step.get("uses"), str) and "actions/upload-artifact@" in step["uses"]
            ]
            for step in uploads:
                path = str(step.get("with", {}).get("path", ""))
                self.assertNotIn("/manifest.json\n", path)
                self.assertNotIn("platform-production-external-load-input.json\n", path)
            self.assertTrue(any(".age" in str(step.get("with", {}).get("path", "")) for step in uploads))

    def test_candidate_evaluator_and_cleanup_event_scrub_masks_before_rewrite(self) -> None:
        workflow = yaml.safe_load((WORKFLOW_ROOT / "platform-production-external-load.yml").read_text(encoding="utf-8"))
        external_scrubs = {
            "cpu-diagnostic-pair": "Remove control identity from the runner event file",
            "evaluate-load": "Remove control identity from the runner event file",
            "resolve-host-tools-pin": "Mask and remove control identity before host-tools resolution",
            "fixture-setup": "Mask and remove control identity before setup",
        }
        for job_name, scrub_name in external_scrubs.items():
            with self.subTest(job=job_name), tempfile.TemporaryDirectory() as temporary:
                event_path = Path(temporary) / "event.json"
                email = "private-address+fixture@example.invalid"
                event_path.write_text(json.dumps({"inputs": {"control_email": email, "profile_id": "external-vote"}, "repository": {"description": "metadata mentions control_email"}}), encoding="utf-8")
                event_path.chmod(0o600)
                steps = workflow["jobs"][job_name]["steps"]
                scrub = next(step for step in steps if step.get("name") == scrub_name)
                self.assertLess(scrub["run"].index("::add-mask::"), scrub["run"].index("os.replace(tmp,path)"))
                result = subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", scrub["run"]],
                    env={**os.environ, "GITHUB_EVENT_PATH": str(event_path)},
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("::add-mask::private-address+fixture@example.invalid", result.stdout)
                self.assertNotIn(email, event_path.read_text(encoding="utf-8"))
                scrubbed = json.loads(event_path.read_text(encoding="utf-8"))
                self.assertNotIn("control_email", scrubbed["inputs"])
                self.assertEqual(scrubbed["repository"]["description"], "metadata mentions control_email")

        validator = workflow["jobs"]["validate-external-inputs"]
        with tempfile.TemporaryDirectory() as temporary:
            event_path = Path(temporary) / "event.json"
            runner_temp = Path(temporary)
            email = "private-address+fixture@example.invalid"
            inputs = {
                "confirmation": "RUN-PRODUCTION-EXTERNAL-LOAD", "control_email": email,
                "profile_id": "authenticated-page-load-v1", "timeout_diagnostics": "false",
                "cpu_diagnostic_pair": "false", "cpu_diagnostic_only": "false",
                "ssr_capacity_diagnostic_only": "false", "ssr_capacity_concurrency": "8",
                "ssr_capacity_workload": "authenticated_workspace_read_capacity_v1",
            }
            event_path.write_text(json.dumps({"inputs": inputs, "repository": {"description": "metadata mentions control_email"}}), encoding="utf-8")
            event_path.chmod(0o600)
            scrub = validator["steps"][0]
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", scrub["run"]],
                env={**os.environ, "GITHUB_EVENT_PATH": str(event_path), "RUNNER_TEMP": str(runner_temp)},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("::add-mask::private-address+fixture@example.invalid", result.stdout)
            email_path = runner_temp / "platform-production-control-email"
            self.assertEqual(email_path.read_text(encoding="ascii"), f"{email.lower()}\n")
            self.assertEqual(email_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(email, event_path.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(event_path.read_text(encoding="utf-8"))["repository"]["description"], "metadata mentions control_email")

            serializer = next(step for step in validator["steps"] if step.get("name") == "Serialize the private external-load input")
            script = serializer["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
            private_input = runner_temp / "external-load-input.json"
            args = [
                str(private_input), str(email_path), "RUN-PRODUCTION-EXTERNAL-LOAD", "a" * 40, "a" * 40,
                "8", "567890", "40", "500", "false", "false", "false", "8",
                "authenticated_workspace_read_capacity_v1", "", "",
            ]
            result = subprocess.run(
                [sys.executable, "-", *args], input=script, text=True,
                env=os.environ.copy(),
                capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(email_path.exists())
            private_payload = json.loads(private_input.read_text(encoding="ascii"))
            self.assertEqual(private_payload["control_email"], email.lower())
            self.assertEqual(private_payload["target_sha"], "a" * 40)
            self.assertEqual(private_payload["tournament_count"], "40")
            self.assertEqual(private_payload["users_per_tournament"], "500")

        host_steps = workflow["jobs"]["resolve-host-tools-pin"]["steps"]
        self.assertLess(
            next(i for i, step in enumerate(host_steps) if step.get("name") == "Mask and remove control identity before host-tools resolution"),
            next(i for i, step in enumerate(host_steps) if step.get("uses", "").startswith("actions/checkout@")),
        )

        cleanup = yaml.safe_load((WORKFLOW_ROOT / "platform-production-retained-load-cleanup.yml").read_text(encoding="utf-8"))
        cleanup_text = (WORKFLOW_ROOT / "platform-production-retained-load-cleanup.yml").read_text(encoding="utf-8")
        self.assertNotIn("${{ inputs.control_email }}", cleanup_text)
        cleanup_steps = cleanup["jobs"]["cleanup"]["steps"]
        cleanup_validation = next(step for step in cleanup_steps if step.get("name") == "Validate explicit cleanup confirmation")
        self.assertIn("retained-cleanup-control-email", cleanup_validation.get("run", ""))
        self.assertNotIn("CONTROL_EMAIL", str(cleanup_validation.get("env", {})))
        cleanup_remove_index = next(i for i, step in enumerate(cleanup_steps) if step.get("name") == "Remove staged cleanup control identity")
        cleanup_ssh_index = next(i for i, step in enumerate(cleanup_steps) if step.get("name") == "Configure production SSH")
        self.assertLess(cleanup_remove_index, cleanup_ssh_index)
        resolve_steps = cleanup["jobs"]["resolve-host-tools-pin"]["steps"]
        self.assertLess(
            next(i for i, step in enumerate(resolve_steps) if step.get("name") == "Mask and remove control identity before candidate checkout"),
            next(i for i, step in enumerate(resolve_steps) if step.get("uses", "").startswith("actions/checkout@")),
        )
        for job_name, expected_step in (("resolve-host-tools-pin", "Mask and remove control identity before candidate checkout"), ("cleanup", "Mask and remove control identity before cleanup processing")):
            with self.subTest(cleanup_job=job_name), tempfile.TemporaryDirectory() as temporary:
                event_path = Path(temporary) / "event.json"
                email = "private-address+fixture@example.invalid"
                event_path.write_text(json.dumps({"inputs": {"control_email": email, "confirmation": "DELETE-PRODUCTION-RETAINED-LOAD", "load_run_id": "12345"}, "repository": {"description": "metadata mentions control_email"}}), encoding="utf-8")
                event_path.chmod(0o600)
                step = next(step for step in cleanup["jobs"][job_name]["steps"] if step.get("name") == expected_step)
                runner_temp = Path(temporary)
                result = subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", step["run"]],
                    env={**os.environ, "GITHUB_EVENT_PATH": str(event_path), "RUNNER_TEMP": str(runner_temp)},
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("::add-mask::private-address+fixture@example.invalid", result.stdout)
                self.assertNotIn(email, event_path.read_text(encoding="utf-8"))
                self.assertEqual(json.loads(event_path.read_text(encoding="utf-8"))["repository"]["description"], "metadata mentions control_email")
                email_path = runner_temp / "retained-cleanup-control-email"
                if job_name == "cleanup":
                    self.assertEqual(email_path.read_text(encoding="ascii"), f"{email.lower()}\n")
                    self.assertEqual(email_path.stat().st_mode & 0o777, 0o600)
                else:
                    self.assertFalse(email_path.exists())
                if job_name == "cleanup":
                    remove_step = next(step for step in cleanup["jobs"][job_name]["steps"] if step.get("name") == "Remove staged cleanup control identity")
                    removed = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", remove_step["run"]],
                        env={**os.environ, "RUNNER_TEMP": str(runner_temp)},
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(removed.returncode, 0, removed.stderr)
                    self.assertFalse(email_path.exists())

        for job_name, expected_step in (
            ("resolve-host-tools-pin", "Mask and remove control identity before candidate checkout"),
            ("cleanup", "Mask and remove control identity before cleanup processing"),
        ):
            for description in (
                "copied private-address+fixture@example.invalid",
                "copied private-address+fixture\\u0040example.invalid",
            ):
                with self.subTest(cleanup_job=job_name, credential_copy=description.startswith("copied private-address+")), tempfile.TemporaryDirectory() as temporary:
                    event_path = Path(temporary) / "event.json"
                    email = "private-address+fixture@example.invalid"
                    raw = (
                        '{"inputs":{"control_email":"private-address+fixture@example.invalid",'
                        '"confirmation":"DELETE-PRODUCTION-RETAINED-LOAD","load_run_id":"12345"},'
                        f'"repository":{{"description":"{description}"}}}}'
                    )
                    event_path.write_text(raw, encoding="utf-8")
                    event_path.chmod(0o600)
                    step = next(step for step in cleanup["jobs"][job_name]["steps"] if step.get("name") == expected_step)
                    result = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", step["run"]],
                        env={**os.environ, "GITHUB_EVENT_PATH": str(event_path), "RUNNER_TEMP": temporary},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(email, result.stderr)
                    self.assertIn("workflow event sanitization failed closed", result.stderr)


if __name__ == "__main__":
    unittest.main()
