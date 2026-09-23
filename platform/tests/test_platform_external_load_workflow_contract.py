"""Fail-closed DAG contracts for the production external-load workflow.

These tests intentionally inspect the workflow as source.  The production
workflow has several ``always()`` branches so cleanup can run after a fault;
the final passing artifact is therefore guarded by an explicit truth table
instead of relying on GitHub's implicit job conclusion propagation.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-external-load.yml"


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


class ExternalLoadWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = WORKFLOW.read_text(encoding="utf-8")
        cls.jobs = _jobs(cls.source)

    def test_dag_has_explicit_result_barriers_and_always_cleanup(self) -> None:
        self.assertIn("needs.fixture-setup.result == 'success'", self.jobs["load-client"])
        self.assertIn("needs.validate-external-inputs.result == 'success'", self.jobs["load-client"])
        self.assertIn("if: ${{ always() }}", self.jobs["fixture-finalize"])
        self.assertIn("if: ${{ always() }}", self.jobs["evaluate-load"])
        self.assertIn("needs.fixture-setup.result", self.jobs["evaluate-load"])
        self.assertIn("needs.load-client.result", self.jobs["evaluate-load"])
        self.assertIn("needs.fixture-finalize.result", self.jobs["evaluate-load"])
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

    def test_evaluator_cannot_hide_upstream_failure_or_publish_success(self) -> None:
        evaluator = self.jobs["evaluate-load"]
        self.assertIn("upstream_ready=0", evaluator)
        self.assertIn('needs.load-client.outputs.load_status }}\" == 0', evaluator)
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
        evaluator = self.jobs["evaluate-load"]
        self.assertIn("sanitizer_status=0", evaluator)
        self.assertIn("External evidence projection/sanitizer failed.", evaluator)


if __name__ == "__main__":
    unittest.main()
