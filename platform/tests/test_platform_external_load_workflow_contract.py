"""Fail-closed DAG contracts for the production external-load workflow.

These tests intentionally inspect the workflow as source.  The production
workflow has several ``always()`` branches so cleanup can run after a fault;
the final passing artifact is therefore guarded by an explicit truth table
instead of relying on GitHub's implicit job conclusion propagation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-external-load.yml"
CANARY_WORKFLOW = REPO_ROOT / ".github/workflows/platform-load-containment-canary.yml"


def _jobs(source: str) -> dict[str, str]:
    matches = re.finditer(
        r"^  (?P<name>[A-Za-z0-9_-]+):\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        source,
        re.MULTILINE | re.DOTALL,
    )
    return {match.group("name"): match.group("body") for match in matches}


def _pass_truth_table(state: dict[str, object]) -> bool:
    """Model the final gate's authoritative status inputs."""

    return all(
        (
            state["validate_result"] == "success",
            state["setup_result"] == "success",
            state["setup_status"] == "0",
            state["setup_ssh_cleanup_status"] == "0",
            state["load_result"] == "success",
            state["load_status"] == "0",
            state["candidate_state"] in {"produced", "pending_origin", "timeout_diagnostics"},
            state["report_ready"] == "1",
            state["namespace_barrier_result"] == "success",
            state["namespace_closed_status"] == "0",
            state["manual_barrier_result"] == "success",
            state["manual_required"] == "0",
            state["finalize_result"] == "success",
            state["remote_status"] == "0",
            state["observer_ready"] == "1",
            state["finalize_status"] == "0",
            state["cleanup_status"] == "0",
            state["cleanup_exports_status"] == "0",
            state["ssh_cleanup_status"] == "0",
            state["cleanup_identity_status"] == "0",
            state["handoff_status"] == "0",
            state["candidate_artifact_status"] == "0",
            state["origin_artifact_status"] == "0",
            state["evaluation_status"] == "0",
            state["acceptance_status"] in {"accepted", "diagnostic_complete"},
            state["sanitizer_status"] == "0",
        )
    )


def _evidence_publish_truth_table(state: dict[str, object]) -> bool:
    return all(
        (
            state["validate_result"] == "success",
            state["setup_result"] == "success",
            state["setup_status"] == "0",
            state["setup_ssh_cleanup_status"] == "0",
            state["load_result"] == "success",
            state["load_status"] == "0",
            state["candidate_state"] in {"produced", "pending_origin"},
            state["report_ready"] == "1",
            state["namespace_barrier_result"] == "success",
            state["namespace_closed_status"] == "0",
            state["manual_barrier_result"] == "success",
            state["manual_required"] == "0",
            state["finalize_result"] == "success",
            state["remote_status"] == "0",
            state["observer_ready"] == "1",
            state["finalize_status"] == "0",
            state["cleanup_status"] == "0",
            state["cleanup_exports_status"] == "0",
            state["ssh_cleanup_status"] == "0",
            state["cleanup_identity_status"] == "0",
            state["handoff_status"] == "0",
            state["candidate_artifact_status"] == "0",
            state["origin_artifact_status"] == "0",
            state["evaluation_status"] == "0",
            state["acceptance_status"] in {"accepted", "slo_failed"},
            state["sanitizer_status"] == "0",
        )
    )


def _step_script(job: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    start = job.index(marker)
    rest = job[start + len(marker) :]
    end = rest.find("\n      - name: ")
    block = rest if end < 0 else rest[:end]
    lines = block.splitlines()
    run_line = lines.index("        run: |")
    body: list[str] = []
    for line in lines[run_line + 1 :]:
        if line.startswith("          "):
            body.append(line[10:])
        elif line == "":
            body.append("")
        else:
            break
    return textwrap.dedent("\n".join(body))


class ExternalLoadWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = WORKFLOW.read_text(encoding="utf-8")
        cls.jobs = _jobs(cls.source)
        cls.canary_source = CANARY_WORKFLOW.read_text(encoding="utf-8")

    def test_dag_has_explicit_result_barriers_and_always_cleanup(self) -> None:
        self.assertIn("needs.fixture-setup.result == 'success'", self.jobs["load-client"])
        self.assertIn("needs.validate-external-inputs.result == 'success'", self.jobs["load-client"])
        self.assertIn("if: ${{ always() }}", self.jobs["fixture-finalize"])
        self.assertIn("if: ${{ always() }}", self.jobs["evaluate-load"])
        self.assertIn("needs.fixture-setup.result", self.jobs["evaluate-load"])
        self.assertIn("needs.load-client.result", self.jobs["evaluate-load"])
        self.assertIn("needs.fixture-finalize.result", self.jobs["evaluate-load"])
        self.assertIn("needs.namespace-containment-barrier.outputs.namespace_closed_status", self.jobs["fixture-finalize"])
        self.assertIn("emergency/manual", self.jobs["namespace-containment-manual-barrier"])
        for name in ("Exact cleanup of external fixture", "Remove finalizer SSH material"):
            self.assertIn(f"- name: {name}", self.jobs["fixture-finalize"])
            step = self.jobs["fixture-finalize"].split(f"- name: {name}", 1)[1]
            self.assertIn("if: ${{ always() }}", step)

    def test_every_authoritative_status_is_consumed_by_the_gate(self) -> None:
        gate = self.jobs["evaluate-load"].split("- name: Enforce external load", 1)[1]
        statuses = (
            "needs.validate-external-inputs.result",
            "needs.fixture-setup.result",
            "needs.fixture-setup.outputs.setup_status",
            "needs.fixture-setup.outputs.ssh_cleanup_status",
            "needs.load-client.result",
            "needs.load-client.outputs.load_status",
            "needs.load-client.outputs.candidate_state",
            "needs.load-client.outputs.report_ready",
            "needs.namespace-containment-barrier.result",
            "needs.namespace-containment-barrier.outputs.namespace_closed_status",
            "needs.namespace-containment-manual-barrier.result",
            "needs.namespace-containment-manual-barrier.outputs.manual_required",
            "needs.fixture-finalize.result",
            "needs.fixture-finalize.outputs.remote_status",
            "needs.fixture-finalize.outputs.observer_ready",
            "needs.fixture-finalize.outputs.finalize_status",
            "needs.fixture-finalize.outputs.cleanup_status",
            "needs.fixture-finalize.outputs.cleanup_exports_status",
            "needs.fixture-finalize.outputs.ssh_cleanup_status",
            "needs.fixture-finalize.outputs.cleanup_identity_status",
            "needs.fixture-finalize.outputs.handoff_status",
            "needs.fixture-finalize.outputs.candidate_artifact_status",
            "steps.verify-evaluator-artifacts.outputs.candidate_artifact_status",
            "steps.verify-evaluator-artifacts.outputs.origin_artifact_status",
            "steps.evaluate-load.outputs.evaluation_status",
            "steps.evaluate-load.outputs.acceptance_status",
            "steps.sanitize.outputs.sanitizer_status",
        )
        for status in statuses:
            with self.subTest(status=status):
                self.assertIn(status, gate)

    def test_fault_injection_truth_table_rejects_each_phase_failure(self) -> None:
        passing = {
            "validate_result": "success",
            "setup_result": "success",
            "setup_status": "0",
            "setup_ssh_cleanup_status": "0",
            "load_result": "success",
            "load_status": "0",
            "candidate_state": "produced",
            "report_ready": "1",
            "namespace_barrier_result": "success",
            "namespace_closed_status": "0",
            "manual_barrier_result": "success",
            "manual_required": "0",
            "finalize_result": "success",
            "remote_status": "0",
            "observer_ready": "1",
            "finalize_status": "0",
            "cleanup_status": "0",
            "cleanup_exports_status": "0",
            "ssh_cleanup_status": "0",
            "cleanup_identity_status": "0",
            "handoff_status": "0",
            "candidate_artifact_status": "0",
            "origin_artifact_status": "0",
            "evaluation_status": "0",
            "acceptance_status": "accepted",
            "sanitizer_status": "0",
        }
        self.assertTrue(_pass_truth_table(passing))
        for key in passing:
            with self.subTest(fault=key):
                faulted = dict(passing)
                faulted[key] = (
                    "failure"
                    if key.endswith("result")
                    else ("0" if passing[key] == "1" else "1")
                )
                self.assertFalse(_pass_truth_table(faulted))
        missing_finalizer_cleanup = dict(passing)
        missing_finalizer_cleanup["ssh_cleanup_status"] = ""
        self.assertFalse(_pass_truth_table(missing_finalizer_cleanup))

    def test_failed_handoff_still_requires_cleanup_and_cleanup_failure_dominates(self) -> None:
        passing = {
            "validate_result": "success",
            "setup_result": "success",
            "setup_status": "0",
            "setup_ssh_cleanup_status": "0",
            "load_result": "success",
            "load_status": "0",
            "candidate_state": "produced",
            "report_ready": "1",
            "namespace_barrier_result": "success",
            "namespace_closed_status": "0",
            "manual_barrier_result": "success",
            "manual_required": "0",
            "finalize_result": "success",
            "remote_status": "0",
            "observer_ready": "1",
            "finalize_status": "0",
            "cleanup_status": "0",
            "cleanup_exports_status": "0",
            "ssh_cleanup_status": "0",
            "cleanup_identity_status": "0",
            "handoff_status": "0",
            "candidate_artifact_status": "0",
            "origin_artifact_status": "0",
            "evaluation_status": "0",
            "acceptance_status": "accepted",
            "sanitizer_status": "0",
        }
        failed_handoff = dict(passing)
        failed_handoff["handoff_status"] = "1"
        self.assertFalse(_pass_truth_table(failed_handoff))
        failed_cleanup = dict(failed_handoff)
        failed_cleanup["cleanup_status"] = "1"
        self.assertFalse(_pass_truth_table(failed_cleanup))

        finalizer = self.jobs["fixture-finalize"]
        cleanup = finalizer.split("- name: Exact cleanup of external fixture", 1)[1].split(
            "- name: Remove untrusted origin transport logs", 1
        )[0]
        self.assertIn("cleanup_identity_status", cleanup)
        self.assertNotIn('HANDOFF_STATUS" == 0', cleanup)
        self.assertIn("external-cleanup <", cleanup)
        self.assertIn("external-cleanup-exports <", cleanup)
        self.assertIn("cleanup_status=1", cleanup)

    def test_handoffs_bind_run_attempt_sha_and_archive_digest(self) -> None:
        self.assertIn("GITHUB_RUN_ATTEMPT", self.source)
        self.assertIn('workflow_run.get("id") != int(run_id)', self.source)
        self.assertIn('workflow_run.get("head_sha") != target_sha', self.source)
        for artifact in (
            "platform-production-external-load-input-${{ github.run_id }}-${{ github.run_attempt }}",
            "platform-production-external-load-manifest-${{ github.run_id }}-${{ github.run_attempt }}",
            "platform-production-external-load-client-${{ github.run_id }}-${{ github.run_attempt }}",
            "platform-production-external-load-origin-${{ github.run_id }}-${{ github.run_attempt }}",
        ):
            self.assertIn(artifact, self.source)
        self.assertNotIn("ARTIFACT_DIGEST", self.source)
        self.assertNotIn("artifact-digest", self.source)
        self.assertNotIn("needs.fixture-setup.outputs.manifest_artifact_digest", self.source)
        self.assertEqual(self.source.count('expected_digest = payload.get("digest")'), 4)
        self.assertIn('re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest)', self.source)
        self.assertIn('actual_digest != expected_digest.removeprefix("sha256:")', self.source)
        self.assertIn("if-no-files-found: error", self.source)

    def test_metadata_digest_verifiers_accept_producer_metadata_and_reject_mismatches(self) -> None:
        blocks = re.findall(
            r"(?m)^[ \t]+import hashlib\n(?P<script>.*?)(?=^[ \t]+PY$)",
            self.source,
            re.DOTALL,
        )
        scripts = [
            "import hashlib\n" + textwrap.dedent(block)
            for block in blocks
            if "metadata_path, archive_path, artifact_id, artifact_name, run_id, target_sha = sys.argv[1:]" in block
        ]
        self.assertEqual(len(scripts), 4)

        archive_bytes = b"synthetic artifact bytes bound to the API metadata"
        expected_digest = "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
        metadata = {
            "id": 4815162342,
            "name": "platform-production-external-load-manifest-123456789-3",
            "expired": False,
            "digest": expected_digest,
            "workflow_run": {"id": 123456789, "head_sha": "a" * 40},
        }

        with tempfile.TemporaryDirectory() as temporary_dir:
            metadata_path = Path(temporary_dir) / "metadata.json"
            archive_path = Path(temporary_dir) / "artifact.zip"

            for index, script in enumerate(scripts):
                with self.subTest(verifier_index=index):
                    def run(payload: dict[str, object], archive: bytes = archive_bytes) -> subprocess.CompletedProcess[str]:
                        metadata_path.write_text(json.dumps(payload), encoding="utf-8")
                        archive_path.write_bytes(archive)
                        return subprocess.run(
                            [
                                sys.executable,
                                "-c",
                                script,
                                str(metadata_path),
                                str(archive_path),
                                "4815162342",
                                "platform-production-external-load-manifest-123456789-3",
                                "123456789",
                                "a" * 40,
                            ],
                            capture_output=True,
                            text=True,
                            check=False,
                        )

                    self.assertEqual(run(metadata).returncode, 0)
                    mutations = (
                        {"id": 4815162343},
                        {"name": "platform-production-external-load-manifest-123456789-2"},
                        {"expired": True},
                        {"digest": "sha256:" + "A" * 64},
                        {"digest": None},
                        {"workflow_run": {"id": 123456788, "head_sha": "a" * 40}},
                        {"workflow_run": {"id": 123456789, "head_sha": "b" * 40}},
                    )
                    for mutation in mutations:
                        altered = copy.deepcopy(metadata)
                        altered.update(mutation)
                        self.assertNotEqual(run(altered).returncode, 0)
                    self.assertNotEqual(run(metadata, archive_bytes + b"tampered").returncode, 0)

    def test_evaluator_uses_declared_source_sha_for_artifact_and_candidate_binding(self) -> None:
        workflow = yaml.safe_load(self.source)
        evaluator_job = workflow["jobs"]["evaluate-load"]
        job_env = evaluator_job["env"]
        self.assertEqual(job_env["SOURCE_GIT_SHA"], "${{ github.sha }}")
        self.assertNotIn("TARGET_SHA", job_env)
        verifier_sha_envs = {
            "fixture-setup": "TARGET_SHA",
            "load-client": "TARGET_SHA",
            "fixture-finalize": "TARGET_SHA",
            "evaluate-load": "SOURCE_GIT_SHA",
        }
        for job_name, sha_env in verifier_sha_envs.items():
            with self.subTest(job=job_name):
                job = workflow["jobs"][job_name]
                self.assertEqual(job["env"][sha_env], "${{ github.sha }}")
                calls = [
                    match.group(1)
                    for step in job["steps"]
                    for match in re.finditer(
                        r'/usr/bin/python3 - "\$metadata" "\$archive" .*? "\$GITHUB_RUN_ID" "\$(\w+)" <<\'PY\'',
                        step.get("run", ""),
                    )
                ]
                self.assertTrue(calls, f"{job_name} has no metadata verifier")
                self.assertEqual(set(calls), {sha_env})

        target_sha = "a" * 40
        run_id = "123456789"
        attempt = "3"
        candidate_id = "4815162342"
        origin_id = "4815162343"
        archive_bytes = b"evaluator artifact bytes bound to authenticated metadata"
        archive_digest = "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
        artifacts = {
            candidate_id: {
                "id": int(candidate_id),
                "name": f"platform-production-external-load-client-{run_id}-{attempt}",
                "size_in_bytes": len(archive_bytes),
                "expired": False,
                "digest": archive_digest,
                "workflow_run": {"id": int(run_id), "head_sha": target_sha},
            },
            origin_id: {
                "id": int(origin_id),
                "name": f"platform-production-external-load-origin-{run_id}-{attempt}",
                "size_in_bytes": len(archive_bytes),
                "expired": False,
                "digest": archive_digest,
                "workflow_run": {"id": int(run_id), "head_sha": target_sha},
            },
        }
        verifier_step = next(
            step
            for step in evaluator_job["steps"]
            if step.get("name") == "Verify evaluator artifact identity and digest"
        )
        with tempfile.TemporaryDirectory(prefix="external-load-evaluator-sha-") as temporary:
            root = Path(temporary)
            metadata_root = root / "metadata"
            metadata_root.mkdir()
            archive_root = root / "archives"
            archive_root.mkdir()
            for artifact_id, payload in artifacts.items():
                (metadata_root / f"{artifact_id}.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                (archive_root / f"{artifact_id}.zip").write_bytes(archive_bytes)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            curl_stub = bin_dir / "curl"
            curl_stub.write_text(
                "#!/usr/bin/python3\n"
                "import os, shutil, sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "output = Path(args[args.index('--output') + 1])\n"
                "url = next(arg for arg in args if arg.startswith('https://'))\n"
                "artifact_id = url.rsplit('/', 1)[-1] if not url.endswith('/zip') else url.rsplit('/', 2)[-2]\n"
                "root = Path(os.environ['TEST_ARTIFACT_ROOT'])\n"
                "source = root / ('archives' if url.endswith('/zip') else 'metadata') / (artifact_id + ('.zip' if url.endswith('/zip') else '.json'))\n"
                "shutil.copyfile(source, output)\n",
                encoding="ascii",
            )
            curl_stub.chmod(0o755)
            github_output = root / "github-output"
            github_output.touch()

            def evaluator_env(source_sha: str = target_sha) -> dict[str, str]:
                resolved = {
                    "SOURCE_GIT_SHA": source_sha,
                    "PROFILE_ID": "ready-vote-slo-v2",
                    "TIMEOUT_DIAGNOSTICS": "false",
                    "INPUT_ARTIFACT_ID": "4815162341",
                    "CANDIDATE_ARTIFACT_ID": candidate_id,
                    "ORIGIN_ARTIFACT_ID": origin_id,
                }
                self.assertNotIn("TARGET_SHA", resolved)
                return {
                    "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                    "HOME": str(root),
                    "RUNNER_TEMP": str(root),
                    "GITHUB_OUTPUT": str(github_output),
                    "GITHUB_API_URL": "https://api.github.com",
                    "GITHUB_REPOSITORY": "StrayForest/old_sparky",
                    "GITHUB_RUN_ID": run_id,
                    "GITHUB_RUN_ATTEMPT": attempt,
                    "GH_TOKEN": "synthetic-read-token",
                    "TEST_ARTIFACT_ROOT": str(root),
                    **resolved,
                }

            result = subprocess.run(
                ["/bin/bash", "-e", "-o", "pipefail", "-c", verifier_step["run"]],
                env=evaluator_env(),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "candidate_artifact_status=0",
                github_output.read_text(),
                result.stdout + result.stderr,
            )
            self.assertIn(
                "origin_artifact_status=0",
                github_output.read_text(),
                result.stdout + result.stderr,
            )

            github_output.write_text("", encoding="ascii")
            result = subprocess.run(
                ["/bin/bash", "-e", "-o", "pipefail", "-c", verifier_step["run"]],
                env=evaluator_env("b" * 40),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("candidate_artifact_status=1", github_output.read_text())
            self.assertIn("origin_artifact_status=1", github_output.read_text())

            evaluate_step = next(
                step
                for step in evaluator_job["steps"]
                if step.get("name") == "Evaluate checked-out load report"
            )
            status_match = re.search(
                r'if /usr/bin/python3 - "\$load_status_file" "\$report" "\$(?P<sha_env>[A-Z_]+)" '
                r'"\$GITHUB_RUN_ID" "\$GITHUB_RUN_ATTEMPT" "\$PROFILE_ID" "\$TIMEOUT_DIAGNOSTICS" <<\'PY\'\n'
                r"(?P<script>.*?)\nPY",
                evaluate_step["run"],
                re.DOTALL,
            )
            self.assertIsNotNone(status_match)
            assert status_match is not None
            sha_env = status_match.group("sha_env")
            env = evaluator_env()
            self.assertIn(sha_env, env)
            self.assertEqual(sha_env, "SOURCE_GIT_SHA")
            report_path = root / "candidate-report.json"
            report_bytes = b"{}\n"
            report_path.write_bytes(report_bytes)
            status_path = root / "load-status.json"
            receipt = {
                "schema": 2,
                "status": 0,
                "client_exit_status": 3,
                "candidate_state": "pending_origin",
                "report_ready": True,
                "target_sha": target_sha,
                "run_id": run_id,
                "run_attempt": attempt,
                "profile_id": "ready-vote-slo-v2",
                "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            }
            status_path.write_text(json.dumps(receipt), encoding="utf-8")

            def validate_status(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
                status_path.write_text(json.dumps(payload), encoding="utf-8")
                return subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        textwrap.dedent(status_match.group("script")),
                        str(status_path),
                        str(report_path),
                        env[sha_env],
                        run_id,
                        attempt,
                        "ready-vote-slo-v2",
                        "false",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )

            self.assertEqual(validate_status(receipt).returncode, 0)
            wrong_sha_receipt = dict(receipt)
            wrong_sha_receipt["target_sha"] = "b" * 40
            self.assertNotEqual(validate_status(wrong_sha_receipt).returncode, 0)

    def test_control_identity_is_masked_before_consuming_steps_and_never_becomes_env_or_argv(self) -> None:
        self.assertNotIn("${{ inputs.control_email }}", self.source)
        self.assertNotIn("CONTROL_EMAIL:", self.source)
        self.assertNotIn("$CONTROL_EMAIL", self.source)
        self.assertNotIn("--control-email", self.source)
        validator = self.jobs["validate-external-inputs"]
        self.assertIn("validate_external_payload(payload)", validator)
        self.assertIn("_write_private_json(Path(output_path)", validator)
        self.assertIn('control_email_path.read_text(encoding="ascii")', validator)

        mask_steps = {
            "validate-external-inputs": "Mask and stage the control identity from the event file",
            "fixture-setup": "Mask the control identity before reading the handoff",
            "load-client": "Mask the control identity before reading the handoff",
            "fixture-finalize": "Mask and stage the control identity from the event file",
        }
        for job_name, step_name in mask_steps.items():
            with self.subTest(job=job_name):
                job = self.jobs[job_name]
                step_names = re.findall(r"^      - name: (.+)$", job, re.MULTILINE)
                self.assertTrue(step_names)
                self.assertIn(step_name, step_names)
                prior_steps = job.split(f"- name: {step_name}", 1)[0]
                self.assertNotIn("GITHUB_EVENT_PATH", prior_steps)
                self.assertNotIn("control_email_path", prior_steps)
                self.assertNotIn("CONTROL_EMAIL", prior_steps)
                script = _step_script(job, step_name)
                self.assertLess(script.index("os.open("), script.index("::add-mask::"))
                if "os.open(destination" in script:
                    self.assertLess(
                        script.index("sys.stdout.flush()"),
                        script.index("os.open(destination"),
                    )

        valid_inputs = {
            "confirmation": "RUN-PRODUCTION-EXTERNAL-LOAD",
            "control_email": "Control%qa@example.invalid",
            "profile_id": "ready-vote-slo-v2",
            "timeout_diagnostics": False,
        }
        invalid_payloads = (
            json.dumps({"inputs": {**valid_inputs, "unexpected": "value"}}),
            (
                '{"inputs":{"confirmation":"RUN-PRODUCTION-EXTERNAL-LOAD",'
                '"control_email":"control@example.invalid",'
                '"control_email":"other@example.invalid",'
                '"profile_id":"ready-vote-slo-v2","timeout_diagnostics":false}}'
            ),
        )
        for job_name, step_name in mask_steps.items():
            script = _step_script(self.jobs[job_name], step_name)
            with self.subTest(job=job_name, event="closed-valid"):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    event_path = root / "event.json"
                    event_path.write_text(
                        json.dumps({"inputs": valid_inputs}), encoding="utf-8"
                    )
                    result = subprocess.run(
                        ["/bin/bash", "-euo", "pipefail", "-c", script],
                        env={
                            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "GITHUB_EVENT_PATH": str(event_path),
                            "RUNNER_TEMP": str(root),
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertIn("::add-mask::", result.stdout)
                    self.assertNotIn("::warning::", result.stdout)

            for invalid_payload in invalid_payloads:
                with self.subTest(job=job_name, event="invalid-closed-inputs"):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        event_path = root / "event.json"
                        event_path.write_text(invalid_payload, encoding="utf-8")
                        result = subprocess.run(
                            ["/bin/bash", "-euo", "pipefail", "-c", script],
                            env={
                                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                "GITHUB_EVENT_PATH": str(event_path),
                                "RUNNER_TEMP": str(root),
                            },
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(result.stdout, "")
                        self.assertNotIn("control@example.invalid", result.stderr)

        validator_script = _step_script(
            self.jobs["validate-external-inputs"], mask_steps["validate-external-inputs"]
        )
        malformed_files = ("group-world-writable", "oversized", "symlink", "wrong-owner")
        for case in malformed_files:
            if case == "wrong-owner" and os.geteuid() != 0:
                continue
            with self.subTest(event_file=case):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    event_path = root / "event.json"
                    event_bytes = json.dumps({"inputs": valid_inputs}).encode("utf-8")
                    if case == "symlink":
                        target = root / "event-target.json"
                        target.write_bytes(event_bytes)
                        event_path.symlink_to(target)
                    elif case == "oversized":
                        event_path.write_bytes(event_bytes + b" " * 65537)
                    else:
                        event_path.write_bytes(event_bytes)
                        if case == "group-world-writable":
                            event_path.chmod(0o666)
                        elif case == "wrong-owner":
                            os.chown(event_path, os.geteuid() + 1, -1)
                    result = subprocess.run(
                        ["/bin/bash", "-euo", "pipefail", "-c", validator_script],
                        env={
                            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "GITHUB_EVENT_PATH": str(event_path),
                            "RUNNER_TEMP": str(root),
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn("control@example.invalid", result.stderr)

        # The two jobs that need a private local cleanup value stage it only
        # after the runner has received the masking command.
        for job_name, step_name in (
            ("validate-external-inputs", mask_steps["validate-external-inputs"]),
            ("fixture-finalize", mask_steps["fixture-finalize"]),
        ):
            script = _step_script(self.jobs[job_name], step_name)
            self.assertLess(
                script.index("::add-mask::"), script.index("os.open(destination")
            )

        finalizer = self.jobs["fixture-finalize"]
        self.assertIn('"$cleanup_input_path" "$TARGET_SHA" "$GITHUB_RUN_ID"', finalizer)
        self.assertIn('control_email_path.read_text(encoding="ascii")', finalizer)
        self.assertNotRegex(finalizer, r'python3\s+-[^\n]*\$\{?CONTROL_EMAIL')
        self.assertIn('"$RUNNER_TEMP/platform-production-control-email"', self.jobs["validate-external-inputs"])
        self.assertIn('"$RUNNER_TEMP/platform-production-control-email"', finalizer)
        for job_name, step_name in (
            ("validate-external-inputs", "Remove external-load validator handoff"),
            ("fixture-finalize", "Remove finalizer SSH material"),
        ):
            with self.subTest(cleanup_job=job_name):
                cleanup = self.jobs[job_name].split(f"- name: {step_name}", 1)[1]
                self.assertIn("if: ${{ always() }}", cleanup)
                self.assertIn('"$RUNNER_TEMP/platform-production-control-email"', cleanup)
                self.assertIn("test ! -e", cleanup)

    def test_retained_cleanup_identity_uses_event_file_and_private_stdin_handoff(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("CONTROL_EMAIL:", workflow)
        self.assertNotIn("${{ inputs.control_email }}", workflow)
        self.assertNotIn("$CONTROL_EMAIL", workflow)
        self.assertIn('"$GITHUB_EVENT_PATH"', workflow)
        self.assertIn("os.O_NOFOLLOW", workflow)
        self.assertIn('os.fstat(descriptor)', workflow)
        self.assertIn('"control_email": canonical_email', workflow)
        self.assertIn('"$input_path"', workflow)
        for mode in ("retained-cleanup", "retained-cleanup-exports"):
            self.assertIn(f"{mode} < \"$input_path\"", workflow)
        cleanup_step = _jobs(workflow)["cleanup"]
        parsed_workflow = yaml.safe_load(workflow)
        cleanup_job = parsed_workflow["jobs"]["cleanup"]
        self.assertIn("resolve-host-tools-pin", cleanup_job["needs"])
        self.assertEqual(
            cleanup_job["env"]["HOST_TOOLS_SHA"],
            "${{ needs.resolve-host-tools-pin.outputs.host_tools_sha }}",
        )
        cleanup_dispatch = next(
            step
            for step in cleanup_job["steps"]
            if step.get("name") == "Clean the exact retained load run on production"
        )
        self.assertNotIn("HOST_TOOLS_SHA", cleanup_dispatch.get("env", {}))
        self.assertNotIn("steps.resolve_retained_load_host_tools", workflow)
        external_workflow = yaml.safe_load(self.source)
        finalize_job = external_workflow["jobs"]["fixture-finalize"]
        self.assertIn("resolve-host-tools-pin", finalize_job["needs"])
        self.assertEqual(
            finalize_job["env"]["HOST_TOOLS_SHA"],
            "${{ needs.resolve-host-tools-pin.outputs.host_tools_sha }}",
        )
        for step_name in (
            "Signal fixture completion and collect origin evidence",
            "Exact cleanup of external fixture",
        ):
            step = next(
                step for step in finalize_job["steps"] if step.get("name") == step_name
            )
            with self.subTest(step=step_name):
                self.assertNotIn("HOST_TOOLS_SHA", step.get("env", {}))
        self.assertNotIn("steps.resolve_retained_load_host_tools", self.source)
        self.assertNotRegex(
            cleanup_step,
            r"python3\s+-[^\n]*\$\{?inputs\.control_email|"
            r"python3\s+-[^\n]*\$CONTROL_EMAIL",
        )

        valid_event = {"inputs": {"control_email": "Control%qa@example.invalid"}}
        invalid_event = {
            "inputs": {"control_email": "private@example.invalid\r\n::warning::injected"}
        }
        valid_event["inputs"].update(
            {
                "confirmation": "DELETE-PRODUCTION-RETAINED-LOAD",
                "load_run_id": "123456",
            }
        )
        invalid_event["inputs"].update(
            {
                "confirmation": "DELETE-PRODUCTION-RETAINED-LOAD",
                "load_run_id": "123456",
            }
        )
        cleanup_validation = _step_script(
            cleanup_step, "Validate explicit cleanup confirmation"
        )
        self.assertIn(
            'test "$QA_CONFIRMATION" = "DELETE-PRODUCTION-RETAINED-LOAD"',
            cleanup_validation,
        )
        duplicate_event = (
            '{"inputs":{"confirmation":"DELETE-PRODUCTION-RETAINED-LOAD",'
            '"control_email":"first@example.invalid",'
            '"control_email":"second@example.invalid","load_run_id":"123456"}}'
        )
        unknown_event = {
            "inputs": {
                **valid_event["inputs"],
                "unexpected": "value",
            }
        }
        event_cases = (
            ("valid", valid_event, True),
            ("invalid", invalid_event, True),
            ("duplicate", duplicate_event, False),
            ("unknown", unknown_event, True),
        )
        for event_name, event, serialize_event in event_cases:
            with self.subTest(event=event_name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    event_path = root / "event.json"
                    output_path = root / "platform-retained-cleanup-input.json"
                    event_contents = json.dumps(event) if serialize_event else event
                    event_path.write_text(event_contents, encoding="utf-8")
                    result = subprocess.run(
                        ["/bin/bash", "-euo", "pipefail", "-c", cleanup_validation],
                        env={
                            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "GITHUB_REF": "refs/heads/dev",
                            "GITHUB_EVENT_PATH": str(event_path),
                            "GITHUB_RUN_ID": "234567",
                            "QA_CONFIRMATION": "DELETE-PRODUCTION-RETAINED-LOAD",
                            "LOAD_RUN_ID": "123456",
                            "TARGET_SHA": "a" * 40,
                            "RUNNER_TEMP": str(root),
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotIn("private@example.invalid", result.stdout)
                    self.assertNotIn("private@example.invalid", result.stderr)
                    if event_name == "valid":
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(
                            result.stdout.splitlines(),
                            [
                                "::add-mask::Control%25qa@example.invalid",
                                "::add-mask::control%25qa@example.invalid",
                            ],
                        )
                        self.assertEqual(result.stderr, "")
                        self.assertNotIn("Control%qa@example.invalid", result.stdout)
                        self.assertEqual(output_path.stat().st_mode & 0o777, 0o600)
                        cleanup_payload = json.loads(
                            output_path.read_text(encoding="ascii")
                        )
                        self.assertEqual(
                            cleanup_payload,
                            {
                                "schema": 1,
                                "target_sha": "a" * 40,
                                "control_email": "control%qa@example.invalid",
                                "load_run_id": "123456",
                                "cleanup_run_id": "234567",
                            },
                        )
                        self.assertEqual(
                            set(cleanup_payload),
                            {
                                "schema",
                                "target_sha",
                                "control_email",
                                "load_run_id",
                                "cleanup_run_id",
                            },
                        )
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse(output_path.exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / "event.json"
            output_path = root / "platform-retained-cleanup-input.json"
            event_path.write_text(json.dumps(valid_event), encoding="utf-8")
            result = subprocess.run(
                ["/bin/bash", "-euo", "pipefail", "-c", cleanup_validation],
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "GITHUB_REF": "refs/heads/dev",
                    "GITHUB_EVENT_PATH": str(event_path),
                    "GITHUB_RUN_ID": "234567",
                    "QA_CONFIRMATION": "WRONG-CONFIRMATION",
                    "LOAD_RUN_ID": "123456",
                    "TARGET_SHA": "a" * 40,
                    "RUNNER_TEMP": str(root),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output_path.exists())

        for event_file_case in (
            "group-world-writable",
            "oversized",
            "symlink",
            "wrong-owner",
        ):
            if event_file_case == "wrong-owner" and os.geteuid() != 0:
                continue
            with self.subTest(event_file=event_file_case):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    event_path = root / "event.json"
                    valid_bytes = json.dumps(valid_event).encode("utf-8")
                    if event_file_case == "symlink":
                        event_target = root / "event-target.json"
                        event_target.write_bytes(valid_bytes)
                        event_path.symlink_to(event_target)
                    else:
                        extra = b" " * 65537 if event_file_case == "oversized" else b""
                        event_path.write_bytes(valid_bytes + extra)
                        if event_file_case == "group-world-writable":
                            event_path.chmod(0o666)
                        elif event_file_case == "wrong-owner":
                            os.chown(event_path, os.geteuid() + 1, -1)
                    result = subprocess.run(
                        ["/bin/bash", "-euo", "pipefail", "-c", cleanup_validation],
                        env={
                            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "GITHUB_REF": "refs/heads/dev",
                            "GITHUB_EVENT_PATH": str(event_path),
                            "GITHUB_RUN_ID": "234567",
                            "QA_CONFIRMATION": "DELETE-PRODUCTION-RETAINED-LOAD",
                            "LOAD_RUN_ID": "123456",
                            "TARGET_SHA": "a" * 40,
                            "RUNNER_TEMP": str(root),
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertFalse(
                        (root / "platform-retained-cleanup-input.json").exists()
                    )

    def test_load_runner_is_pinned_and_has_containment_probe_margin(self) -> None:
        self.assertNotIn("runs-on: ubuntu-latest", self.source)
        for job_id in (
            "validate-external-inputs",
            "fixture-setup",
            "load-client",
            "fixture-finalize",
            "evaluate-load",
        ):
            with self.subTest(job=job_id):
                self.assertIn("runs-on: ubuntu-24.04", self.jobs[job_id])
        load_client = self.jobs["load-client"]
        self.assertIn("timeout-minutes: 270", load_client)
        self.assertIn("Probe mandatory load PID-namespace containment", load_client)
        self.assertIn("probe_pid_namespace_capability", load_client)
        validator = self.jobs["validate-external-inputs"]
        self.assertIn("before fixture setup", validator)
        self.assertIn("probe_pid_namespace_capability", validator)

    def test_containment_canary_is_safe_and_pinned(self) -> None:
        self.assertIn("runs-on: ubuntu-24.04", self.canary_source)
        self.assertIn("probe_pid_namespace_capability", self.canary_source)
        self.assertIn("run_supervised", self.canary_source)
        self.assertIn("namespace_closed", self.canary_source)
        self.assertIn("os.setsid", self.canary_source)
        self.assertIn("os.fork()", self.canary_source)
        self.assertIn("nested-descendant", self.canary_source)
        self.assertIn("double-fork-grandchild", self.canary_source)
        self.assertIn("starttime", self.canary_source)
        self.assertIn("heartbeat", self.canary_source)
        self.assertIn("descendants_reaped", self.canary_source)
        self.assertIn("runtime_supervisor", self.canary_source)
        self.assertIn("max_duration_seconds=8.0", self.canary_source)
        self.assertNotIn("secrets.", self.canary_source)
        self.assertNotIn("platform_load.py run", self.canary_source)
        self.assertNotIn("manifest", self.canary_source)
        self.assertNotIn("fixture", self.canary_source)

    def test_evaluator_cannot_hide_upstream_failure_or_publish_success(self) -> None:
        evaluator = self.jobs["evaluate-load"]
        self.assertIn("upstream_ready=0", evaluator)
        self.assertIn('needs.load-client.outputs.report_ready }}\" == 1', evaluator)
        self.assertIn('payload.get("status") != 0', evaluator)
        self.assertIn('payload.get("schema") != 2', evaluator)
        self.assertIn('state == "pending_origin" and raw_status == 3 and timeout_diagnostics == "false"', evaluator)
        self.assertIn('state == "produced" and raw_status == 0', evaluator)
        self.assertIn('TIMEOUT_DIAGNOSTICS" != true && "$load_status" == 3', self.jobs["load-client"])
        self.assertIn('hashlib.sha256(report_file.read_bytes()).hexdigest()', evaluator)
        self.assertIn("--defer-completed-slo-failure", evaluator)
        self.assertIn('status == 3', evaluator)
        self.assertIn('needs.fixture-finalize.outputs.cleanup_exports_status }}\" == 0', evaluator)
        publish = evaluator.split("- name: Publish external load evidence", 1)[1]
        self.assertIn("needs.load-client.result == 'success'", publish)
        self.assertIn("steps.evaluate-load.outputs.evaluation_status == '0'", publish)
        self.assertIn("steps.evaluate-load.outputs.acceptance_status == 'slo_failed'", publish)
        self.assertIn("steps.sanitize.outputs.sanitizer_status == '0'", publish)
        final_gate = evaluator.split(
            "- name: Enforce external load and exact cleanup gates", 1
        )[1]
        self.assertIn('if [[ "$acceptance_status" == slo_failed ]]', final_gate)
        self.assertIn("sanitized evidence was published", final_gate)

        completed_slo_miss = {
            "validate_result": "success",
            "setup_result": "success",
            "setup_status": "0",
            "setup_ssh_cleanup_status": "0",
            "load_result": "success",
            "load_status": "0",
            "candidate_state": "pending_origin",
            "report_ready": "1",
            "namespace_barrier_result": "success",
            "namespace_closed_status": "0",
            "manual_barrier_result": "success",
            "manual_required": "0",
            "finalize_result": "success",
            "remote_status": "0",
            "observer_ready": "1",
            "finalize_status": "0",
            "cleanup_status": "0",
            "cleanup_exports_status": "0",
            "ssh_cleanup_status": "0",
            "cleanup_identity_status": "0",
            "handoff_status": "0",
            "candidate_artifact_status": "0",
            "origin_artifact_status": "0",
            "evaluation_status": "0",
            "acceptance_status": "slo_failed",
            "sanitizer_status": "0",
        }
        # A completed budget miss is publishable evidence, but the final
        # enforcement truth table remains red after publication.
        self.assertFalse(_pass_truth_table(completed_slo_miss))
        self.assertTrue(_evidence_publish_truth_table(completed_slo_miss))
        self.assertEqual(completed_slo_miss["acceptance_status"], "slo_failed")
        failed_cleanup = dict(completed_slo_miss)
        failed_cleanup["cleanup_status"] = "1"
        self.assertFalse(_evidence_publish_truth_table(failed_cleanup))

        evaluate_script = _step_script(
            self.jobs["evaluate-load"], "Evaluate checked-out load report"
        )
        receipt_verifier = re.search(
            r"<<'PY'\n(.*?)\nPY", evaluate_script, re.DOTALL
        )
        self.assertIsNotNone(receipt_verifier)
        assert receipt_verifier is not None
        with tempfile.TemporaryDirectory(prefix="load-receipt-contract-") as temp:
            root = Path(temp)
            report = root / "report.json"
            receipt = root / "load-status.json"
            report_bytes = b'{"schema":2,"candidate":true}\n'
            report.write_bytes(report_bytes)
            payload = {
                "schema": 2,
                "status": 0,
                "client_exit_status": 3,
                "candidate_state": "pending_origin",
                "report_ready": True,
                "target_sha": "a" * 40,
                "run_id": "12345",
                "run_attempt": "1",
                "profile_id": "authenticated-page-load-v1",
                "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            }

            def verify(*, timeout: str = "false", text: str | None = None) -> subprocess.CompletedProcess[str]:
                receipt.write_text(
                    text if text is not None else json.dumps(payload),
                    encoding="utf-8",
                )
                return subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        receipt_verifier.group(1),
                        str(receipt),
                        str(report),
                        payload["target_sha"],
                        payload["run_id"],
                        payload["run_attempt"],
                        payload["profile_id"],
                        timeout,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )

            self.assertEqual(verify().returncode, 0)
            self.assertNotEqual(verify(timeout="true").returncode, 0)
            self.assertNotEqual(
                verify(text=json.dumps({**payload, "client_exit_status": 1})).returncode,
                0,
            )
            self.assertNotEqual(
                verify(text=json.dumps(payload)[:-1] + ',"schema":2}').returncode,
                0,
            )
            report.write_bytes(report_bytes + b"tampered")
            self.assertNotEqual(verify().returncode, 0)

    def test_cleanup_exports_and_projection_are_failure_bearing(self) -> None:
        finalizer = self.jobs["fixture-finalize"]
        self.assertIn("external-cleanup-exports", finalizer)
        self.assertNotIn("actions/checkout@", finalizer)
        self.assertIn(
            "HOST_TOOLS_SHA: ${{ needs.resolve-host-tools-pin.outputs.host_tools_sha }}",
            finalizer,
        )
        self.assertIn('"$retained_load_dispatcher" external-finalize', finalizer)
        self.assertIn('"$retained_load_dispatcher" \\\n              external-cleanup-exports', finalizer)
        # Fixture setup and DB cleanup retain their current-release helpers;
        # only completion-marker creation and exact export deletion use C6.
        fixture_setup = self.jobs["fixture-setup"]
        self.assertIn(
            "/opt/oldsparky/platform/current/tools/platform_workflow_remote_dispatch.py external-fixture",
            fixture_setup,
        )
        self.assertIn("/opt/oldsparky/platform/current/tools/platform_workflow_remote_dispatch.py \\\n              external-cleanup <", finalizer)
        self.assertIn("cleanup_exports_status=", finalizer)
        self.assertIn("cleanup_status\" != 0 || \"$cleanup_exports_status\" != 0", finalizer)
        self.assertIn("cleanup summary projection input is invalid", finalizer)
        fixture_cleanup = _step_script(
            fixture_setup, "Remove fixture-setup SSH material"
        )
        with tempfile.TemporaryDirectory(prefix="external-load-ssh-cleanup-") as temp:
            runner_temp = Path(temp)
            ssh_dir = runner_temp / "production-external-load-ssh-setup"
            ssh_dir.mkdir()
            (ssh_dir / "id_ed25519").write_text("test key material", encoding="ascii")
            (ssh_dir / "config").write_text("test config", encoding="ascii")
            output_path = runner_temp / "github-output"
            output_path.touch()
            env_file = runner_temp / "github-env"
            env_file.touch()
            fixture_env = {
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_RUN_ID": f"codex-test-{os.getpid()}",
                "GITHUB_OUTPUT": str(output_path),
                "GITHUB_ENV": str(env_file),
            }
            completed = subprocess.run(
                ["/bin/bash", "-c", fixture_cleanup],
                capture_output=True,
                text=True,
                env=fixture_env,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(output_path.read_text(encoding="ascii"), "ssh_cleanup_status=0\n")
            self.assertFalse(ssh_dir.exists())

        with tempfile.TemporaryDirectory(prefix="external-load-ssh-cleanup-fail-") as temp:
            runner_temp = Path(temp)
            target = runner_temp / "unexpected-target"
            target.mkdir()
            ssh_dir = runner_temp / "production-external-load-ssh-setup"
            ssh_dir.symlink_to(target, target_is_directory=True)
            output_path = runner_temp / "github-output"
            output_path.touch()
            env_file = runner_temp / "github-env"
            env_file.touch()
            completed = subprocess.run(
                ["/bin/bash", "-c", fixture_cleanup],
                capture_output=True,
                text=True,
                env={
                    "RUNNER_TEMP": str(runner_temp),
                    "GITHUB_RUN_ID": f"codex-test-fail-{os.getpid()}",
                    "GITHUB_OUTPUT": str(output_path),
                    "GITHUB_ENV": str(env_file),
                },
                timeout=10,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(output_path.read_text(encoding="ascii"), "")

        finalizer_cleanup = _step_script(finalizer, "Remove finalizer SSH material")
        self.assertLess(
            finalizer_cleanup.index('test -z "${SSH_DIR:-}"'),
            finalizer_cleanup.index("ssh_cleanup_status=0"),
        )
        with tempfile.TemporaryDirectory(prefix="external-load-finalizer-ssh-cleanup-") as temp:
            runner_temp = Path(temp)
            ssh_dir = runner_temp / "production-external-load-ssh-finalize"
            ssh_dir.mkdir()
            (ssh_dir / "id_ed25519").write_text("test key material", encoding="ascii")
            (ssh_dir / "config").write_text("test config", encoding="ascii")
            output_path = runner_temp / "github-output"
            output_path.touch()
            env_file = runner_temp / "github-env"
            env_file.touch()
            finalizer_env = {
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_RUN_ID": f"codex-finalizer-test-{os.getpid()}",
                "GITHUB_OUTPUT": str(output_path),
                "GITHUB_ENV": str(env_file),
            }
            completed = subprocess.run(
                ["/bin/bash", "-c", finalizer_cleanup],
                capture_output=True,
                text=True,
                env=finalizer_env,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(ssh_dir.exists())
            self.assertEqual(output_path.read_text(encoding="ascii"), "ssh_cleanup_status=0\n")

        with tempfile.TemporaryDirectory(prefix="external-load-finalizer-ssh-cleanup-fail-") as temp:
            runner_temp = Path(temp)
            target = runner_temp / "unexpected-target"
            target.mkdir()
            ssh_dir = runner_temp / "production-external-load-ssh-finalize"
            ssh_dir.symlink_to(target, target_is_directory=True)
            output_path = runner_temp / "github-output"
            output_path.touch()
            env_file = runner_temp / "github-env"
            env_file.touch()
            completed = subprocess.run(
                ["/bin/bash", "-c", finalizer_cleanup],
                capture_output=True,
                text=True,
                env={
                    "RUNNER_TEMP": str(runner_temp),
                    "GITHUB_RUN_ID": f"codex-finalizer-fail-{os.getpid()}",
                    "GITHUB_OUTPUT": str(output_path),
                    "GITHUB_ENV": str(env_file),
                },
                timeout=10,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(output_path.read_text(encoding="ascii"), "")

        pin_job = self.jobs["resolve-host-tools-pin"]
        self.assertIn("Checkout exact target as untrusted source data", pin_job)
        self.assertIn("platform_host_tools_pin.py resolve", pin_job)
        self.assertIn('--target-sha "$TARGET_SHA"', pin_job)
        self.assertNotIn("secrets.PROD_SSH_", pin_job)
        diagnostic = finalizer.split(
            "- name: Diagnose origin evidence publication gate", 1
        )[1].split("- name: Publish origin evidence", 1)[0]
        diagnostic_script = _step_script(
            finalizer, "Diagnose origin evidence publication gate"
        )
        self.assertIn("if: ${{ always() }}", diagnostic)
        self.assertIn("ORIGIN_PUBLISH_GATE", diagnostic_script)
        diagnostic_yaml = yaml.safe_load(self.source)["jobs"]["fixture-finalize"]["steps"]
        diagnostic_upload = next(
            step
            for step in diagnostic_yaml
            if step.get("name") == "Publish closed cleanup failure diagnostic"
        )
        self.assertIn("always()", diagnostic_upload["if"])
        self.assertIn("steps.cleanup_ssh.outcome == 'success'", diagnostic_upload["if"])
        self.assertIn("explicit_conditions_met == 'no'", diagnostic_upload["if"])
        self.assertIn("cleanup-diagnostic.json", diagnostic_upload["with"]["path"])
        self.assertIn("retention-days: 1", self.source)
        self.assertNotIn("${{", diagnostic_script)
        self.assertNotIn("secrets.", diagnostic)
        workflow = yaml.safe_load(self.source)
        diagnostic_step = next(
            step
            for step in workflow["jobs"]["fixture-finalize"]["steps"]
            if step.get("name") == "Diagnose origin evidence publication gate"
        )
        diagnostic_env = diagnostic_step["env"]
        self.assertNotIn("ORIGIN_PRIOR_STEPS_SUCCESS", diagnostic_env)
        for value in diagnostic_env.values():
            with self.subTest(env_expression=value):
                self.assertNotRegex(
                    value,
                    r"\b(?:success|failure|cancelled|always)\s*\(",
                    "GitHub status functions are allowed in steps.if, not steps.env",
                )
        for category in (
            '"missing"',
            '"invalid"',
            '"cancelled"',
            '"skipped"',
            "explicit_conditions_met=",
        ):
            with self.subTest(category=category):
                self.assertIn(category, diagnostic_script)
        allowed_environment = {
            "ORIGIN_NAMESPACE_CLOSED": "0",
            "ORIGIN_MANUAL_REQUIRED": "0",
            "ORIGIN_CLEANUP_SSH_OUTCOME": "success",
            "ORIGIN_REMOTE_STATUS": "0",
            "ORIGIN_OBSERVER_READY": "1",
            "ORIGIN_FINALIZE_STATUS": "0",
            "ORIGIN_CLEANUP_STATUS": "0",
            "ORIGIN_CLEANUP_EXPORTS_STATUS": "0",
            "ORIGIN_SSH_CLEANUP_STATUS": "0",
            "ORIGIN_CLEANUP_IDENTITY_STATUS": "0",
            "ORIGIN_CLEANUP_REMOTE_SSH_EXIT": "0",
            "ORIGIN_CLEANUP_REMOTE_STAGE": "complete",
            "ORIGIN_CLEANUP_REMOTE_CHILD_EXIT": "0",
        }
        with tempfile.TemporaryDirectory(prefix="external-load-origin-diagnostic-") as temp:
            evidence_root = Path(temp) / "external-evidence"
            evidence_root.mkdir(mode=0o700)
            output_path = Path(temp) / "github-output"
            output_path.touch()
            valid_summary = subprocess.run(
                ["/bin/bash", "-c", diagnostic_script],
                check=True,
                capture_output=True,
                text=True,
                env={
                    **allowed_environment,
                    "RUNNER_TEMP": temp,
                    "GITHUB_OUTPUT": str(output_path),
                },
                timeout=10,
            ).stdout
            self.assertIn("explicit_conditions_met=yes", valid_summary)
            diagnostic_path = evidence_root / "cleanup-diagnostic.json"
            diagnostic = json.loads(diagnostic_path.read_text(encoding="ascii"))
            self.assertEqual(set(diagnostic), {"schema", "conditions", "explicit_conditions_met"})
            self.assertEqual(diagnostic["schema"], 1)
            self.assertEqual(diagnostic["explicit_conditions_met"], "yes")
            self.assertEqual(diagnostic["conditions"]["cleanup_stage"], "complete")
            self.assertEqual(output_path.read_text(encoding="ascii"), "explicit_conditions_met=yes\n")
            self.assertEqual(diagnostic_path.stat().st_mode & 0o777, 0o600)

        invalid_environment = dict(allowed_environment)
        invalid_environment["ORIGIN_REMOTE_STATUS"] = "0\nraw-path-or-secret"
        invalid_environment["ORIGIN_CLEANUP_SSH_OUTCOME"] = "skipped"
        invalid_environment.pop("ORIGIN_FINALIZE_STATUS")
        invalid_summary = subprocess.run(
            ["/bin/bash", "-c", diagnostic_script],
            check=True,
            capture_output=True,
            text=True,
            env=invalid_environment,
            timeout=10,
        ).stdout
        self.assertIn("remote=invalid", invalid_summary)
        self.assertIn("ssh_step=skipped", invalid_summary)
        self.assertIn("finalize=missing", invalid_summary)
        self.assertIn("explicit_conditions_met=no", invalid_summary)
        self.assertNotIn("raw-path-or-secret", invalid_summary)
        with tempfile.TemporaryDirectory(prefix="external-load-origin-failure-") as temp:
            evidence_root = Path(temp) / "external-evidence"
            evidence_root.mkdir(mode=0o700)
            output_path = Path(temp) / "github-output"
            output_path.touch()
            failure_environment = {
                **allowed_environment,
                "ORIGIN_REMOTE_STATUS": "0\nsecret-path-marker",
                "ORIGIN_CLEANUP_REMOTE_STAGE": "untrusted-stage",
                "ORIGIN_CLEANUP_REMOTE_CHILD_EXIT": "7",
                "RUNNER_TEMP": temp,
                "GITHUB_OUTPUT": str(output_path),
            }
            subprocess.run(
                ["/bin/bash", "-c", diagnostic_script],
                check=True,
                capture_output=True,
                text=True,
                env=failure_environment,
                timeout=10,
            )
            failure_path = evidence_root / "cleanup-diagnostic.json"
            failure_diagnostic = json.loads(failure_path.read_text(encoding="ascii"))
            self.assertEqual(failure_diagnostic["explicit_conditions_met"], "no")
            self.assertEqual(failure_diagnostic["conditions"]["remote"], "invalid")
            self.assertEqual(failure_diagnostic["conditions"]["cleanup_stage"], "invalid")
            self.assertEqual(failure_diagnostic["conditions"]["cleanup_child_exit"], "7")
            self.assertNotIn("secret-path-marker", failure_path.read_text(encoding="ascii"))
            self.assertEqual(output_path.read_text(encoding="ascii"), "explicit_conditions_met=no\n")
        with tempfile.TemporaryDirectory(prefix="external-load-origin-missing-stage-") as temp:
            evidence_root = Path(temp) / "external-evidence"
            evidence_root.mkdir(mode=0o700)
            output_path = Path(temp) / "github-output"
            output_path.touch()
            missing_environment = dict(allowed_environment)
            missing_environment.pop("ORIGIN_CLEANUP_REMOTE_CHILD_EXIT")
            missing_environment.update(
                {"RUNNER_TEMP": temp, "GITHUB_OUTPUT": str(output_path)}
            )
            subprocess.run(
                ["/bin/bash", "-c", diagnostic_script],
                check=True,
                capture_output=True,
                text=True,
                env=missing_environment,
                timeout=10,
            )
            missing_diagnostic = json.loads(
                (evidence_root / "cleanup-diagnostic.json").read_text(encoding="ascii")
            )
            self.assertEqual(missing_diagnostic["explicit_conditions_met"], "no")
            self.assertEqual(missing_diagnostic["conditions"]["cleanup_child_exit"], "missing")

        publish = finalizer.split("- name: Publish origin evidence", 1)[1].split(
            "\n      - name:", 1
        )[0]
        for gate_input in (
            "always()",
            "needs.namespace-containment-barrier.outputs.namespace_closed_status == '0'",
            "needs.namespace-containment-manual-barrier.outputs.manual_required == '0'",
            "steps.cleanup_ssh.outcome == 'success'",
            "steps.external-finalize.outputs.remote_status == '0'",
            "steps.external-finalize.outputs.observer_ready == '1'",
            "steps.external-finalize.outputs.finalize_status == '0'",
            "steps.cleanup.outputs.cleanup_status == '0'",
            "steps.cleanup.outputs.cleanup_remote_ssh_exit_code == '0'",
            "steps.cleanup.outputs.cleanup_remote_stage == 'complete'",
            "steps.cleanup.outputs.cleanup_remote_child_exit == '0'",
            "steps.cleanup.outputs.cleanup_exports_status == '0'",
            "steps.cleanup_ssh.outputs.ssh_cleanup_status != ''",
            "steps.cleanup_ssh.outputs.ssh_cleanup_status == '0'",
            "steps.revalidate-finalizer.outputs.cleanup_identity_status == '0'",
        ):
            with self.subTest(gate_input=gate_input):
                self.assertIn(gate_input, publish)
        self.assertNotIn("success()", publish)

        evaluator_steps = yaml.safe_load(self.source)["jobs"]["evaluate-load"]["steps"]
        evidence_upload = next(
            step for step in evaluator_steps if step.get("name") == "Publish external load evidence"
        )
        self.assertIn(
            "needs.fixture-finalize.outputs.ssh_cleanup_status != ''",
            evidence_upload["if"],
        )
        self.assertIn(
            "needs.fixture-finalize.outputs.ssh_cleanup_status == '0'",
            evidence_upload["if"],
        )
        self.assertIn(
            "needs.fixture-setup.outputs.ssh_cleanup_status != ''",
            evidence_upload["if"],
        )
        self.assertIn(
            "needs.fixture-setup.outputs.ssh_cleanup_status == '0'",
            evidence_upload["if"],
        )

        evaluator = self.jobs["evaluate-load"]
        self.assertIn("sanitizer_status=0", evaluator)
        self.assertIn("External evidence projection/sanitizer failed.", evaluator)


if __name__ == "__main__":
    unittest.main()
