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


if __name__ == "__main__":
    unittest.main()
