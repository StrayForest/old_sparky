"""Fail-closed DAG contracts for the production external-load workflow.

These tests intentionally inspect the workflow as source.  The production
workflow has several ``always()`` branches so cleanup can run after a fault;
the final passing artifact is therefore guarded by an explicit truth table
instead of relying on GitHub's implicit job conclusion propagation.
"""

from __future__ import annotations

from pathlib import Path
import re
import shlex
import os
import subprocess
import tempfile
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-external-load.yml"
TRUSTED_WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-external-load-trusted.yml"
RECOVERY_WORKFLOW = REPO_ROOT / ".github/workflows/platform-production-external-load-recovery.yml"
PROVENANCE_HELPER = REPO_ROOT / "platform/tools/platform_external_load_provenance.py"


def _jobs(source: str) -> dict[str, str]:
    matches = re.finditer(
        r"^  (?P<name>[A-Za-z0-9_-]+):\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        source,
        re.MULTILINE | re.DOTALL,
    )
    return {match.group("name"): match.group("body") for match in matches}


def _curl_commands(source: str) -> list[str]:
    """Join each backslash-continued curl command for argv policy checks."""

    lines = source.splitlines()
    commands: list[str] = []
    for index, line in enumerate(lines):
        command = line.strip()
        if not re.match(r"^curl(?:\s|\\)", command):
            continue
        next_index = index
        while command.endswith("\\") and next_index + 1 < len(lines):
            command = f"{command[:-1].rstrip()} {lines[next_index + 1].strip()}"
            next_index += 1
        commands.append(command)
    return commands


def _pass_truth_table(state: dict[str, object]) -> bool:
    """Model the final gate's authoritative status inputs."""

    return all(
        (
            state["validate_result"] == "success",
            state["setup_result"] == "success",
            state["setup_status"] == "0",
            state["manifest_validation_status"] == "0",
            state["credential_cleanup_status"] == "0",
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
            state["diagnostic_ids_status"] == "0",
            state["ssh_cleanup_status"] == "0",
            state["cleanup_identity_status"] == "0",
            state["handoff_status"] == "0",
            state["candidate_artifact_status"] == "0",
            state["origin_artifact_status"] == "0",
            state["input_artifact_status"] == "0",
            state["evaluation_status"] == "0",
            state["sanitizer_status"] == "0",
            state["provenance_status"] == "0",
        )
    )


class ExternalLoadWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.entry_source = WORKFLOW.read_text(encoding="utf-8")
        cls.source = TRUSTED_WORKFLOW.read_text(encoding="utf-8")
        cls.recovery_source = RECOVERY_WORKFLOW.read_text(encoding="utf-8")
        cls.jobs = _jobs(cls.source)
        cls.entry_document = yaml.safe_load(cls.entry_source)
        cls.trusted_document = yaml.safe_load(cls.source)
        cls.recovery_document = yaml.safe_load(cls.recovery_source)

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
            "needs.fixture-setup.outputs.manifest_validation_status",
            "needs.fixture-setup.outputs.credential_cleanup_status",
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
            "needs.fixture-finalize.outputs.diagnostic_ids_status",
            "needs.fixture-finalize.outputs.ssh_cleanup_status",
            "needs.fixture-finalize.outputs.cleanup_identity_status",
            "needs.fixture-finalize.outputs.handoff_status",
            "needs.fixture-finalize.outputs.candidate_artifact_status",
            "steps.verify-evaluator-artifacts.outputs.candidate_artifact_status",
            "steps.verify-evaluator-artifacts.outputs.origin_artifact_status",
            "steps.verify-evaluator-artifacts.outputs.input_artifact_status",
            "steps.evaluate-load.outputs.evaluation_status",
            "steps.sanitize.outputs.sanitizer_status",
            "steps.provenance.outputs.provenance_status",
        )
        for status in statuses:
            with self.subTest(status=status):
                self.assertIn(status, gate)

    def test_fault_injection_truth_table_rejects_each_phase_failure(self) -> None:
        passing = {
            "validate_result": "success",
            "setup_result": "success",
            "setup_status": "0",
            "manifest_validation_status": "0",
            "credential_cleanup_status": "0",
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
            "diagnostic_ids_status": "0",
            "ssh_cleanup_status": "0",
            "cleanup_identity_status": "0",
            "handoff_status": "0",
            "candidate_artifact_status": "0",
            "origin_artifact_status": "0",
            "input_artifact_status": "0",
            "evaluation_status": "0",
            "sanitizer_status": "0",
            "provenance_status": "0",
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
            "manifest_validation_status": "0",
            "credential_cleanup_status": "0",
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
            "diagnostic_ids_status": "0",
            "ssh_cleanup_status": "0",
            "cleanup_identity_status": "0",
            "handoff_status": "0",
            "candidate_artifact_status": "0",
            "origin_artifact_status": "0",
            "input_artifact_status": "0",
            "evaluation_status": "0",
            "sanitizer_status": "0",
            "provenance_status": "0",
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
        self.assertIn("platform_external_load_provenance.py", self.source)
        self.assertIn("--archive", self.source)
        provenance = PROVENANCE_HELPER.read_text(encoding="utf-8")
        self.assertIn('workflow_run.get("id")', provenance)
        self.assertIn('workflow_run.get("head_sha")', provenance)
        self.assertIn("validate_artifact_archive", provenance)
        for artifact in (
            "platform-production-external-load-input-${{ inputs.source_run_id }}-${{ inputs.source_run_attempt }}",
            "platform-production-external-load-client-${{ inputs.source_run_id }}-${{ inputs.source_run_attempt }}",
            "platform-production-external-load-origin-${{ inputs.source_run_id }}-${{ inputs.source_run_attempt }}",
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
        self.assertIn("steps.provenance.outputs.provenance_status == '0'", publish)
        self.assertIn("needs.fixture-finalize.outputs.diagnostic_ids_status == '0'", publish)

    def test_evaluator_binds_target_and_source_sha_and_uses_pure_validator(self) -> None:
        evaluator = self.jobs["evaluate-load"]
        self.assertIn("TARGET_SHA: ${{ inputs.target_sha }}", evaluator)
        self.assertIn("ref: ${{ env.TRUSTED_RUNNER_SHA }}", evaluator)
        self.assertIn("trusted-external-load-runner", evaluator)
        self.assertNotIn("ref: ${{ env.TARGET_SHA }}", evaluator)
        self.assertNotIn("platform_load.py run", evaluator)
        for command in ("artifact", "load-status", "report"):
            self.assertIn(
                f"platform_external_load_provenance.py\" {command}",
                evaluator,
            )
        self.assertIn(
            'tools/platform_load.py evaluate --profile "$PROFILE_ID"',
            evaluator,
        )
        self.assertIn(
            'steps.verify-evaluator-artifacts.outputs.origin_artifact_status }}" == 0',
            evaluator,
        )
        self.assertIn(
            'steps.verify-evaluator-artifacts.outputs.input_artifact_status }}" == 0',
            evaluator,
        )
        self.assertIn("- name: Download exact source run metadata", evaluator)
        self.assertIn("id: download-run-metadata", evaluator)
        self.assertIn("install -m 600 /dev/null", evaluator)
        self.assertIn("RUN_METADATA_PATH: ${{ steps.download-run-metadata.outputs.run_metadata_path }}", evaluator)
        self.assertIn("run_metadata_path_status=0", evaluator)
        self.assertIn("external-load-source-run-metadata.json", evaluator)
        self.assertIn("stat -c '%a' \"$RUN_METADATA_PATH\"", evaluator)
        self.assertIn('--run-metadata "$run_metadata_path"', evaluator)
        self.assertLess(
            evaluator.index("- name: Download exact source run metadata"),
            evaluator.index("- name: Verify evaluator artifact identity and digest"),
        )
        setup = self.jobs["fixture-setup"]
        self.assertIn('--timeout-diagnostics-run-id "$RUN_ID"', setup)
        self.assertIn('MANIFEST_VALIDATION_STATUS:-1', setup)

    def test_network_transfers_have_bounded_arguments_without_retry(self) -> None:
        def assert_external_curl_policy(source: str) -> None:
            commands = _curl_commands(source)
            self.assertTrue(commands)
            for command in commands:
                is_artifact_download = "--location" in command
                expected = (
                    ("--connect-timeout 10", "--max-time 30", "--max-filesize 67108864")
                    if is_artifact_download
                    else ("--connect-timeout 5", "--max-time 15", "--max-filesize 65536")
                )
                for option in expected:
                    self.assertEqual(1, command.count(option), command)
                self.assertEqual(1, command.count("--retry"), command)
                self.assertIn("--retry 0", command)
                self.assertNotIn("--retry 1", command)

        for source in (self.source, self.recovery_source):
            assert_external_curl_policy(source)

        for source in (
            (REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup-trusted.yml").read_text(
                encoding="utf-8"
            ),
            (REPO_ROOT / ".github/workflows/platform-production-retained-load-abort-trusted.yml").read_text(
                encoding="utf-8"
            ),
        ):
            commands = _curl_commands(source)
            self.assertTrue(commands)
            self.assertEqual(source.count("--connect-timeout 10"), len(commands))
            self.assertEqual(source.count("--max-time 30"), len(commands))
            self.assertNotIn("--retry", source)

        for label, mutated in (
            ("metadata connect timeout", self.source.replace("--connect-timeout 5", "--connect-timeout 6", 1)),
            ("metadata absolute timeout", self.source.replace("--max-time 15", "--max-time 30", 1)),
            ("enabled retry", self.source.replace("--retry 0", "--retry 1", 1)),
            ("removed retry", self.source.replace("--retry 0 ", "", 1)),
            ("metadata response cap", self.source.replace("--max-filesize 65536", "--max-filesize 65537", 1)),
            ("artifact absolute timeout", self.source.replace("--max-time 30", "--max-time 31", 1)),
        ):
            with self.subTest(mutation=label):
                with self.assertRaises(AssertionError):
                    assert_external_curl_policy(mutated)

    def test_fake_transfer_argv_contains_the_boundaries_from_source(self) -> None:
        """Exercise representative source command argv through fake binaries."""

        def first_command(command: str) -> list[str]:
            for raw_line in self.source.splitlines():
                line = raw_line.strip()
                line = re.sub(r"^(?:if\s+|&&\s+)", "", line)
                if not line.startswith(f"{command} "):
                    continue
                return shlex.split(line.rstrip("\\").strip())[1:]
            self.fail(f"no representative {command} command")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            trace = root / "argv"
            for command in ("curl", "ssh", "scp"):
                fake = fake_bin / command
                fake.write_text(
                    '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$TRACE"\n',
                    encoding="utf-8",
                )
                fake.chmod(0o755)
                argv = first_command(command)
                environment = os.environ.copy()
                environment.update({"PATH": f"{fake_bin}:/usr/bin:/bin", "TRACE": str(trace)})
                result = subprocess.run(
                    [command, *argv],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(trace.read_text(encoding="utf-8").splitlines(), argv)
                if command == "curl":
                    self.assertEqual(argv[argv.index("--connect-timeout") + 1], "5")
                    self.assertEqual(argv[argv.index("--max-time") + 1], "15")
                    self.assertEqual(argv[argv.index("--retry") + 1], "0")
                    self.assertEqual(argv[argv.index("--max-filesize") + 1], "65536")
                else:
                    for option in (
                        "ConnectTimeout=10",
                        "ServerAliveInterval=15",
                        "ServerAliveCountMax=3",
                    ):
                        self.assertIn(option, argv)

    def test_cleanup_status_outputs_are_failure_bearing(self) -> None:
        setup = self.jobs["fixture-setup"]
        self.assertLess(
            setup.index("- name: Remove fixture-setup SSH material"),
            setup.index("- name: Publish trusted load data"),
        )
        load_publish = setup.split("- name: Publish trusted load data", 1)[1]
        self.assertIn("steps.external-evaluate.outcome == 'success'", load_publish)
        self.assertIn("steps.cleanup_ssh.outputs.ssh_cleanup_status == '0'", load_publish)
        self.assertIn("steps.validate-manifest.outputs.manifest_validation_status == '0'", load_publish)
        self.assertIn("steps.external-evaluate.outputs.credential_cleanup_status == '0'", load_publish)
        self.assertIn("needs.fixture-setup.outputs.ssh_cleanup_status == '0'", self.jobs["load-client"])
        for job_name in ("fixture-setup", "fixture-finalize"):
            job = self.jobs[job_name]
            self.assertIn("ssh_cleanup_status:", job)
            self.assertIn('echo "ssh_cleanup_status=$ssh_cleanup_status"', job)
        self.assertIn("CLEANUP_SSH_STATUS", setup)
        for name in (
            "platform-production-retained-load-cleanup.yml",
            "platform-production-retained-load-abort.yml",
        ):
            source = (REPO_ROOT / ".github/workflows" / name).read_text(
                encoding="utf-8"
            )
            trusted_name = name.replace(".yml", "-trusted.yml")
            trusted_source = (REPO_ROOT / ".github/workflows" / trusted_name).read_text(
                encoding="utf-8"
            )
            self.assertIn('echo "ssh_cleanup_status=$ssh_cleanup_status"', trusted_source)
            self.assertIn("outputs.ssh_cleanup_status == '0'", trusted_source)
            self.assertRegex(source, rf"uses: StrayForest/old_sparky/\.github/workflows/{re.escape(trusted_name)}@[0-9a-f]{{40}}")

    def test_cleanup_exports_and_projection_are_failure_bearing(self) -> None:
        finalizer = self.jobs["fixture-finalize"]
        self.assertIn("external-cleanup-exports", finalizer)
        self.assertIn("cleanup_exports_status=", finalizer)
        self.assertIn("cleanup_status\" != 0 || \"$cleanup_exports_status\" != 0", finalizer)
        self.assertIn("cleanup summary projection input is invalid", finalizer)
        evaluator = self.jobs["evaluate-load"]
        self.assertIn("sanitizer_status=0", evaluator)
        self.assertIn("remove_file()", evaluator)
        self.assertIn("move_file()", evaluator)
        self.assertIn("remove_tree()", evaluator)
        self.assertIn("rm -rf --", evaluator)
        self.assertIn("External evidence projection/sanitizer failed.", evaluator)
        self.assertIn(
            'sanitize_log "$RUNNER_TEMP/external-evidence/cleanup-canonical.log" "$public_dir/cleanup-canonical.log"',
            evaluator,
        )
        self.assertIn(
            'remove_file "$RUNNER_TEMP/external-evidence/cleanup-canonical.log"',
            evaluator,
        )
        self.assertIn('canonical_raw="$artifact_dir/cleanup-canonical.raw"', finalizer)
        self.assertIn('cleanup_log="$artifact_dir/cleanup-canonical.log"', finalizer)
        self.assertIn('platform_evidence_sanitizer.py" \\', finalizer)
        self.assertIn('rm -f -- "$canonical_raw"', finalizer)

    def test_trusted_runner_is_the_only_executable_and_profile_is_data_only(self) -> None:
        self.assertNotIn("sparse-checkout:", self.source)
        self.assertIn("Contents API", self.jobs["validate-external-inputs"])
        for job_name in ("validate-external-inputs", "fixture-setup", "load-client", "fixture-finalize", "evaluate-load"):
            job = self.jobs[job_name]
            self.assertIn("env.TRUSTED_RUNNER_SHA", job, job_name)
        validate = self.jobs["validate-external-inputs"]
        self.assertIn("platform/performance/profiles", validate)
        self.assertIn("platform_external_load_provenance.py\" profile", validate)
        self.assertNotIn("platform/tools/platform_load.py run", validate)
        self.assertNotIn("platform/tools/platform_load.py validate", validate)
        setup = self.jobs["fixture-setup"]
        self.assertIn("Run trusted external load after SSH cleanup", setup)
        self.assertIn("env -i PATH=", setup)
        self.assertIn("chmod 600", setup)
        self.assertIn("manifest_path", setup)
        self.assertIn("cleanup_local", setup)
        self.assertNotIn("Publish closed fixture manifest", self.source)
        self.assertNotIn("platform-production-external-load-manifest-", self.source)
        self.assertNotIn("client-raw.log", self.source)
        self.assertIn("profile_digest", self.source)
        self.assertIn("--trusted-sha", self.source)
        self.assertIn("trusted_runner_sha", PROVENANCE_HELPER.read_text(encoding="utf-8"))

    def test_finalizer_uses_extracted_metadata_and_archive_byte_validator(self) -> None:
        finalizer = self.jobs["fixture-finalize"]
        self.assertIn("--archive \"$archive\"", finalizer)
        self.assertIn("platform_external_load_provenance.py\" artifact", finalizer)
        self.assertNotIn("sha256sum -c", finalizer)
        self.assertIn("--artifact-id \"$artifact_id\"", finalizer)
        self.assertIn("--artifact-name \"$artifact_name\"", finalizer)
        self.assertIn("--run-attempt \"$SOURCE_RUN_ATTEMPT\"", finalizer)

    def test_manifest_validation_and_credential_deletion_are_terminal_inputs(self) -> None:
        setup = self.jobs["fixture-setup"]
        self.assertIn("id: validate-manifest", setup)
        self.assertIn('echo "manifest_validation_status=$manifest_status"', setup)
        self.assertIn("credential_cleanup_status=%s", setup)
        self.assertIn('if [[ "${CLEANUP_SSH_STATUS:-1}" == 0', setup)
        self.assertIn('&& "${MANIFEST_VALIDATION_STATUS:-1}" == 0', setup)
        cleanup = setup.split("- name: Remove fixture-setup SSH material", 1)[1].split(
            "- name: Run trusted external load after SSH cleanup", 1
        )[0]
        load = setup.split("- name: Run trusted external load after SSH cleanup", 1)[1]
        self.assertIn('[[ -f "$manifest_path" && ! -L "$manifest_path" ]]', cleanup)
        self.assertIn('[[ "$(stat -c \'%a\' "$manifest_path"', cleanup)
        self.assertIn('          else\n            rm -f -- "$manifest_path" || ssh_cleanup_status=1', cleanup)
        self.assertIn("trap 'cleanup_local' EXIT", load)
        self.assertIn('rm -f -- "$raw_report" "$log_path" "$manifest_path"', load)
        self.assertLess(load.index("trap 'cleanup_local' EXIT"), load.index("load_args=("))

    def test_retained_cleanup_uses_explicit_load_sha_and_abort_has_terminal_gate(self) -> None:
        cleanup = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("target_sha:", cleanup)
        cleanup_trusted = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup-trusted.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("TARGET_SHA: ${{ inputs.target_sha }}", cleanup_trusted)
        self.assertNotIn("TARGET_SHA: ${{ github.sha }}", cleanup)
        abort_trusted = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-abort-trusted.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("id: run-abort", abort_trusted)
        self.assertIn("Fail if abort evidence or SSH cleanup was not verified", abort_trusted)
        self.assertIn("steps.run-abort.outputs.remote_status", abort_trusted)
        self.assertIn("steps.cleanup_ssh.outputs.ssh_cleanup_status", abort_trusted)

    def test_public_entry_is_a_parsed_pinned_data_only_boundary(self) -> None:
        event = self.entry_document.get("on", self.entry_document.get(True))
        self.assertIn("workflow_dispatch", event)
        job = self.entry_document["jobs"]["trusted-external-load"]
        uses = job["uses"]
        self.assertRegex(uses, r"^StrayForest/old_sparky/\.github/workflows/platform-production-external-load-trusted\.yml@[0-9a-f]{40}$")
        self.assertEqual(
            uses,
            "StrayForest/old_sparky/.github/workflows/"
            "platform-production-external-load-trusted.yml@"
            "251a4e814abfff59ba4fdff5db4b030829cb889c",
        )
        self.assertNotIn("runs-on", job)
        self.assertNotIn("environment", job)
        self.assertNotIn("steps", job)
        self.assertNotIn("secrets", job)
        self.assertNotIn("secrets: inherit", self.entry_source)
        for forbidden in ("actions/checkout", "platform_load.py", "ssh ", "scp ", "PROD_SSH_", "environment: production"):
            self.assertNotIn(forbidden, self.entry_source)

    def test_identity_gate_precedes_external_and_retained_secret_jobs(self) -> None:
        identity = self.trusted_document["jobs"]["validate-caller-identity"]
        self.assertNotIn("environment", identity)
        self.assertNotIn("secrets", identity)
        identity_source = self.jobs["validate-caller-identity"]
        for marker in (
            "EXPECTED_WORKFLOW_PATH: .github/workflows/platform-production-external-load.yml",
            "CALLER_WORKFLOW_REF",
            'CALLER_WORKFLOW_REF" == "$EXPECTED_REPOSITORY/$EXPECTED_WORKFLOW_PATH@$EXPECTED_DEFAULT_REF"',
            'CALLER_WORKFLOW_SHA" =~ ^[0-9a-f]{40}$',
            "commits/$CALLER_WORKFLOW_SHA",
            "branches/dev",
            'payload.get("protected") is not True',
            'CALLER_EVENT_NAME" == "workflow_dispatch"',
            'CALLER_REF" == "$EXPECTED_DEFAULT_REF"',
            'CALLER_REF_PROTECTED" == "true"',
            "actions/runs/$CALLER_RUN_ID",
            'payload.get("path") != workflow_path',
            'payload.get("head_branch") != "dev"',
            "actions/workflows/$workflow_id",
            'payload.get("default_branch") != "dev"',
        ):
            self.assertIn(marker, identity_source)
        for job_name in ("resolve-trusted-runner", "fixture-setup", "fixture-finalize"):
            job = self.trusted_document["jobs"][job_name]
            self.assertIn("validate-caller-identity", str(job.get("needs")))
            self.assertIn("needs.validate-caller-identity.outputs.caller_identity_validated", str(job.get("if")))

        retained = (
            (
                "platform-production-retained-load-cleanup.yml",
                "platform-production-retained-load-cleanup-trusted.yml",
                "cleanup",
                {
                    "confirmation",
                    "load_run_id",
                    "target_sha",
                    "control_email",
                },
                ".github/workflows/platform-production-retained-load-cleanup.yml",
                "Platform production retained load cleanup",
            ),
            (
                "platform-production-retained-load-abort.yml",
                "platform-production-retained-load-abort-trusted.yml",
                "abort",
                {"confirmation", "load_run_id", "target_sha"},
                ".github/workflows/platform-production-retained-load-abort.yml",
                "Platform production retained load abort",
            ),
        )
        for wrapper_name, trusted_name, production_job_name, expected_inputs, expected_path, expected_name in retained:
            wrapper_source = (REPO_ROOT / ".github/workflows" / wrapper_name).read_text(encoding="utf-8")
            trusted_source = (REPO_ROOT / ".github/workflows" / trusted_name).read_text(encoding="utf-8")
            wrapper = yaml.safe_load(wrapper_source)
            trusted = yaml.safe_load(trusted_source)
            wrapper_event = wrapper.get("on", wrapper.get(True))
            trusted_event = trusted.get("on", trusted.get(True))
            self.assertEqual(set(wrapper_event["workflow_dispatch"]["inputs"]), expected_inputs)
            self.assertEqual(set(trusted_event["workflow_call"]["inputs"]), expected_inputs)
            for event in (wrapper_event["workflow_dispatch"], trusted_event["workflow_call"]):
                for input_name in expected_inputs:
                    specification = event["inputs"][input_name]
                    self.assertEqual(specification["required"], True)
                    self.assertEqual(specification["type"], "string")
            self.assertNotIn("secrets", wrapper_source)
            wrapper_jobs = wrapper["jobs"]
            self.assertEqual(set(wrapper_jobs), {production_job_name})
            wrapper_job = wrapper_jobs[production_job_name]
            self.assertEqual(set(wrapper_job), {"uses", "with"})
            self.assertEqual(
                wrapper_job["with"],
                {name: f"${{{{ inputs.{name} }}}}" for name in expected_inputs},
            )
            self.assertRegex(
                wrapper_job["uses"],
                rf"^StrayForest/old_sparky/\.github/workflows/{re.escape(trusted_name)}@[0-9a-f]{{40}}$",
            )
            trusted_jobs = trusted["jobs"]
            self.assertEqual(trusted.get("permissions"), {"contents": "read"})
            self.assertIn("validate-caller-identity", trusted_jobs)
            gate = trusted_jobs["validate-caller-identity"]
            self.assertNotIn("environment", gate)
            self.assertNotIn("secrets", gate)
            gate_source = _jobs(trusted_source)["validate-caller-identity"]
            for marker in (
                f"EXPECTED_WORKFLOW_PATH: {expected_path}",
                f"EXPECTED_WORKFLOW_NAME: {expected_name}",
                "CALLER_WORKFLOW_REF",
                'CALLER_WORKFLOW_REF" == "$EXPECTED_REPOSITORY/$EXPECTED_WORKFLOW_PATH@$EXPECTED_DEFAULT_REF"',
                'CALLER_WORKFLOW_SHA" =~ ^[0-9a-f]{40}$',
                "commits/$CALLER_WORKFLOW_SHA",
                "branches/dev",
                'payload.get("protected") is not True',
                'CALLER_EVENT_NAME" == "workflow_dispatch"',
                'CALLER_REF" == "$EXPECTED_DEFAULT_REF"',
                "actions/runs/$CALLER_RUN_ID",
                'payload.get("path") != workflow_path',
                'payload.get("head_branch") != "dev"',
                "actions/workflows/$workflow_id",
                'payload.get("default_branch") != "dev"',
            ):
                self.assertIn(marker, gate_source, wrapper_name)
            production_job = trusted_jobs[production_job_name]
            self.assertEqual(production_job["environment"], "production")
            self.assertIn("validate-caller-identity", str(production_job["needs"]))
            self.assertIn("needs.validate-caller-identity.outputs.caller_identity_validated", str(production_job["if"]))
            self.assertIn("head -c 1048576", trusted_source)

    def test_real_github_workflow_context_shape_rejects_ref_and_sha_drift(self) -> None:
        repository = "StrayForest/old_sparky"
        workflow_path = ".github/workflows/platform-production-external-load.yml"
        protected_ref = "refs/heads/dev"

        def accepted(context: dict[str, object]) -> bool:
            workflow_sha = context.get("workflow_sha")
            source_sha = context.get("sha")
            return (
                context.get("repository") == repository
                and context.get("workflow_ref") == f"{repository}/{workflow_path}@{protected_ref}"
                and context.get("ref") == protected_ref
                and context.get("ref_protected") is True
                and isinstance(workflow_sha, str)
                and re.fullmatch(r"[0-9a-f]{40}", workflow_sha) is not None
                and isinstance(source_sha, str)
                and re.fullmatch(r"[0-9a-f]{40}", source_sha) is not None
            )

        valid = {
            "repository": repository,
            "workflow_ref": f"{repository}/{workflow_path}@{protected_ref}",
            "workflow_sha": "a" * 40,
            "ref": protected_ref,
            "ref_protected": True,
            "sha": "b" * 40,
        }
        self.assertTrue(accepted(valid))
        wrong_ref = dict(valid, workflow_ref=f"{repository}/{workflow_path}@{valid['workflow_sha']}")
        self.assertFalse(accepted(wrong_ref))
        wrong_branch = dict(valid, workflow_ref=f"{repository}/{workflow_path}@refs/heads/main", ref="refs/heads/main")
        self.assertFalse(accepted(wrong_branch))
        wrong_workflow_sha = dict(valid, workflow_sha="A" * 40)
        self.assertFalse(accepted(wrong_workflow_sha))
        wrong_source_sha = dict(valid, sha="not-a-commit")
        self.assertFalse(accepted(wrong_source_sha))

    def test_trusted_workflow_owns_secret_jobs_and_rejects_malicious_e_surface(self) -> None:
        trusted_on = self.trusted_document.get("on", self.trusted_document.get(True))
        self.assertIn("workflow_call", trusted_on)
        self.assertIn("resolve-trusted-runner", self.trusted_document["jobs"])
        resolver = self.trusted_document["jobs"]["resolve-trusted-runner"]
        self.assertEqual(resolver["environment"], "production")
        for name in ("fixture-setup", "fixture-finalize"):
            self.assertEqual(self.trusted_document["jobs"][name]["environment"], "production")
        self.assertIn("PROFILE_ID", self.jobs["validate-external-inputs"])
        self.assertIn("Contents API", self.jobs["validate-external-inputs"])
        self.assertIn("object_pairs_hook", self.jobs["validate-external-inputs"])

    def test_cancellation_recovery_is_t_owned_and_binds_the_triggering_run(self) -> None:
        event = self.recovery_document.get("on", self.recovery_document.get(True))
        self.assertEqual(event["workflow_run"]["workflows"], ["Platform production external load"])
        self.assertEqual(event["workflow_run"]["types"], ["completed"])
        recovery_jobs = self.recovery_document["jobs"]
        self.assertIn("validate-trigger-identity", recovery_jobs)
        self.assertIn("resolve-trusted-runner", recovery_jobs)
        self.assertIn("recover", recovery_jobs)
        identity = recovery_jobs["validate-trigger-identity"]
        resolve = recovery_jobs["resolve-trusted-runner"]
        recover = recovery_jobs["recover"]
        self.assertNotIn("environment", identity)
        self.assertIn("workflow_id", identity["outputs"])
        identity_source = self.recovery_source.split("- name: Verify protected workflow-run", 1)[1]
        self.assertIn('payload.get("event") != "workflow_dispatch"', identity_source)
        self.assertIn('payload.get("path") != workflow_path', identity_source)
        self.assertIn('payload.get("name") != workflow_name', identity_source)
        self.assertIn('payload.get("head_branch") != "dev"', identity_source)
        self.assertIn('payload.get("default_branch") != "dev"', identity_source)
        self.assertIn('payload.get("name") != workflow_name', identity_source)
        self.assertIn('payload.get("state") != "active"', identity_source)
        self.assertIn("branches/dev", identity_source)
        self.assertIn('payload.get("protected") is not True', identity_source)
        self.assertIn("actions/workflows/$workflow_id", identity_source)
        self.assertIn("actions/runs/$SOURCE_RUN_ID/artifacts", identity_source)
        self.assertIn("validate-trigger-identity", str(resolve.get("needs")))
        self.assertIn("validate-trigger-identity", str(recover.get("needs")))
        self.assertIn("needs.validate-trigger-identity.outputs.trigger_validated", str(recover.get("if")))
        self.assertEqual(resolve["environment"], "production")
        self.assertEqual(recover["environment"], "production")
        self.assertEqual(recover["permissions"], {"actions": "read", "contents": "read"})
        self.assertIn("cancelled", recover["if"])
        self.assertIn("timed_out", recover["if"])
        self.assertIn("failure", recover["if"])
        recovery_uses = [
            step.get("uses", "")
            for step in recover["steps"]
            if isinstance(step, dict)
        ]
        self.assertFalse(any("actions/download-artifact@" in value for value in recovery_uses))
        self.assertTrue(any("actions/checkout@" in value for value in recovery_uses))
        self.assertIn("actions/runs/$SOURCE_RUN_ID", self.recovery_source)
        self.assertIn("run_attempt", self.recovery_source)
        self.assertIn("head_sha", self.recovery_source)
        self.assertIn("external-cleanup-exports", self.recovery_source)
        self.assertIn("platform_external_load_provenance.py\" artifact", self.recovery_source)
        self.assertIn("--run-metadata", self.recovery_source)
        self.assertIn("PurePosixPath", self.recovery_source)
        self.assertIn("bounded_receiver", self.recovery_source)
        self.assertIn("Fail if recovery cleanup was not verified", self.recovery_source)
        self.assertIn("trap cleanup_local EXIT INT TERM HUP", self.recovery_source)
        self.assertLess(
            self.recovery_source.index("trap cleanup_local EXIT INT TERM HUP"),
            self.recovery_source.index('printf \'%s\\n\' "$PROD_SSH_KEY"'),
        )
        self.assertNotIn("secrets: inherit", self.recovery_source)
        self.assertNotIn("ref: ${{ github.event.workflow_run.head_sha }}", self.recovery_source)

    def test_recovery_and_retained_ssh_outputs_are_bounded_and_transfers_verify_size(self) -> None:
        for path in (
            RECOVERY_WORKFLOW,
            REPO_ROOT / ".github/workflows/platform-production-retained-load-abort-trusted.yml",
            REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup-trusted.yml",
        ):
            source = path.read_text(encoding="utf-8")
            if path == RECOVERY_WORKFLOW:
                self.assertIn("bounded_receiver", source, path.name)
            else:
                self.assertIn("head -c 1048576", source, path.name)
            if path != RECOVERY_WORKFLOW:
                self.assertIn("stat -c '%s'", source, path.name)
                self.assertIn("local_size", source, path.name)
            self.assertNotRegex(source, r"ssh[\s\\\n]+[^\n]*>[^\n]*2>&1")
        self.assertIn("INPUT_ARTIFACT_SIZE", self.recovery_source)
        self.assertIn("size_in_bytes", self.recovery_source)

    def test_always_run_trusted_checkouts_have_resolver_barriers(self) -> None:
        document = self.trusted_document
        for job_name, job in document["jobs"].items():
            if not isinstance(job, dict) or "steps" not in job:
                continue
            has_always = "always()" in str(job.get("if", "")) or any(
                "always()" in str(step.get("if", ""))
                for step in job["steps"]
                if isinstance(step, dict)
            )
            if not has_always:
                continue
            for step in job["steps"]:
                if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/checkout@"):
                    condition = str(step.get("if", ""))
                    self.assertIn("needs.resolve-trusted-runner.result == 'success'", condition, job_name)
                    self.assertIn("trusted_runner_sha != ''", condition, job_name)
                    self.assertIn("trusted_runner_attested == 'true'", condition, job_name)


if __name__ == "__main__":
    unittest.main()
