"""Fail-closed DAG contracts for the production external-load workflow.

These tests intentionally inspect the workflow as source.  The production
workflow has several ``always()`` branches so cleanup can run after a fault;
the final passing artifact is therefore guarded by an explicit truth table
instead of relying on GitHub's implicit job conclusion propagation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


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

    def test_failed_handoff_still_requires_cleanup_and_cleanup_failure_dominates(self) -> None:
        passing = {
            "validate_result": "success",
            "setup_result": "success",
            "setup_status": "0",
            "setup_ssh_cleanup_status": "0",
            "load_result": "success",
            "load_status": "0",
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
        self.assertIn("sha256sum -c", self.source)
        self.assertIn('workflow_run.get("id") != int(run_id)', self.source)
        self.assertIn('workflow_run.get("head_sha") != target_sha', self.source)
        for artifact in (
            "platform-production-external-load-input-${{ github.run_id }}-${{ github.run_attempt }}",
            "platform-production-external-load-manifest-${{ github.run_id }}-${{ github.run_attempt }}",
            "platform-production-external-load-client-${{ github.run_id }}-${{ github.run_attempt }}",
            "platform-production-external-load-origin-${{ github.run_id }}-${{ github.run_attempt }}",
        ):
            self.assertIn(artifact, self.source)
        self.assertIn("test -n \"$artifact_id\" && test -n \"$artifact_digest\"", self.source)
        self.assertIn("if-no-files-found: error", self.source)

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
                self.assertEqual(step_names[0], step_name)
                script = _step_script(job, step_name)
                self.assertLess(script.index("GITHUB_EVENT_PATH"), script.index("::add-mask::"))
                if "os.open(" in script:
                    self.assertLess(script.index("sys.stdout.flush()"), script.index("os.open("))

        # The two jobs that need a private local cleanup value stage it only
        # after the runner has received the masking command.
        for job_name, step_name in (
            ("validate-external-inputs", mask_steps["validate-external-inputs"]),
            ("fixture-finalize", mask_steps["fixture-finalize"]),
        ):
            script = _step_script(self.jobs[job_name], step_name)
            self.assertLess(script.index("::add-mask::"), script.index("os.open("))

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

        valid_event = {"inputs": {"control_email": "Control%qa@example.invalid"}}
        invalid_event = {
            "inputs": {"control_email": "private@example.invalid\r\n::warning::injected"}
        }
        for job_name, step_name in (
            ("validate-external-inputs", mask_steps["validate-external-inputs"]),
            ("fixture-setup", mask_steps["fixture-setup"]),
            ("load-client", mask_steps["load-client"]),
            ("fixture-finalize", mask_steps["fixture-finalize"]),
        ):
            script = _step_script(self.jobs[job_name], step_name)
            with self.subTest(job=job_name, event="valid"):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    event_path = root / "event.json"
                    event_path.write_text(json.dumps(valid_event), encoding="utf-8")
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
                    self.assertNotIn("::warning::", result.stdout)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(
                        result.stdout.splitlines(),
                        [
                            "::add-mask::Control%25qa@example.invalid",
                            "::add-mask::control%25qa@example.invalid",
                        ],
                    )
                    if "stage" in step_name:
                        email_file = root / "platform-production-control-email"
                        self.assertEqual(email_file.read_text(encoding="ascii"), "control%qa@example.invalid\n")
                        self.assertEqual(email_file.stat().st_mode & 0o777, 0o600)
            with self.subTest(job=job_name, event="malicious-crlf"):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    event_path = root / "event.json"
                    event_path.write_text(json.dumps(invalid_event), encoding="utf-8")
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
                    self.assertNotIn("private@example.invalid", result.stderr)
                    self.assertNotIn("::warning::", result.stderr)
                    self.assertFalse((root / "platform-production-control-email").exists())

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
        self.assertNotIn('needs.load-client.outputs.load_status }}\" == 0', evaluator)
        self.assertIn('needs.fixture-finalize.outputs.cleanup_exports_status }}\" == 0', evaluator)
        publish = evaluator.split("- name: Publish external load evidence", 1)[1]
        self.assertIn("needs.load-client.result == 'success'", publish)
        self.assertIn("steps.evaluate-load.outputs.evaluation_status == '0'", publish)
        self.assertIn("steps.sanitize.outputs.sanitizer_status == '0'", publish)

    def test_cleanup_exports_and_projection_are_failure_bearing(self) -> None:
        finalizer = self.jobs["fixture-finalize"]
        self.assertIn("external-cleanup-exports", finalizer)
        self.assertIn("cleanup_exports_status=", finalizer)
        self.assertIn("cleanup_status\" != 0 || \"$cleanup_exports_status\" != 0", finalizer)
        self.assertIn("cleanup summary projection input is invalid", finalizer)
        diagnostic = finalizer.split(
            "- name: Diagnose origin evidence publication gate", 1
        )[1].split("- name: Publish origin evidence", 1)[0]
        diagnostic_script = _step_script(
            finalizer, "Diagnose origin evidence publication gate"
        )
        self.assertIn("if: ${{ always() }}", diagnostic)
        self.assertIn("ORIGIN_PUBLISH_GATE", diagnostic_script)
        self.assertNotIn("${{", diagnostic_script)
        self.assertNotIn("secrets.", diagnostic)
        for category in (
            '"missing"',
            '"invalid"',
            '"cancelled"',
            '"skipped"',
            "eligible=",
        ):
            with self.subTest(category=category):
                self.assertIn(category, diagnostic_script)
        allowed_environment = {
            "ORIGIN_PRIOR_STEPS_SUCCESS": "true",
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
        }
        valid_summary = subprocess.run(
            ["/bin/bash", "-c", diagnostic_script],
            check=True,
            capture_output=True,
            text=True,
            env=allowed_environment,
            timeout=10,
        ).stdout
        self.assertIn("eligible=yes", valid_summary)

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
        self.assertIn("eligible=no", invalid_summary)
        self.assertNotIn("raw-path-or-secret", invalid_summary)

        publish = finalizer.split("- name: Publish origin evidence", 1)[1].split(
            "\n      - name:", 1
        )[0]
        for gate_input in (
            "success()",
            "needs.namespace-containment-barrier.outputs.namespace_closed_status == '0'",
            "needs.namespace-containment-manual-barrier.outputs.manual_required == '0'",
            "steps.cleanup_ssh.outcome == 'success'",
            "steps.external-finalize.outputs.remote_status == '0'",
            "steps.external-finalize.outputs.observer_ready == '1'",
            "steps.external-finalize.outputs.finalize_status == '0'",
            "steps.cleanup.outputs.cleanup_status == '0'",
            "steps.cleanup.outputs.cleanup_exports_status == '0'",
            "steps.cleanup_ssh.outputs.ssh_cleanup_status == '0'",
            "steps.revalidate-finalizer.outputs.cleanup_identity_status == '0'",
        ):
            with self.subTest(gate_input=gate_input):
                self.assertIn(gate_input, publish)

        evaluator = self.jobs["evaluate-load"]
        self.assertIn("sanitizer_status=0", evaluator)
        self.assertIn("External evidence projection/sanitizer failed.", evaluator)


if __name__ == "__main__":
    unittest.main()
