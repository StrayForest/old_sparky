from __future__ import annotations

import re
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = REPO_ROOT / ".github" / "workflows"
DISPATCHER = REPO_ROOT / "platform" / "tools" / "platform_workflow_remote_dispatch.py"


def _job_blocks(source: str) -> dict[str, str]:
    """Return top-level job bodies without requiring a YAML dependency."""

    matches = list(
        re.finditer(
            r"^  (?P<name>[A-Za-z0-9_-]+):\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            source,
            re.MULTILINE | re.DOTALL,
        )
    )
    return {match.group("name"): match.group("body") for match in matches}


def _workflow(name: str) -> str:
    return (WORKFLOW_ROOT / name).read_text(encoding="utf-8")


class ProductionSecretJobIsolationTests(unittest.TestCase):
    def test_deploy_dag_places_build_and_production_consumers_on_distinct_jobs(self) -> None:
        source = _workflow("platform-production-deploy.yml")
        jobs = _job_blocks(source)
        self.assertIn("if: ${{ always() }}", jobs["validate-dispatch"])
        self.assertIn("      - validate-dispatch\n", jobs["build-release"])
        self.assertIn("      - validate-classifier\n", jobs["build-release"])
        self.assertIn("actions/checkout@", jobs["build-release"])
        self.assertIn("platform_build_release.sh", jobs["build-release"])
        self.assertIn("actions/upload-artifact@", jobs["build-release"])
        self.assertIn("artifact_id:", jobs["build-release"])
        self.assertNotRegex(jobs["build-release"], r"secrets\.PROD_SSH_")
        self.assertNotIn("environment: production", jobs["build-release"])
        self.assertIn("needs.host-capability-preflight.result == 'success'", jobs["preflight"])
        self.assertIn('/usr/bin/python3.12 -I -B "$HOST_TOOLS_DISPATCHER"', jobs["preflight"])
        self.assertNotIn("current/tools/platform_workflow_remote_dispatch.py", jobs["preflight"])
        self.assertIn("inputs.mode == 'deploy' || inputs.mode == 'preflight'", jobs["build-host-tools"])
        self.assertIn("inputs.mode == 'deploy' || inputs.mode == 'preflight'", jobs["host-capability-preflight"])
        self.assertIn("needs:\n      - validate-dispatch\n      - build-release", source)
        self.assertIn("needs.build-release.result == 'success'", jobs["production"])
        self.assertIn("path: trusted-classifier", jobs["production"])
        self.assertNotIn("ref: ${{ env.TARGET_SHA }}", jobs["production"])
        self.assertIn("actions/download-artifact@", jobs["production"])
        self.assertIn("PUBLISHED_ARTIFACT_ID", jobs["production"])
        self.assertNotIn("id-token: write", jobs["production"])

    def test_external_load_dag_propagates_setup_and_cleanup_barriers(self) -> None:
        source = _workflow("platform-production-external-load.yml")
        jobs = _job_blocks(source)
        self.assertIn("if: ${{ always() }}", jobs["validate-external-inputs"])
        self.assertIn("needs: validate-external-inputs", jobs["fixture-setup"])
        self.assertIn("environment: production", jobs["fixture-setup"])
        self.assertIn("manifest_ready:", jobs["fixture-setup"])
        self.assertIn("needs:\n      - validate-external-inputs\n      - fixture-setup", jobs["load-client"])
        self.assertIn("actions/checkout@", jobs["load-client"])
        self.assertNotRegex(jobs["load-client"], r"secrets\.PROD_SSH_")
        self.assertIn("needs:\n      - validate-external-inputs\n      - resolve-host-tools-pin\n      - fixture-setup\n      - load-client", jobs["fixture-finalize"])
        self.assertIn("if: ${{ always()", jobs["fixture-finalize"])
        self.assertIn("if: ${{ always()", jobs["evaluate-load"])
        self.assertIn("needs.fixture-finalize.outputs.cleanup_status", jobs["evaluate-load"])
        self.assertIn("steps.cleanup.outputs.cleanup_status", jobs["fixture-finalize"])
        self.assertIn("Remove fixture-setup SSH material", jobs["fixture-setup"])
        self.assertIn("Remove finalizer SSH material", jobs["fixture-finalize"])

        fixture_setup = jobs["fixture-setup"]
        finalizer = jobs["fixture-finalize"]
        self.assertIn("supervisor_exit_status:", fixture_setup)
        self.assertIn("pre_candidate_cleanup_allowed:", fixture_setup)
        self.assertIn("rb\"(?:0|[1-9][0-9]{0,2})\\n\"", fixture_setup)
        self.assertIn("if value > 255", fixture_setup)
        self.assertIn("needs.load-client.result == 'skipped'", finalizer)
        self.assertIn("needs['cpu-diagnostic-pair'].result == 'skipped'", finalizer)
        self.assertIn("needs.fixture-setup.outputs.pre_candidate_cleanup_allowed == '1'", finalizer)
        self.assertIn("needs.fixture-setup.outputs.manifest_ready != '1'", finalizer)
        self.assertIn("needs.fixture-setup.outputs.manifest_ready == '1'", finalizer)
        self.assertIn("needs.namespace-containment-barrier.outputs.namespace_closed_status == '0'", finalizer)
        self.assertIn("manual_required=1", jobs["namespace-containment-manual-barrier"])
        self.assertIn("external-cleanup-exports < \"$input_path\"", finalizer)
        self.assertIn("supervisor_status", fixture_setup)

        cleanup_conditions: list[str] = []
        finalizer_lines = finalizer.splitlines()
        for index, line in enumerate(finalizer_lines):
            if line.strip() != "if: >-":
                continue
            condition_lines: list[str] = []
            for following in finalizer_lines[index + 1:]:
                if not following.startswith("          "):
                    break
                condition_lines.append(following.strip())
            if condition_lines and condition_lines[0].startswith("always() &&"):
                cleanup_conditions.append(" ".join(condition_lines))
        self.assertEqual(len(cleanup_conditions), 3)
        self.assertEqual(len(set(cleanup_conditions)), 1)
        condition = cleanup_conditions[0]
        substitutions = {
            "needs['cpu-diagnostic-pair'].result == 'skipped'": "pair_skipped",
            "needs.namespace-containment-barrier.outputs.namespace_closed_status == '0'": "namespace_closed",
            "needs.namespace-containment-manual-barrier.outputs.manual_required == '0'": "manual_clear",
            "needs.fixture-setup.outputs.pre_candidate_cleanup_allowed == '1'": "cleanup_flag",
            "needs.fixture-setup.outputs.manifest_ready != '1'": "manifest_missing",
            "needs.load-client.result == 'skipped'": "load_skipped",
            "inputs.cpu_diagnostic_only == true": "cpu_only",
            "inputs.ssr_capacity_diagnostic_only == true": "ssr_only",
            "always()": "always_true",
        }
        for original, replacement in substitutions.items():
            condition = condition.replace(original, replacement)
        condition = condition.replace("&&", " and ").replace("||", " or ")

        def allows_cleanup(**overrides: bool) -> bool:
            values = {
                "always_true": True,
                "namespace_closed": False,
                "manual_clear": False,
                "cpu_only": True,
                "ssr_only": False,
                "cleanup_flag": True,
                "manifest_missing": True,
                "load_skipped": True,
                "pair_skipped": True,
                **overrides,
            }
            return bool(eval(condition, {"__builtins__": {}}, values))

        self.assertTrue(allows_cleanup())
        self.assertTrue(allows_cleanup(namespace_closed=True, manual_clear=True, cleanup_flag=False))
        for denied in (
            {"cleanup_flag": False},
            {"manifest_missing": False},
            {"load_skipped": False},
            {"pair_skipped": False},
            {"cpu_only": False, "ssr_only": False},
        ):
            with self.subTest(denied=denied):
                self.assertFalse(allows_cleanup(**denied))
        self.assertIn(
            "needs.fixture-setup.outputs.manifest_ready == '1'",
            finalizer[finalizer.index("id: external-finalize"):],
        )
        self.assertIn("namespace_closed_status == '0'", finalizer[finalizer.index("id: cleanup"):])

        parser_match = re.search(
            r'(?ms)^\s*supervisor_status="\$\(/usr/bin/python3 - "\$artifact_dir/supervisor\.exit\.download" <<\'PY\'\n(.*?)^\s*PY\n\s*\)"',
            fixture_setup,
        )
        self.assertIsNotNone(parser_match)
        parser_source = textwrap.dedent(parser_match.group(1))

        def parse_status(raw: bytes) -> subprocess.CompletedProcess[bytes]:
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                marker = directory / "supervisor.exit"
                marker.write_bytes(raw)
                marker.chmod(0o600)
                return subprocess.run(
                    [sys.executable, "-c", parser_source, str(marker)],
                    capture_output=True,
                    check=False,
                )

        for valid in (b"0\n", b"1\n", b"137\n", b"255\n"):
            with self.subTest(valid_status=valid):
                parsed = parse_status(valid)
                self.assertEqual(parsed.returncode, 0)
                self.assertEqual(parsed.stdout, valid)
                self.assertEqual(parsed.stderr, b"")
        for malformed in (b"00\n", b"+1\n", b"256\n", b"1", b"1\n\n", b"\xff\n"):
            with self.subTest(invalid_status=malformed):
                parsed = parse_status(malformed)
                self.assertEqual(parsed.returncode, 2)
                self.assertEqual(parsed.stdout, b"")
                self.assertEqual(parsed.stderr, b"")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            marker = directory / "supervisor.exit"
            marker.write_bytes(b"1\n")
            marker.chmod(0o600)
            hardlink = directory / "linked"
            os.link(marker, hardlink)
            linked = subprocess.run(
                [sys.executable, "-c", parser_source, str(marker)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(linked.returncode, 2)
            self.assertEqual(linked.stdout, b"")

    def test_live_launch_sanitizer_is_a_fresh_secret_free_failure_barrier(self) -> None:
        source = _workflow("platform-live-launch.yml")
        jobs = _job_blocks(source)
        self.assertIn("if: ${{ always() }}", jobs["validate-live-inputs"])
        self.assertIn("needs: validate-live-inputs", jobs["live-sanity"])
        self.assertIn("environment: production", jobs["live-sanity"])
        self.assertIn("needs: live-sanity", jobs["sanitize-live-report"])
        self.assertIn("if: ${{ always() }}", jobs["sanitize-live-report"])
        self.assertNotIn("actions/checkout@", jobs["sanitize-live-report"])
        self.assertIn("platform-live-launch-sanitized-", jobs["live-sanity"])
        self.assertNotIn("platform-live-launch-raw-", jobs["live-sanity"])
        self.assertNotRegex(jobs["sanitize-live-report"], r"secrets\.PROD_SSH_")
        self.assertIn("download", jobs["sanitize-live-report"].lower())
        self.assertIn("steps.cleanup_ssh.outcome == 'success'", jobs["live-sanity"])
        self.assertIn('input_path="$RUNNER_TEMP/live-launch-input.json"', jobs["live-sanity"])
        self.assertIn('rm -f -- "$input_path"', jobs["live-sanity"])
        self.assertIn('test ! -e "$input_path"', jobs["live-sanity"])
        self.assertNotIn("actions/checkout@", jobs["live-sanity"])
        self.assertIn("HANDOFF_ARTIFACT_ID", jobs["live-sanity"])
        self.assertNotIn("artifact-digest", jobs["live-sanity"])
        self.assertIn('metadata.get("digest")', jobs["live-sanity"])
        self.assertIn("MAX_ARCHIVE_BYTES", jobs["live-sanity"])
        self.assertIn("ArtifactRedirect", jobs["live-sanity"])
        self.assertIn("trap cleanup_raw_report EXIT", jobs["live-sanity"])
        self.assertIn("contents: none", jobs["live-sanity"])
        self.assertIn("contents: read", jobs["validate-live-inputs"])

    def test_candidate_jobs_never_receive_production_ssh_secrets(self) -> None:
        candidate_markers = (
            "platform_build_release.sh",
            "platform_load.py",
            "platform_live_launch_report.py",
            "platform_cpu_diagnostic_pair.py",
        )
        for workflow_name in (
            "platform-production-deploy.yml",
            "platform-production-external-load.yml",
            "platform-live-launch.yml",
        ):
            jobs = _job_blocks(_workflow(workflow_name))
            for job_name, body in jobs.items():
                if any(marker in body for marker in candidate_markers):
                    if job_name == "cpu-diagnostic-pair":
                        self.assertIn("ref: ${{ env.TARGET_SHA }}", body)
                        self.assertIn("test \"$(git rev-parse HEAD)\" = \"$TARGET_SHA\"", body)
                        self.assertIn("needs:\n      - validate-external-inputs", body)
                        continue
                    self.assertNotRegex(
                        body,
                        r"(?:secrets\.PROD_SSH_|PROD_SSH_(?:HOST|USER|KEY):)",
                        f"candidate code and deployment credentials share {workflow_name}:{job_name}",
                    )

    def test_secret_jobs_are_fresh_fixed_dispatch_boundaries(self) -> None:
        for workflow_name in (
            "platform-production-deploy.yml",
            "platform-production-external-load.yml",
            "platform-live-launch.yml",
        ):
            jobs = _job_blocks(_workflow(workflow_name))
            secret_jobs = {
                name: body
                for name, body in jobs.items()
                if "secrets.PROD_SSH_KEY" in body
            }
            self.assertTrue(secret_jobs, workflow_name)
            for job_name, body in secret_jobs.items():
                if job_name == "production":
                    self.assertIn("actions/checkout@", body, f"{workflow_name}:{job_name}")
                    self.assertIn("path: trusted-classifier", body, f"{workflow_name}:{job_name}")
                    self.assertNotIn("ref: ${{ env.TARGET_SHA }}", body, f"{workflow_name}:{job_name}")
                elif job_name == "host-capability-preflight":
                    self.assertIn("actions/checkout@", body, f"{workflow_name}:{job_name}")
                    self.assertIn("ref: ${{ env.TARGET_SHA }}", body, f"{workflow_name}:{job_name}")
                    self.assertIn("fetch-depth: 1", body, f"{workflow_name}:{job_name}")
                    self.assertIn("persist-credentials: false", body, f"{workflow_name}:{job_name}")
                    self.assertIn("path: target-source", body, f"{workflow_name}:{job_name}")
                    self.assertIn("needs.validate-dispatch.result == 'success'", body)
                    self.assertLess(
                        body.index("Rebind outer artifact and attest exact inner bundle before extraction"),
                        body.index("Validate root SSH identity and installed generation"),
                    )
                    self.assertIn(
                        '"$GITHUB_WORKSPACE/target-source/platform/tools/platform_host_tools_installed_check.py"',
                        body,
                    )
                    self.assertNotIn("platform_host_tools_bundle.py", body, f"{workflow_name}:{job_name}")
                elif job_name == "cpu-diagnostic-pair":
                    self.assertIn("ref: ${{ env.TARGET_SHA }}", body)
                    self.assertIn("test \"$(git rev-parse HEAD)\" = \"$TARGET_SHA\"", body)
                    self.assertIn("platform_cpu_diagnostic_pair.py parent", body)
                    self.assertIn("needs:\n      - validate-external-inputs", body)
                    pair_source = (
                        REPO_ROOT / "platform" / "tools" / "platform_cpu_diagnostic_pair.py"
                    ).read_text(encoding="utf-8")
                    self.assertIn(
                        "/opt/oldsparky/platform/current/tools/platform_workflow_remote_dispatch.py",
                        pair_source,
                    )
                    self.assertIn('"cpu-diagnostic-plan"', pair_source)
                    self.assertIn("clear_worker_environment=True", pair_source)
                    self.assertIn("_hide_ssh_material(", pair_source)
                else:
                    self.assertNotIn("actions/checkout@", body, f"{workflow_name}:{job_name}")
                self.assertNotRegex(
                    body,
                    r"platform_(?:build_release|load|live_launch_report)\.py|platform_build_release\.sh",
                    f"candidate executable in {workflow_name}:{job_name}",
                )
                self.assertIn("environment: production", body, f"{workflow_name}:{job_name}")
                if job_name != "cpu-diagnostic-pair":
                    self.assertIn("platform_workflow_remote_dispatch.py", body)
                self.assertNotIn("bash -s --", body, f"{workflow_name}:{job_name}")
                self.assertNotRegex(body, r"\bssh\s+[^\n]*&\s*$")

    def test_remote_fixture_background_is_detached_at_trusted_boundary(self) -> None:
        dispatcher = DISPATCHER.read_text(encoding="utf-8")
        self.assertIn("start_new_session=True", dispatcher)
        self.assertIn("close_fds=True", dispatcher)
        self.assertIn('stdin=subprocess.PIPE', dispatcher)
        self.assertIn('_control_email_stdin(payload["control_email"])', dispatcher)
        external = _workflow("platform-production-external-load.yml")
        self.assertIn("external-fixture < \"$input_path\"", external)
        self.assertNotIn("external-fixture < \"$input_path\" &", external)
        self.assertNotIn("ssh \"$PROD_SSH_USER@$PROD_SSH_HOST\" bash", external)

    def test_deploy_validation_is_always_running_and_precedes_mode_branches(self) -> None:
        source = _workflow("platform-production-deploy.yml")
        jobs = _job_blocks(source)
        validation = jobs["validate-dispatch"]
        self.assertIn("if: ${{ always() }}", validation)
        self.assertIn('case "$DEPLOY_MODE" in', validation)
        self.assertIn("preflight|deploy|baseline-reconcile|recovery-deploy)", validation)
        self.assertIn("platform_workflow_input_guard.py deployment", validation)
        for argument in (
            "--classifier-run-id",
            "--classifier-run-attempt",
            "--web-compression",
        ):
            self.assertIn(argument, validation)
        self.assertIn("needs: validate-dispatch", source)
        for mode in ("inputs.mode == 'preflight'", "inputs.mode == 'deploy'"):
            self.assertIn(mode, source)
        self.assertLess(
            source.index("case \"$DEPLOY_MODE\" in"),
            source.index("inputs.mode == 'preflight'"),
        )
        self.assertLess(
            source.index("case \"$DEPLOY_MODE\" in"),
            source.index("inputs.mode == 'deploy'"),
        )
        self.assertIn("needs.validate-dispatch.result == 'success'", source)

    def test_secret_consumers_revalidate_closed_handoffs_and_exact_sha(self) -> None:
        deploy = _workflow("platform-production-deploy.yml")
        external = _workflow("platform-production-external-load.yml")
        live = _workflow("platform-live-launch.yml")
        for source in (deploy, external, live):
            self.assertIn("exact target SHA", source)
            self.assertIn("set(payload)", source)
        self.assertIn("RELEASE.provenance.json", deploy)
        self.assertIn("PUBLISHED_ARTIFACT_DIGEST", deploy)
        self.assertIn("sha256sum -c", deploy)
        self.assertIn("classifier-manifest.json", deploy)
        self.assertIn("digest", deploy)


if __name__ == "__main__":
    unittest.main()
