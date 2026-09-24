from __future__ import annotations

import re
from pathlib import Path
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
        self.assertIn("needs:\n      - validate-external-inputs\n      - fixture-setup\n      - load-client", jobs["fixture-finalize"])
        self.assertIn("if: ${{ always()", jobs["fixture-finalize"])
        self.assertIn("if: ${{ always()", jobs["evaluate-load"])
        self.assertIn("needs.fixture-finalize.outputs.cleanup_status", jobs["evaluate-load"])
        self.assertIn("steps.cleanup.outputs.cleanup_status", jobs["fixture-finalize"])
        self.assertIn("Remove fixture-setup SSH material", jobs["fixture-setup"])
        self.assertIn("Remove finalizer SSH material", jobs["fixture-finalize"])

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
        self.assertIn('handoff_dir="$RUNNER_TEMP/live-launch-input"', jobs["live-sanity"])
        self.assertIn('rmdir -- "$handoff_dir"', jobs["live-sanity"])
        self.assertIn("trap cleanup_raw_report EXIT", jobs["live-sanity"])

    def test_candidate_jobs_never_receive_production_ssh_secrets(self) -> None:
        candidate_markers = (
            "platform_build_release.sh",
            "platform_load.py",
            "platform_live_launch_report.py",
        )
        for workflow_name in (
            "platform-production-deploy.yml",
            "platform-production-external-load.yml",
            "platform-live-launch.yml",
        ):
            jobs = _job_blocks(_workflow(workflow_name))
            for job_name, body in jobs.items():
                if any(marker in body for marker in candidate_markers):
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
                    self.assertNotIn("actions/checkout@", body, f"{workflow_name}:{job_name}")
                    self.assertNotIn("platform_host_tools_bundle.py", body, f"{workflow_name}:{job_name}")
                    self.assertNotIn("ref: ${{ env.TARGET_SHA }}", body, f"{workflow_name}:{job_name}")
                else:
                    self.assertNotIn("actions/checkout@", body, f"{workflow_name}:{job_name}")
                self.assertNotRegex(
                    body,
                    r"platform_(?:build_release|load|live_launch_report)\.py|platform_build_release\.sh",
                    f"candidate executable in {workflow_name}:{job_name}",
                )
                self.assertIn("environment: production", body, f"{workflow_name}:{job_name}")
                self.assertIn("platform_workflow_remote_dispatch.py", body)
                self.assertNotIn("bash -s --", body, f"{workflow_name}:{job_name}")
                self.assertNotRegex(body, r"\bssh\s+[^\n]*&\s*$")

    def test_remote_fixture_background_is_detached_at_trusted_boundary(self) -> None:
        dispatcher = DISPATCHER.read_text(encoding="utf-8")
        self.assertIn("start_new_session=True", dispatcher)
        self.assertIn("close_fds=True", dispatcher)
        self.assertIn('stdin=subprocess.DEVNULL', dispatcher)
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
        self.assertIn("preflight|deploy)", validation)
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
