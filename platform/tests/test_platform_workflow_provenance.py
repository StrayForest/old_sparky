from __future__ import annotations

import copy
import ast
from datetime import datetime, timezone
import importlib
import inspect
import json
import os
import re
import sys
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


TOOLS = Path(__file__).resolve().parents[1] / "tools"
WORKFLOW_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
STATUS_FINALIZER_WORKFLOW = WORKFLOW_DIR / "platform-security-status-finalizer.yml"
sys.path.insert(0, str(TOOLS))

from tools.platform_workflow_provenance import (  # noqa: E402
    AUTODEPLOY_DISPATCH_STEP_NAME,
    AUTODEPLOY_WORKFLOW_NAME,
    AUTODEPLOY_WORKFLOW_PATH,
    DEPLOY_WORKFLOW_NAME,
    DEPLOY_WORKFLOW_PATH,
    DEPLOY_STATUS_CONTEXT,
    SECURITY_STATUS_CONTEXT,
    SECURITY_SUCCESS_DESCRIPTION,
    RECOVERY_REQUIRED_SECURITY_JOBS,
    ProvenanceError,
    _payload_rows,
    deployment_snapshot_digest,
    latest_context_status,
    parse_status_timestamp,
    validate_deployment_event,
    collect_github_api_pages,
    validate_deployment_marker,
    validate_failed_report_deployment,
    validate_report_only_remote_diagnostic,
    validate_recovery_source_security_gates,
    validate_autodeploy_dispatch,
    SECURITY_WORKFLOW_NAME,
    SECURITY_WORKFLOW_PATH,
)
from tools.platform_deploy_baseline import (  # noqa: E402
    BASELINE_RUNTIME_GATE_NAMES,
    REPORT_ONLY_RECOVERY_CONFIRMATION,
    active_deployment_status_identity,
    classify_cumulative_baseline,
    validate_active_baseline,
    validate_baseline_runtime_proof,
    validate_report_only_recovery_baseline,
    wait_for_autodeploy_completion,
)
from tools.platform_ci_classifier import (  # noqa: E402
    CANDIDATE_PACKAGING_FILES,
    CANDIDATE_PACKAGING_REASON,
    RECOVERY_BOOTSTRAP_FILES,
    RECOVERY_BOOTSTRAP_REASON,
    classify,
    manifest_digest,
)


class WorkflowProvenanceTests(unittest.TestCase):
    def test_bounded_github_pagination_accepts_arrays_and_complete_objects_only(self) -> None:
        statuses = [{"id": 1}, {"id": 2}]
        self.assertEqual(
            collect_github_api_pages(
                lambda page: statuses if page == 1 else [],
                collection_key="statuses",
            ),
            statuses,
        )

        jobs = [{"id": index} for index in range(100)]
        self.assertEqual(
            collect_github_api_pages(
                lambda page: {"total_count": 101, "jobs": jobs}
                if page == 1
                else {"total_count": 101, "jobs": [{"id": 100}]},
                collection_key="jobs",
            ),
            jobs + [{"id": 100}],
        )
        artifacts = [{"id": 7}]
        self.assertEqual(
            collect_github_api_pages(
                lambda _page: {"total_count": 1, "artifacts": artifacts},
                collection_key="artifacts",
            ),
            artifacts,
        )

        invalid_pages = (
            lambda page: {"total_count": 2, "jobs": [{"id": 1}]} if page == 1 else {"total_count": 2, "jobs": []},
            lambda page: {"total_count": 101, "jobs": jobs}
            if page == 1
            else {"total_count": 102, "jobs": [{"id": 100}]},
            lambda _page: {"jobs": []},
            lambda _page: {"total_count": 1, "jobs": [None]},
            lambda _page: {"total_count": 1, "jobs": []},
        )
        for fetch_page in invalid_pages:
            with self.subTest(fetch_page=fetch_page), self.assertRaises(ProvenanceError):
                collect_github_api_pages(fetch_page, collection_key="jobs")

        with self.assertRaises(ProvenanceError):
            collect_github_api_pages(
                lambda _page: {"total_count": 0, "jobs": []},
                collection_key="jobs",
                page_limit=0,
            )

    SHA = "a" * 40

    def _run_finalizer_dispatch_wait(
        self,
        *,
        attempt_state: str,
        latest_attempt: int = 1,
        timeout_probe: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
        workflow = yaml.safe_load(STATUS_FINALIZER_WORKFLOW.read_text(encoding="utf-8"))
        shell = workflow["jobs"]["finalize-status"]["steps"][0]["run"]
        finalizer_shell = shell
        if timeout_probe:
            finalizer_shell = finalizer_shell.replace("SECONDS + 180", "SECONDS + 1")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/python3
                    import json
                    import os
                    from pathlib import Path
                    import sys

                    args = sys.argv[1:]
                    output = Path(args[args.index("--output") + 1])
                    url = next(value for value in args if value.startswith("https://"))
                    with open(os.environ["REQUEST_LOG"], "a", encoding="utf-8") as log:
                        log.write(url + "\\n")
                    if "/attempts/" in url:
                        counter = Path(os.environ["ATTEMPT_COUNTER"])
                        calls = int(counter.read_text() or "0") if counter.exists() else 0
                        counter.write_text(str(calls + 1), encoding="ascii")
                        if os.environ["ATTEMPT_STATE"] == "timeout":
                            result = {"status": "in_progress", "conclusion": None}
                        elif os.environ["ATTEMPT_STATE"] == "failed":
                            result = dict(json.loads(Path(os.environ["ATTEMPT_RUN"]).read_text()))
                            result.update({"status": "completed", "conclusion": "failure"})
                        elif calls == 0:
                            result = {"status": "in_progress", "conclusion": None}
                        else:
                            result = dict(json.loads(Path(os.environ["ATTEMPT_RUN"]).read_text()))
                    elif url.endswith("/actions/runs/12345"):
                        result = json.loads(Path(os.environ["LATEST_RUN"]).read_text())
                    elif url.endswith("/actions/workflows/77"):
                        result = {
                            "id": 77,
                            "path": ".github/workflows/platform-security.yml",
                            "name": "Platform security and build",
                        }
                    else:
                        raise SystemExit("unexpected API endpoint")
                    output.write_text(json.dumps(result), encoding="utf-8")
                    """
                ),
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            fake_sleep = fake_bin / "sleep"
            fake_sleep.write_text("#!/bin/sh\n/bin/sleep 0.05\n", encoding="utf-8")
            fake_sleep.chmod(0o755)
            marker = f"platform-baseline-runtime-v1:{self.SHA}:s111.1:a222.1:d333.1:r12345.1"
            attempt_metadata = {
                "id": 12345,
                "workflow_id": 77,
                "name": marker,
                "display_title": marker,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
                "head_sha": self.SHA,
                "head_branch": "dev",
                "event": "workflow_dispatch",
                "path": ".github/workflows/platform-security.yml",
                "html_url": "https://github.com/StrayForest/old_sparky/actions/runs/12345",
                "repository": {"full_name": "StrayForest/old_sparky"},
                "actor": {"login": "github-actions[bot]"},
            }
            attempt_path = root / "attempt.json"
            attempt_path.write_text(json.dumps(attempt_metadata), encoding="utf-8")
            latest_path = root / "latest.json"
            latest_run = dict(attempt_metadata)
            latest_run["run_attempt"] = latest_attempt
            latest_path.write_text(json.dumps(latest_run), encoding="utf-8")
            log_path = root / "requests.txt"
            counter_path = root / "attempt-count.txt"
            runner_temp = root / "runner"
            runner_temp.mkdir()
            output_path = root / "github-output"
            output_path.touch()
            required_jobs = [
                "Authenticate internal baseline runtime proof",
                "Backend DB-free contours",
                "Backend PostgreSQL and Redis integration",
                "Backend privileged ephemeral contour",
                "Backend aggregate",
                "Python quality",
                "Security gates",
                "web-quality",
                "Web hermetic",
                "Documentation consistency",
                "Migration scenarios",
                "Verification contract",
                "Conditional release runtime fixture",
                "Trusted dev immutable release runtime",
                "status-start",
                "status-final",
                "Dispatch exact baseline proof finalizer",
            ]
            jobs_path = root / "jobs.json"
            jobs_path.write_text(
                json.dumps(
                    {
                        "total_count": len(required_jobs),
                        "jobs": [
                            {"name": name, "status": "completed", "conclusion": "success"}
                            for name in required_jobs
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "sitecustomize.py").write_text(
                textwrap.dedent(
                    """\
                    import os
                    from pathlib import Path
                    import urllib.request

                    class Response:
                        def __init__(self, url, body):
                            self.url = url
                            self.body = body
                        def __enter__(self):
                            return self
                        def __exit__(self, *args):
                            return False
                        def read(self, size=-1):
                            return self.body if size < 0 else self.body[:size]
                        def geturl(self):
                            return self.url

                    class Opener:
                        def open(self, request, timeout=20):
                            url = request.full_url
                            if not url.endswith("/jobs?per_page=100"):
                                raise RuntimeError("unexpected API endpoint")
                            return Response(url, Path(os.environ["JOBS_PAYLOAD"]).read_bytes())

                    urllib.request.build_opener = lambda *args: Opener()
                    """
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "RUNNER_TEMP": str(runner_temp),
                    "GITHUB_OUTPUT": str(output_path),
                    "GITHUB_SERVER_URL": "https://github.com",
                    "GITHUB_REPOSITORY": "StrayForest/old_sparky",
                    "GITHUB_API_URL": "https://api.github.com",
                    "GITHUB_REF": "refs/heads/dev",
                    "GH_TOKEN": "test-token",
                    "FINALIZER_EVENT_NAME": "workflow_dispatch",
                    "FINALIZER_ACTOR": "github-actions[bot]",
                    "SOURCE_RUN_ID": "12345",
                    "SOURCE_RUN_ATTEMPT": "1",
                    "SOURCE_WORKFLOW_ID": "",
                    "ATTEMPT_STATE": attempt_state,
                    "ATTEMPT_COUNTER": str(counter_path),
                    "ATTEMPT_RUN": str(attempt_path),
                    "LATEST_RUN": str(latest_path),
                    "REQUEST_LOG": str(log_path),
                    "JOBS_PAYLOAD": str(jobs_path),
                    "PYTHONPATH": str(root),
                }
            )
            environment.pop("BASH_ENV", None)
            environment.pop("ENV", None)
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", finalizer_shell],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=5,
            )
            requests = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
            return result, requests, output_path.read_text(encoding="utf-8")

    def test_baseline_finalizer_waits_for_exact_attempt_and_rejects_failure_drift_or_timeout(self) -> None:
        succeeded, requests, output = self._run_finalizer_dispatch_wait(
            attempt_state="pending-then-success",
        )
        self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
        self.assertEqual(
            requests,
            [
                "https://api.github.com/repos/StrayForest/old_sparky/actions/runs/12345/attempts/1",
                "https://api.github.com/repos/StrayForest/old_sparky/actions/runs/12345/attempts/1",
                "https://api.github.com/repos/StrayForest/old_sparky/actions/runs/12345",
                "https://api.github.com/repos/StrayForest/old_sparky/actions/workflows/77",
            ],
        )
        self.assertIn("context=platform-baseline-runtime", output)
        self.assertIn(f"target_sha={self.SHA}", output)
        self.assertIn("publish=true", output)
        self.assertIn("https://github.com/StrayForest/old_sparky/actions/runs/12345/attempts/1", output)

        failed, failure_requests, _ = self._run_finalizer_dispatch_wait(
            attempt_state="failed",
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("did not complete successfully", failed.stderr)
        self.assertEqual(len(failure_requests), 1)

        drifted, drift_requests, _ = self._run_finalizer_dispatch_wait(
            attempt_state="pending-then-success",
            latest_attempt=2,
        )
        self.assertNotEqual(drifted.returncode, 0)
        self.assertIn("not the latest successful exact attempt", drifted.stderr)
        self.assertEqual(
            drift_requests,
            [
                "https://api.github.com/repos/StrayForest/old_sparky/actions/runs/12345/attempts/1",
                "https://api.github.com/repos/StrayForest/old_sparky/actions/runs/12345/attempts/1",
                "https://api.github.com/repos/StrayForest/old_sparky/actions/runs/12345",
                "https://api.github.com/repos/StrayForest/old_sparky/actions/workflows/77",
            ],
        )
        timeout, timeout_requests, _ = self._run_finalizer_dispatch_wait(
            attempt_state="timeout",
            timeout_probe=True,
        )
        self.assertNotEqual(timeout.returncode, 0)
        self.assertIn("Timed out waiting for the exact baseline proof attempt", timeout.stderr)
        self.assertGreaterEqual(len(timeout_requests), 1)
        self.assertTrue(all("/attempts/1" in request for request in timeout_requests))

    def _run_finalizer_terminal_post(self, *, latest_attempt: int) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        workflow = yaml.safe_load(STATUS_FINALIZER_WORKFLOW.read_text(encoding="utf-8"))
        shell = workflow["jobs"]["finalize-status"]["steps"][1]["run"]
        marker = f"platform-baseline-runtime-v1:{self.SHA}:s111.1:a222.1:d333.1:r12345.1"
        latest_run = {
            "id": 12345,
            "run_attempt": latest_attempt,
            "status": "completed",
            "conclusion": "success",
            "event": "workflow_dispatch",
            "head_branch": "dev",
            "head_sha": self.SHA,
            "path": ".github/workflows/platform-security.yml",
            "actor": {"login": "github-actions[bot]"},
            "display_title": marker,
            "name": marker,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            request_log = root / "requests.txt"
            latest_path = root / "latest.json"
            latest_path.write_text(json.dumps(latest_run), encoding="utf-8")
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/python3
                    import json
                    import os
                    from pathlib import Path
                    import sys

                    args = sys.argv[1:]
                    url = next(value for value in args if value.startswith("https://"))
                    method = args[args.index("--request") + 1] if "--request" in args else "GET"
                    with open(os.environ["REQUEST_LOG"], "a", encoding="utf-8") as log:
                        log.write(method + " " + url + "\\n")
                    if url.endswith("/actions/runs/12345") and "--output" in args:
                        Path(args[args.index("--output") + 1]).write_text(
                            Path(os.environ["LATEST_RUN"]).read_text(), encoding="utf-8"
                        )
                    elif method == "POST" and "/statuses/" in url:
                        print("{}")
                    else:
                        raise SystemExit("unexpected endpoint or method")
                    """
                ),
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            runner_temp = root / "runner"
            runner_temp.mkdir()
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "RUNNER_TEMP": str(runner_temp),
                    "GITHUB_API_URL": "https://api.github.com",
                    "GITHUB_REPOSITORY": "StrayForest/old_sparky",
                    "GH_TOKEN": "test-token",
                    "FINALIZER_EVENT_NAME": "workflow_dispatch",
                    "PROOF_RUN_ID": "12345",
                    "PROOF_RUN_ATTEMPT": "1",
                    "PROOF_TARGET_SHA": self.SHA,
                    "TARGET_SHA": self.SHA,
                    "STATUS_CONTEXT": "platform-baseline-runtime",
                    "STATUS_PAYLOAD": json.dumps(
                        {
                            "state": "success",
                            "description": "Platform security and build passed",
                            "target_url": (
                                "https://github.com/StrayForest/old_sparky/actions/runs/"
                                "12345/attempts/1"
                            ),
                        }
                    ),
                    "LATEST_RUN": str(latest_path),
                    "REQUEST_LOG": str(request_log),
                }
            )
            environment.pop("BASH_ENV", None)
            environment.pop("ENV", None)
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", shell],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=5,
            )
            requests = request_log.read_text(encoding="utf-8").splitlines() if request_log.exists() else []
            return result, requests

    def test_baseline_finalizer_late_recheck_blocks_post_when_attempt_changes(self) -> None:
        success, success_requests = self._run_finalizer_terminal_post(latest_attempt=1)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(
            success_requests,
            [
                "GET https://api.github.com/repos/StrayForest/old_sparky/actions/runs/12345",
                f"POST https://api.github.com/repos/StrayForest/old_sparky/statuses/{self.SHA}",
            ],
        )

        changed, changed_requests = self._run_finalizer_terminal_post(latest_attempt=2)
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("no longer the exact latest successful attempt", changed.stderr)
        self.assertEqual(
            changed_requests,
            ["GET https://api.github.com/repos/StrayForest/old_sparky/actions/runs/12345"],
        )

    def _payload(self) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
        run_id = 1234
        attempt = 2
        base = f"https://github.com/StrayForest/old_sparky/actions/runs/{run_id}"
        workflow = {
            "id": 77,
            "path": DEPLOY_WORKFLOW_PATH,
            "name": DEPLOY_WORKFLOW_NAME,
        }
        run = {
            "id": run_id,
            "workflow_id": 77,
            "name": DEPLOY_WORKFLOW_NAME,
            "path": DEPLOY_WORKFLOW_PATH,
            "run_attempt": attempt,
            "event": "workflow_dispatch",
            "head_branch": "dev",
            "head_sha": self.SHA,
            "status": "completed",
            "conclusion": "success",
            "html_url": base,
            "repository": {
                "full_name": "StrayForest/old_sparky",
                "name": "old_sparky",
                "owner": {"login": "StrayForest"},
            },
        }
        jobs = [
            {
                "id": 9001,
                "name": "Deploy production",
                "status": "completed",
                "conclusion": "success",
            }
        ]
        statuses = [
            {
                "id": 9100,
                "context": DEPLOY_STATUS_CONTEXT,
                "state": "success",
                "description": "Production deployment and live smoke passed",
                "target_url": f"{base}/attempts/{attempt}",
                "updated_at": "2026-09-19T10:00:00Z",
                "creator": {
                    "login": "github-actions[bot]",
                    "type": "Bot",
                    "id": 41898282,
                },
            }
        ]
        return workflow, run, jobs, statuses

    def _baseline(self, source_sha: str | None = None) -> dict[str, object]:
        return {
            "schema": 1,
            "source_sha": source_sha or self.SHA,
            "release_slug": "gha-35511236041-1-20260920T123932Z",
            "release_json_sha256": "b" * 64,
            "current_link_dev": 253,
            "current_link_ino": 910001,
            "release_dev": 253,
            "release_ino": 910002,
            "pending_operation": False,
        }

    def _baseline_runtime_payload(self):
        run_id, attempt = 1234, 2
        source_id, source_attempt = 567, 1
        auto_id, auto_attempt = 678, 1
        parent_id, parent_attempt = 789, 3
        workflow = {
            "id": 77,
            "path": SECURITY_WORKFLOW_PATH,
            "name": SECURITY_WORKFLOW_NAME,
        }
        base = f"https://github.com/StrayForest/old_sparky/actions/runs/{run_id}"
        title = (
            f"platform-baseline-runtime-v1:{self.SHA}:"
            f"s{source_id}.{source_attempt}:a{auto_id}.{auto_attempt}:"
            f"d{parent_id}.{parent_attempt}:r{run_id}.{attempt}"
        )
        run = {
            "id": run_id,
            "workflow_id": 77,
            "name": title,
            "path": SECURITY_WORKFLOW_PATH,
            "display_title": title,
            "run_attempt": attempt,
            "event": "workflow_dispatch",
            "head_branch": "dev",
            "head_sha": self.SHA,
            "status": "completed",
            "conclusion": "success",
            "html_url": base,
            "repository": {
                "full_name": "StrayForest/old_sparky",
                "name": "old_sparky",
                "owner": {"login": "StrayForest"},
            },
        }
        jobs = [
            {
                "id": 10000 + index,
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for index, name in enumerate(sorted(BASELINE_RUNTIME_GATE_NAMES))
        ]
        jobs.extend(
            [
                {"id": 10100, "name": "Classifier", "status": "completed", "conclusion": "success"},
                {"id": 10101, "name": "Baseline runtime guard", "status": "completed", "conclusion": "success"},
            ]
        )
        statuses = [
            {
                "id": 20001,
                "context": "platform-baseline-runtime",
                "state": "success",
                "description": "Platform security and build passed",
                "target_url": f"{base}/attempts/{attempt}",
                "updated_at": "2026-10-02T12:00:00Z",
                "created_at": "2026-10-02T12:00:00Z",
                "creator": {
                    "login": "github-actions[bot]",
                    "type": "Bot",
                    "id": 41898282,
                },
            }
        ]
        receipt = {
            "schema": 1,
            "proof_mode": "baseline-runtime",
            "target_sha": self.SHA,
            "source_security_run_id": str(source_id),
            "source_security_run_attempt": str(source_attempt),
            "autodeploy_run_id": str(auto_id),
            "autodeploy_run_attempt": str(auto_attempt),
            "production_deploy_run_id": str(parent_id),
            "production_deploy_run_attempt": str(parent_attempt),
            "proof_run_id": str(run_id),
            "proof_run_attempt": str(attempt),
            "required_gates": [
                "backend", "python-quality", "security", "migration", "docs",
                "web-quality", "web-hermetic", "verification-contract",
                "release-runtime", "release-runtime-real",
            ],
        }
        correlation = {
            "source_security_run_id": source_id,
            "source_security_attempt": source_attempt,
            "autodeploy_run_id": auto_id,
            "autodeploy_attempt": auto_attempt,
            "production_deploy_run_id": parent_id,
            "production_deploy_attempt": parent_attempt,
        }
        return workflow, run, jobs, statuses, receipt, correlation

    def _validate_baseline_runtime_payload(self, payload):
        workflow, run, jobs, statuses, receipt, correlation = payload
        return validate_baseline_runtime_proof(
            workflow,
            run,
            jobs,
            statuses,
            receipt,
            expected_target_sha=self.SHA,
            **correlation,
            jobs_complete=True,
            statuses_complete=True,
            now=datetime(2026, 10, 3, tzinfo=timezone.utc),
        )

    def test_cumulative_baseline_classification_rederives_runtime_sensitivity(self) -> None:
        incremental = classify(
            [".github/workflows/platform-production-deploy.yml"],
            event="push",
            branch="dev",
            target_sha=self.SHA,
        )
        self.assertTrue(incremental["runtime_sensitive"])
        self.assertFalse(incremental["deployable"])

        cumulative = classify_cumulative_baseline(
            incremental,
            [
                ".github/workflows/platform-production-deploy.yml",
                "platform/tools/platform_build_release.sh",
            ],
            expected_target_sha=self.SHA,
        )
        self.assertFalse(cumulative["no_op"])
        manifest = cumulative["manifest"]
        self.assertTrue(manifest["runtime_sensitive"])
        self.assertTrue(manifest["deployable"])
        self.assertEqual(manifest["class"], "full")
        self.assertFalse(manifest["fallback"])

    def test_report_only_recovery_baseline_binds_exact_failed_release_and_closed_route(self) -> None:
        failed_sha = "a" * 40
        target_sha = "b" * 40
        baseline = {
            "schema": 1,
            "source_sha": failed_sha,
            "release_slug": f"gha-1234-2-{failed_sha[:12]}",
            "release_json_sha256": "c" * 64,
            "current_link_dev": 253,
            "current_link_ino": 910001,
            "release_dev": 253,
            "release_ino": 910002,
            "pending_operation": False,
        }
        changed_paths = [
            ".github/workflows/platform-production-deploy.yml",
            "platform/tools/platform_deploy_baseline.py",
            "platform/tools/platform_workflow_provenance.py",
            "platform/docs/deployment-runbook.md",
        ]
        incremental = classify(
            changed_paths,
            event="push",
            branch="dev",
            target_sha=target_sha,
        )
        manifest = classify_cumulative_baseline(
            incremental,
            changed_paths,
            expected_target_sha=target_sha,
        )["manifest"]
        self.assertEqual(manifest["class"], "full")
        self.assertTrue(manifest["runtime_sensitive"])
        self.assertFalse(manifest["deployable"])
        args = {
            "failed_run_id": 1234,
            "failed_run_attempt": 2,
            "failed_source_sha": failed_sha,
            "failed_release_json_sha256": "c" * 64,
            "target_sha": target_sha,
            "current_dev_sha": target_sha,
            "first_parent_shas": [target_sha, failed_sha],
            "confirmation": REPORT_ONLY_RECOVERY_CONFIRMATION,
        }
        result = validate_report_only_recovery_baseline(baseline, manifest, **args)
        self.assertEqual(result["failed_run_id"], 1234)

        invalid_baselines = (
            {**baseline, "source_sha": "d" * 40},
            {**baseline, "release_slug": "gha-1234-2-dddddddddddd"},
            {**baseline, "release_json_sha256": "d" * 64},
            {**baseline, "pending_operation": True},
        )
        for invalid in invalid_baselines:
            with self.subTest(baseline=invalid), self.assertRaises(ProvenanceError):
                validate_report_only_recovery_baseline(invalid, manifest, **args)

        bad_cases = (
            {**args, "confirmation": "operator says so"},
            {**args, "current_dev_sha": "e" * 40},
            {**args, "target_sha": failed_sha, "current_dev_sha": failed_sha},
            {**args, "first_parent_shas": [target_sha, "d" * 40]},
            {**args, "first_parent_shas": [target_sha, failed_sha, failed_sha]},
        )
        for invalid in bad_cases:
            with self.subTest(arguments=invalid), self.assertRaises(ProvenanceError):
                validate_report_only_recovery_baseline(baseline, manifest, **invalid)

        for field, value in (
            ("schema", 99),
            ("target_sha", "d" * 40),
            ("runtime_sensitive", False),
            ("deployable", True),
            ("fallback", True),
            ("class", "docs-only"),
            ("reason", "another route"),
        ):
            invalid_manifest = {**manifest, field: value}
            with self.subTest(manifest_field=field), self.assertRaises(ProvenanceError):
                validate_report_only_recovery_baseline(baseline, invalid_manifest, **args)
        invalid_manifest = {
            **manifest,
            "files": manifest["files"] + ["platform/apps/platform_api/main.py"],
        }
        with self.assertRaises(ProvenanceError):
            validate_report_only_recovery_baseline(baseline, invalid_manifest, **args)

        hidden_app_incremental = classify(
            [".github/workflows/platform-production-deploy.yml"],
            event="push",
            branch="dev",
            target_sha=target_sha,
        )
        hidden_app_route = classify_cumulative_baseline(
            hidden_app_incremental,
            [
                ".github/workflows/platform-production-deploy.yml",
                "platform/apps/platform_api/main.py",
            ],
            expected_target_sha=target_sha,
        )["manifest"]
        with self.assertRaises(ProvenanceError):
            validate_report_only_recovery_baseline(baseline, hidden_app_route, **args)

    def test_cumulative_baseline_rejects_incomplete_or_non_deployable_paths(self) -> None:
        incremental_path = ".github/workflows/platform-production-deploy.yml"
        incremental = classify(
            [incremental_path], event="push", branch="dev", target_sha=self.SHA
        )
        cases = (
            ([], "missing path list"),
            (["platform/tools/platform_build_release.sh"], "omitted incremental path"),
        )
        for paths, label in cases:
            with self.subTest(label=label), self.assertRaises(ProvenanceError):
                classify_cumulative_baseline(
                    incremental, paths, expected_target_sha=self.SHA
                )

        bootstrap_no_op = classify_cumulative_baseline(
            incremental, [incremental_path], expected_target_sha=self.SHA
        )
        self.assertTrue(bootstrap_no_op["no_op"])
        self.assertFalse(bootstrap_no_op["manifest"]["deployable"])
        self.assertTrue(bootstrap_no_op["manifest"]["runtime_sensitive"])

        runtime_overlap_path = "platform/tools/platform_live_qa_guard.py"
        runtime_overlap_incremental = classify(
            [runtime_overlap_path],
            event="push",
            branch="dev",
            target_sha=self.SHA,
        )
        self.assertTrue(runtime_overlap_incremental["runtime_sensitive"])
        runtime_overlap_no_op = classify_cumulative_baseline(
            runtime_overlap_incremental,
            [runtime_overlap_path],
            expected_target_sha=self.SHA,
        )
        self.assertTrue(runtime_overlap_no_op["no_op"])
        self.assertTrue(runtime_overlap_no_op["manifest"]["runtime_sensitive"])

        candidate_installer = "platform/tools/platform_live_qa_runtime_install.py"
        installer_route = classify_cumulative_baseline(
            incremental,
            [incremental_path, candidate_installer],
            expected_target_sha=self.SHA,
        )
        self.assertFalse(installer_route["no_op"])
        self.assertTrue(installer_route["manifest"]["deployable"])
        self.assertTrue(installer_route["manifest"]["runtime_sensitive"])

        for unsafe_paths in (
            [incremental_path, "unowned/private-secret.txt"],
        ):
            with self.subTest(paths=unsafe_paths), self.assertRaises(ProvenanceError):
                classify_cumulative_baseline(
                    incremental, unsafe_paths, expected_target_sha=self.SHA
                )

        ordinary_incremental = classify(
            ["platform/tools/platform_build_release.sh"],
            event="push",
            branch="dev",
            target_sha=self.SHA,
        )
        with self.assertRaises(ProvenanceError):
            classify_cumulative_baseline(
                ordinary_incremental,
                ["platform/tools/platform_build_release.sh"],
                expected_target_sha=self.SHA,
            )

        tampered = dict(incremental)
        tampered["runtime_sensitive"] = False
        with self.assertRaises(ProvenanceError):
            classify_cumulative_baseline(
                tampered,
                [incremental_path, "platform/tools/platform_build_release.sh"],
                expected_target_sha=self.SHA,
            )

    def test_candidate_packaging_reconcile_source_is_closed_and_cumulative(self) -> None:
        candidate_path = ".github/workflows/platform-host-tools-candidate.yml"
        shared_path = ".github/workflows/platform-production-autodeploy.yml"
        recovery_only_path = "platform/tools/platform_recovery_bootstrap.py"
        docs_path = "platform/docs/candidate-route.md"
        app_path = "platform/tools/platform_build_release.sh"
        self.assertIn(candidate_path, CANDIDATE_PACKAGING_FILES)
        self.assertIn(shared_path, CANDIDATE_PACKAGING_FILES)
        self.assertIn(shared_path, RECOVERY_BOOTSTRAP_FILES)
        self.assertIn(recovery_only_path, RECOVERY_BOOTSTRAP_FILES)

        candidate_paths = [candidate_path, shared_path, docs_path]
        incremental = classify(
            candidate_paths,
            event="push",
            branch="dev",
            target_sha=self.SHA,
        )
        self.assertEqual(incremental["reason"], CANDIDATE_PACKAGING_REASON)
        self.assertEqual(incremental["class"], "full")
        self.assertFalse(incremental["deployable"])
        self.assertFalse(incremental["runtime_sensitive"])

        pure_candidate = classify_cumulative_baseline(
            incremental, candidate_paths, expected_target_sha=self.SHA
        )
        self.assertTrue(pure_candidate["no_op"])
        self.assertEqual(
            pure_candidate["manifest"]["reason"], CANDIDATE_PACKAGING_REASON
        )
        with self.assertRaises(ProvenanceError):
            classify_cumulative_baseline(
                incremental, [candidate_path], expected_target_sha=self.SHA
            )

        mixed_candidate_and_application = classify_cumulative_baseline(
            incremental,
            [*candidate_paths, app_path],
            expected_target_sha=self.SHA,
        )
        self.assertFalse(mixed_candidate_and_application["no_op"])
        self.assertTrue(mixed_candidate_and_application["manifest"]["deployable"])
        self.assertTrue(
            mixed_candidate_and_application["manifest"]["runtime_sensitive"]
        )

        # A path present in both closed source families follows the canonical
        # recovery reason precedence when it appears alone. Adding a
        # candidate-exclusive path makes the incremental family unambiguous.
        shared_only = classify(
            [shared_path], event="push", branch="dev", target_sha=self.SHA
        )
        self.assertEqual(shared_only["reason"], RECOVERY_BOOTSTRAP_REASON)
        self.assertTrue(
            classify_cumulative_baseline(
                shared_only, [shared_path], expected_target_sha=self.SHA
            )["no_op"]
        )
        candidate_and_recovery = classify(
            [candidate_path, recovery_only_path],
            event="push",
            branch="dev",
            target_sha=self.SHA,
        )
        self.assertTrue(candidate_and_recovery["deployable"])
        with self.assertRaises(ProvenanceError):
            classify_cumulative_baseline(
                candidate_and_recovery,
                [candidate_path, recovery_only_path],
                expected_target_sha=self.SHA,
            )

        bad_candidate_runtime = dict(incremental)
        bad_candidate_runtime["runtime_sensitive"] = True
        bad_candidate_runtime["digest"] = manifest_digest(bad_candidate_runtime)
        with self.assertRaises(ProvenanceError):
            classify_cumulative_baseline(
                bad_candidate_runtime, candidate_paths, expected_target_sha=self.SHA
            )

        bad_candidate_reason = dict(incremental)
        bad_candidate_reason["reason"] = RECOVERY_BOOTSTRAP_REASON
        bad_candidate_reason["digest"] = manifest_digest(bad_candidate_reason)
        with self.assertRaises(ProvenanceError):
            classify_cumulative_baseline(
                bad_candidate_reason, candidate_paths, expected_target_sha=self.SHA
            )

        with self.assertRaises(ProvenanceError):
            classify_cumulative_baseline(
                incremental,
                [candidate_path, shared_path, "unowned/private-secret.txt"],
                expected_target_sha=self.SHA,
            )

    def test_exact_baseline_runtime_proof_is_accepted(self) -> None:
        result = self._validate_baseline_runtime_payload(self._baseline_runtime_payload())
        self.assertEqual(result["target_sha"], self.SHA)
        self.assertEqual(
            result["attempt_url"],
            "https://github.com/StrayForest/old_sparky/actions/runs/1234/attempts/2",
        )

    def test_baseline_runtime_proof_requires_exact_parent_run_correlation(self) -> None:
        baseline = self._baseline_runtime_payload()
        for field, value in (
            ("target_sha", "b" * 40),
            ("source_security_run_id", "999"),
            ("source_security_run_attempt", "9"),
            ("autodeploy_run_id", "999"),
            ("autodeploy_run_attempt", "9"),
            ("production_deploy_run_id", "999"),
            ("production_deploy_run_attempt", "9"),
            ("proof_run_attempt", "9"),
        ):
            candidate = copy.deepcopy(baseline)
            candidate[4][field] = value
            with self.subTest(field=field), self.assertRaises(ProvenanceError):
                self._validate_baseline_runtime_payload(candidate)

        candidate = copy.deepcopy(baseline)
        candidate[1]["display_title"] += ":stale"
        with self.assertRaises(ProvenanceError):
            self._validate_baseline_runtime_payload(candidate)

        candidate = copy.deepcopy(baseline)
        candidate[1]["path"] = f"{SECURITY_WORKFLOW_PATH}@refs/heads/dev"
        with self.assertRaises(ProvenanceError):
            self._validate_baseline_runtime_payload(candidate)

        title_mutations = (
            ("source_security_run_id", "s568.1"),
            ("autodeploy_run_id", "a679.1"),
            ("production_deploy_run_id", "d790.3"),
        )
        for label, replacement in title_mutations:
            candidate = copy.deepcopy(baseline)
            candidate[1]["display_title"] = candidate[1]["display_title"].replace(
                {"source_security_run_id": "s567.1", "autodeploy_run_id": "a678.1", "production_deploy_run_id": "d789.3"}[label],
                replacement,
            )
            with self.subTest(parent=label), self.assertRaises(ProvenanceError):
                self._validate_baseline_runtime_payload(candidate)

    def test_baseline_runtime_proof_rejects_wrong_workflow_ref_or_target(self) -> None:
        baseline = self._baseline_runtime_payload()
        mutations = (
            (0, "path", ".github/workflows/other.yml"),
            (0, "name", "Other workflow"),
            (1, "event", "push"),
            (1, "name", SECURITY_WORKFLOW_NAME),
            (1, "path", f"{SECURITY_WORKFLOW_PATH}@refs/heads/feature"),
            (1, "head_branch", "feature/other"),
            (1, "head_sha", "b" * 40),
            (1, "status", "in_progress"),
            (1, "conclusion", "failure"),
            (1, "run_attempt", 3),
        )
        for index, field, value in mutations:
            candidate = copy.deepcopy(baseline)
            candidate[index][field] = value
            with self.subTest(index=index, field=field), self.assertRaises(ProvenanceError):
                self._validate_baseline_runtime_payload(candidate)

    def test_baseline_runtime_proof_requires_complete_exact_success_gates(self) -> None:
        baseline = self._baseline_runtime_payload()
        candidate = list(copy.deepcopy(baseline))
        candidate[2] = [job for job in candidate[2] if job["name"] != "Trusted dev immutable release runtime"]
        with self.assertRaises(ProvenanceError):
            self._validate_baseline_runtime_payload(candidate)

        candidate = copy.deepcopy(baseline)
        next(job for job in candidate[2] if job["name"] == "Conditional release runtime fixture")["conclusion"] = "skipped"
        with self.assertRaises(ProvenanceError):
            self._validate_baseline_runtime_payload(candidate)

        candidate = copy.deepcopy(baseline)
        candidate[2].append(dict(candidate[2][0], id=30000))
        with self.assertRaises(ProvenanceError):
            self._validate_baseline_runtime_payload(candidate)

        for complete_flag, jobs_complete, statuses_complete in (
            ("jobs", False, True),
            ("statuses", True, False),
        ):
            candidate = copy.deepcopy(baseline)
            with self.subTest(incomplete=complete_flag), self.assertRaises(ProvenanceError):
                validate_baseline_runtime_proof(
                    candidate[0], candidate[1], candidate[2], candidate[3], candidate[4],
                    expected_target_sha=self.SHA, **candidate[5],
                    jobs_complete=jobs_complete, statuses_complete=statuses_complete,
                )

    def test_baseline_runtime_proof_requires_exact_latest_bot_status_and_receipt(self) -> None:
        baseline = self._baseline_runtime_payload()
        for mutation in (
            lambda payload: payload[3][0].update({"target_url": "https://github.com/attacker"}),
            lambda payload: payload[3][0].update({"state": "failure"}),
            lambda payload: payload[3][0].update({"creator": {"login": "attacker", "type": "User", "id": 1}}),
            lambda payload: payload[4].update({"proof_mode": "standard"}),
            lambda payload: payload[4].update({"required_gates": ["backend"]}),
            lambda payload: payload[4].update({"unexpected": True}),
        ):
            candidate = copy.deepcopy(baseline)
            mutation(candidate)
            with self.assertRaises(ProvenanceError):
                self._validate_baseline_runtime_payload(candidate)

        candidate = copy.deepcopy(baseline)
        candidate[3].append({
            **candidate[3][0],
            "id": 20002,
            "updated_at": "2026-10-02T12:00:00Z",
        })
        with self.assertRaises(ProvenanceError):
            self._validate_baseline_runtime_payload(candidate)

        candidate = copy.deepcopy(baseline)
        candidate[3][0]["id"] = candidate[3][1]["id"] if len(candidate[3]) > 1 else 20001
        candidate[3].append({**candidate[3][0], "updated_at": "2026-10-02T13:00:00Z"})
        with self.assertRaises(ProvenanceError):
            self._validate_baseline_runtime_payload(candidate)

        candidate = copy.deepcopy(baseline)
        candidate[3].clear()
        with self.assertRaises(ProvenanceError):
            self._validate_baseline_runtime_payload(candidate)

    def _autodeploy_payload(
        self,
    ) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
        run_id = 5678
        attempt = 3
        workflow = {
            "id": 88,
            "path": AUTODEPLOY_WORKFLOW_PATH,
            "name": AUTODEPLOY_WORKFLOW_NAME,
        }
        run = {
            "id": run_id,
            "workflow_id": 88,
            "name": AUTODEPLOY_WORKFLOW_NAME,
            "path": AUTODEPLOY_WORKFLOW_PATH,
            "display_title": AUTODEPLOY_WORKFLOW_NAME,
            "run_attempt": attempt,
            "event": "workflow_run",
            "head_branch": "dev",
            "head_sha": self.SHA,
            "status": "completed",
            "conclusion": "success",
            "html_url": f"https://github.com/StrayForest/old_sparky/actions/runs/{run_id}",
            "repository": {
                "full_name": "StrayForest/old_sparky",
                "name": "old_sparky",
                "owner": {"login": "StrayForest"},
            },
        }
        jobs = [
            {
                "id": 9901,
                "name": "dispatch",
                "status": "completed",
                "conclusion": "success",
                "steps": [
                    {"name": "Checkout", "status": "completed", "conclusion": "success"},
                    {
                        "name": AUTODEPLOY_DISPATCH_STEP_NAME,
                        "status": "completed",
                        "conclusion": "success",
                    },
                ],
            }
        ]
        return workflow, run, jobs

    def test_exact_autodeploy_dispatch_attempt_is_accepted(self) -> None:
        workflow, run, jobs = self._autodeploy_payload()
        self.assertEqual(
            validate_autodeploy_dispatch(
                workflow,
                run,
                jobs,
                expected_run_id=5678,
                expected_attempt=3,
                expected_target_sha=self.SHA,
            ),
            "https://github.com/StrayForest/old_sparky/actions/runs/5678/attempts/3",
        )

    def test_autodeploy_completion_wait_accepts_pending_then_exact_success(self) -> None:
        _, completed, _ = self._autodeploy_payload()
        pending = copy.deepcopy(completed)
        pending["status"] = "in_progress"
        pending["conclusion"] = None
        snapshots = iter((pending, completed))
        result = wait_for_autodeploy_completion(
            lambda: next(snapshots),
            expected_workflow_id=88,
            expected_run_id=5678,
            expected_attempt=3,
            expected_target_sha=self.SHA,
            timeout_seconds=1,
            poll_interval_seconds=0.001,
        )
        self.assertIs(result, completed)

    def test_autodeploy_completion_wait_rejects_wrong_path_and_terminal_failure(self) -> None:
        _, completed, _ = self._autodeploy_payload()
        for field, value in (
            ("path", ".github/workflows/platform-production-autodeploy.yml@refs/heads/dev"),
            ("head_sha", "b" * 40),
            ("event", "push"),
            ("run_attempt", 2),
        ):
            with self.subTest(field=field):
                candidate = copy.deepcopy(completed)
                candidate[field] = value
                with self.assertRaises(ProvenanceError):
                    wait_for_autodeploy_completion(
                        lambda: candidate,
                        expected_workflow_id=88,
                        expected_run_id=5678,
                        expected_attempt=3,
                        expected_target_sha=self.SHA,
                        timeout_seconds=0.01,
                        poll_interval_seconds=0.001,
                    )

        failed = copy.deepcopy(completed)
        failed["conclusion"] = "failure"
        with self.assertRaises(ProvenanceError):
            wait_for_autodeploy_completion(
                lambda: failed,
                expected_workflow_id=88,
                expected_run_id=5678,
                expected_attempt=3,
                expected_target_sha=self.SHA,
                timeout_seconds=0.01,
                poll_interval_seconds=0.001,
            )

    def test_autodeploy_completion_wait_times_out_on_pending_attempt(self) -> None:
        _, pending, _ = self._autodeploy_payload()
        pending["status"] = "in_progress"
        pending["conclusion"] = None
        with patch(
            "tools.platform_deploy_baseline.time.monotonic",
            side_effect=(10.0, 11.0),
        ):
            with self.assertRaisesRegex(ProvenanceError, "timed out"):
                wait_for_autodeploy_completion(
                    lambda: pending,
                    expected_workflow_id=88,
                    expected_run_id=5678,
                    expected_attempt=3,
                    expected_target_sha=self.SHA,
                    timeout_seconds=0.5,
                    poll_interval_seconds=0.1,
                )

    def test_new_baseline_inline_imports_resolve_and_match_helper_signatures(self) -> None:
        module_by_name = {
            "tools.platform_deploy_baseline": importlib.import_module(
                "tools.platform_deploy_baseline"
            ),
            "tools.platform_workflow_provenance": importlib.import_module(
                "tools.platform_workflow_provenance"
            ),
        }
        workflow_steps = (
            (
                WORKFLOW_DIR / "platform-production-deploy.yml",
                "Authenticate automatic reconciliation caller",
            ),
            (
                WORKFLOW_DIR / "platform-security.yml",
                "Validate exact current target and correlated source runs",
            ),
        )
        imports: dict[str, object] = {}
        call_nodes: list[ast.Call] = []
        for workflow_path, step_name in workflow_steps:
            document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
            steps = [
                step
                for job in document["jobs"].values()
                for step in job.get("steps", [])
                if isinstance(step, dict) and step.get("name") == step_name
            ]
            self.assertEqual(len(steps), 1, f"expected one step {step_name!r}")
            script = steps[0].get("run")
            self.assertIsInstance(script, str)
            heredocs = re.findall(
                r"<<['\"]PY['\"]\s*\n(.*?)^\s*PY\s*$",
                script,
                flags=re.MULTILINE | re.DOTALL,
            )
            self.assertTrue(heredocs, f"no Python heredoc in {step_name!r}")
            trees = [ast.parse(textwrap.dedent(block)) for block in heredocs]
            for tree in trees:
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module in module_by_name:
                        module = module_by_name[node.module]
                        for alias in node.names:
                            with self.subTest(step=step_name, symbol=alias.name):
                                self.assertTrue(hasattr(module, alias.name))
                            imports[alias.asname or alias.name] = getattr(module, alias.name)
                    elif isinstance(node, ast.Call):
                        call_nodes.append(node)

        runtime_proof_tree = ast.parse(
            (TOOLS / "platform_baseline_runtime_proof.py").read_text(encoding="utf-8")
        )
        runtime_imports = []
        for node in ast.walk(runtime_proof_tree):
            if isinstance(node, ast.ImportFrom) and node.module in module_by_name:
                module = module_by_name[node.module]
                for alias in node.names:
                    with self.subTest(runtime_proof_import=alias.name):
                        self.assertTrue(hasattr(module, alias.name))
                runtime_imports.append(node)
        self.assertTrue(runtime_imports)
        namespace: dict[str, object] = {}
        exec(
            compile(
                ast.Module(body=runtime_imports, type_ignores=[]),
                "baseline-runtime-imports",
                "exec",
            ),
            namespace,
        )
        self.assertIs(
            namespace["BASELINE_RUNTIME_STATUS_CONTEXT"],
            module_by_name["tools.platform_deploy_baseline"].BASELINE_RUNTIME_STATUS_CONTEXT,
        )

        self.assertIn("wait_for_autodeploy_completion", imports)
        for node in call_nodes:
            if isinstance(node.func, ast.Name) and node.func.id in imports:
                helper = imports[node.func.id]
                if not callable(helper) or not inspect.isfunction(helper):
                    continue
                self.assertFalse(any(keyword.arg is None for keyword in node.keywords))
                inspect.signature(helper).bind(
                    *([object()] * len(node.args)),
                    **{keyword.arg: object() for keyword in node.keywords},
                )

    def test_baseline_source_conditional_runtime_jobs_match_one_closed_route(self) -> None:
        workflow = yaml.safe_load(
            (WORKFLOW_DIR / "platform-security.yml").read_text(encoding="utf-8")
        )
        guard = workflow["jobs"]["baseline-runtime-guard"]
        steps = [
            step for step in guard.get("steps", [])
            if isinstance(step, dict)
            and step.get("name") == "Validate exact current target and correlated source runs"
        ]
        self.assertEqual(len(steps), 1)
        script = steps[0].get("run")
        self.assertIsInstance(script, str)
        heredocs = re.findall(
            r"<<['\"]PY['\"]\s*\n(.*?)^\s*PY\s*$",
            script,
            flags=re.MULTILINE | re.DOTALL,
        )
        tree = ast.parse(textwrap.dedent(heredocs[-1]))
        validator_node = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "validate_source_security_jobs"
        )
        module = ast.Module(
            body=[
                ast.Import(names=[ast.alias(name="sys")]),
                validator_node,
            ],
            type_ignores=[],
        )
        namespace: dict[str, object] = {}
        exec(compile(ast.fix_missing_locations(module), "baseline-source-jobs", "exec"), namespace)
        validate = namespace["validate_source_security_jobs"]

        def rows(runtime_conclusion: str) -> list[dict[str, object]]:
            required = (
                "Backend DB-free contours", "Backend PostgreSQL and Redis integration",
                "Backend privileged ephemeral contour", "Backend aggregate", "Python quality",
                "Security gates", "web-quality", "Web hermetic", "Documentation consistency",
                "Migration scenarios", "Verification contract", "status-final",
            )
            result = [
                {"name": name, "status": "completed", "conclusion": "success"}
                for name in required
            ]
            result.extend(
                {
                    "name": name,
                    "status": "completed",
                    "conclusion": runtime_conclusion,
                }
                for name in (
                    "Conditional release runtime fixture",
                    "Trusted dev immutable release runtime",
                )
            )
            return result

        validate(rows("success"))
        validate(rows("skipped"))
        mixed = rows("success")
        next(item for item in mixed if item["name"] == "Trusted dev immutable release runtime")["conclusion"] = "skipped"
        with self.assertRaises(SystemExit):
            validate(mixed)
        failed = rows("success")
        next(item for item in failed if item["name"] == "Trusted dev immutable release runtime")["conclusion"] = "failure"
        with self.assertRaises(SystemExit):
            validate(failed)
        duplicate = rows("skipped")
        duplicate.append(dict(next(item for item in duplicate if item["name"] == "Conditional release runtime fixture")))
        with self.assertRaises(SystemExit):
            validate(duplicate)

    def test_autodeploy_dispatch_requires_exact_run_job_and_step_success(self) -> None:
        for field, value in (
            ("path", ".github/workflows/other.yml"),
            ("name", "Other workflow"),
        ):
            workflow, run, jobs = self._autodeploy_payload()
            workflow[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ProvenanceError):
                    validate_autodeploy_dispatch(
                        workflow,
                        run,
                        jobs,
                        expected_run_id=5678,
                        expected_attempt=3,
                        expected_target_sha=self.SHA,
                    )

        for field, value in (
            ("id", 5679),
            ("run_attempt", 2),
            ("event", "workflow_dispatch"),
            ("head_branch", "feature/test"),
            ("head_sha", "b" * 40),
            ("path", f"{AUTODEPLOY_WORKFLOW_PATH}@refs/heads/dev"),
            ("status", "in_progress"),
            ("conclusion", "failure"),
        ):
            workflow, run, jobs = self._autodeploy_payload()
            run[field] = value
            with self.subTest(run_field=field):
                with self.assertRaises(ProvenanceError):
                    validate_autodeploy_dispatch(
                        workflow,
                        run,
                        jobs,
                        expected_run_id=5678,
                        expected_attempt=3,
                        expected_target_sha=self.SHA,
                    )

        workflow, run, jobs = self._autodeploy_payload()
        for candidate_jobs in (
            [],
            jobs + [{**jobs[0], "id": 9902}],
            [{**jobs[0], "conclusion": "failure"}],
            [{**jobs[0], "steps": []}],
            [
                {
                    **jobs[0],
                    "steps": jobs[0]["steps"]
                    + [dict(jobs[0]["steps"][1], number=3)],
                }
            ],
            [
                {
                    **jobs[0],
                    "steps": [
                        jobs[0]["steps"][0],
                        {**jobs[0]["steps"][1], "conclusion": "failure"},
                    ],
                }
            ],
        ):
            with self.subTest(jobs=candidate_jobs):
                with self.assertRaises(ProvenanceError):
                    validate_autodeploy_dispatch(
                        workflow,
                        run,
                        candidate_jobs,
                        expected_run_id=5678,
                        expected_attempt=3,
                        expected_target_sha=self.SHA,
                    )

    def test_active_baseline_accepts_old_exact_deployment_proof(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        result = validate_active_baseline(
            self._baseline(),
            workflow,
            run,
            jobs,
            statuses,
            expected_target_sha=self.SHA,
            current_dev_sha=self.SHA,
            first_parent_shas=[self.SHA],
            statuses_complete=True,
            jobs_complete=True,
            now=datetime(2026, 10, 20, tzinfo=timezone.utc),
        )
        self.assertEqual(
            result["deployment_attempt_url"],
            "https://github.com/StrayForest/old_sparky/actions/runs/1234/attempts/2",
        )

    def test_active_baseline_accepts_root_slug_bound_legacy_attempt_one_url(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        run["run_attempt"] = 1
        statuses[0]["target_url"] = run["html_url"]
        baseline = self._baseline()
        baseline["release_slug"] = "gha-1234-1-20260920T123932Z"

        result = validate_active_baseline(
            baseline,
            workflow,
            run,
            jobs,
            statuses,
            expected_target_sha=self.SHA,
            current_dev_sha=self.SHA,
            first_parent_shas=[self.SHA],
            statuses_complete=True,
            jobs_complete=True,
            latest_run=run,
            now=datetime(2026, 10, 20, tzinfo=timezone.utc),
        )

        self.assertEqual(
            result["deployment_attempt_url"],
            "https://github.com/StrayForest/old_sparky/actions/runs/1234/attempts/1",
        )
        self.assertEqual(
            statuses[0]["target_url"],
            "https://github.com/StrayForest/old_sparky/actions/runs/1234",
        )

    def test_active_baseline_legacy_url_rejects_run_level_rerun(self) -> None:
        workflow, attempt_one, jobs, statuses = self._payload()
        attempt_one["run_attempt"] = 1
        statuses[0]["target_url"] = attempt_one["html_url"]
        baseline = self._baseline()
        baseline["release_slug"] = "gha-1234-1-20260920T123932Z"
        latest_run = {**attempt_one, "run_attempt": 2}

        with self.assertRaises(ProvenanceError):
            validate_active_baseline(
                baseline,
                workflow,
                attempt_one,
                jobs,
                statuses,
                expected_target_sha=self.SHA,
                current_dev_sha=self.SHA,
                first_parent_shas=[self.SHA],
                statuses_complete=True,
                jobs_complete=True,
                latest_run=latest_run,
                now=datetime(2026, 10, 20, tzinfo=timezone.utc),
            )

    def test_active_baseline_legacy_url_rejects_attempt_two_and_identity_mismatch(self) -> None:
        common = {
            "expected_target_sha": self.SHA,
            "current_dev_sha": self.SHA,
            "first_parent_shas": [self.SHA],
            "statuses_complete": True,
            "jobs_complete": True,
        }
        cases = []

        workflow, run, jobs, statuses = self._payload()
        run["run_attempt"] = 2
        statuses[0]["target_url"] = run["html_url"]
        baseline = self._baseline()
        baseline["release_slug"] = "gha-1234-1-20260920T123932Z"
        cases.append((workflow, run, jobs, statuses, baseline))

        workflow, run, jobs, statuses = self._payload()
        run["run_attempt"] = 1
        statuses[0]["target_url"] = run["html_url"]
        baseline = self._baseline()
        baseline["release_slug"] = "gha-9999-1-20260920T123932Z"
        cases.append((workflow, run, jobs, statuses, baseline))

        workflow, run, jobs, statuses = self._payload()
        run["run_attempt"] = 1
        statuses[0]["target_url"] = "https://github.com/StrayForest/old_sparky/actions/runs/9999"
        baseline = self._baseline()
        baseline["release_slug"] = "gha-9999-1-20260920T123932Z"
        cases.append((workflow, run, jobs, statuses, baseline))

        for workflow, run, jobs, statuses, baseline in cases:
            with self.subTest(attempt=run["run_attempt"], slug=baseline["release_slug"]):
                with self.assertRaises(ProvenanceError):
                    validate_active_baseline(
                        baseline,
                        workflow,
                        run,
                        jobs,
                        statuses,
                        **common,
                    )

    def test_legacy_status_identity_is_closed_and_exact_attempt_urls_stay_strict(self) -> None:
        self.assertEqual(
            active_deployment_status_identity(
                "https://github.com/StrayForest/old_sparky/actions/runs/1234/attempts/2",
                "gha-35511236041-1-20260920T123932Z",
            ),
            (1234, 2, False),
        )
        for target_url, release_slug in (
            (
                "https://github.com/StrayForest/old_sparky/actions/runs/1234?extra=1",
                "gha-1234-1-20260920T123932Z",
            ),
            (
                "https://github.com/StrayForest/old_sparky/actions/runs/1234",
                "gha-1234-1-20260230T123932Z",
            ),
            (
                "https://github.com/StrayForest/old_sparky/actions/runs/1234",
                "gha-1234-2-20260920T123932Z",
            ),
        ):
            with self.subTest(target_url=target_url, release_slug=release_slug):
                with self.assertRaises(ProvenanceError):
                    active_deployment_status_identity(target_url, release_slug)

    def test_active_baseline_may_be_older_first_parent_ancestor(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        target_sha = "c" * 40
        result = validate_active_baseline(
            self._baseline(),
            workflow,
            run,
            jobs,
            statuses,
            expected_target_sha=target_sha,
            current_dev_sha=target_sha,
            first_parent_shas=[target_sha, self.SHA],
            statuses_complete=True,
            jobs_complete=True,
            now=datetime(2026, 10, 20, tzinfo=timezone.utc),
        )
        self.assertEqual(result["target_sha"], target_sha)

    def test_active_baseline_requires_current_target_and_first_parent_ancestry(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        common = {
            "expected_target_sha": self.SHA,
            "current_dev_sha": self.SHA,
            "statuses_complete": True,
            "jobs_complete": True,
        }
        for baseline, current_dev, parents in (
            (self._baseline("b" * 40), self.SHA, [self.SHA]),
            (self._baseline(), "c" * 40, [self.SHA]),
            (self._baseline(), self.SHA, ["c" * 40, "d" * 40]),
            (self._baseline(), self.SHA, [self.SHA, self.SHA]),
            (self._baseline(), self.SHA, []),
        ):
            with self.subTest(baseline_sha=baseline["source_sha"], parents=parents):
                with self.assertRaises(ProvenanceError):
                    validate_active_baseline(
                        baseline,
                        workflow,
                        run,
                        jobs,
                        statuses,
                        **{
                            **common,
                            "current_dev_sha": current_dev,
                            "first_parent_shas": parents,
                        },
                    )

    def test_active_baseline_rejects_pending_or_malformed_host_tuple(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        for key, value in (
            ("pending_operation", True),
            ("pending_operation", None),
            ("current_link_ino", True),
            ("release_dev", -1),
            ("release_json_sha256", "g" * 64),
            ("release_slug", "../current"),
        ):
            baseline = self._baseline()
            baseline[key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(ProvenanceError):
                    validate_active_baseline(
                        baseline,
                        workflow,
                        run,
                        jobs,
                        statuses,
                        expected_target_sha=self.SHA,
                        current_dev_sha=self.SHA,
                        first_parent_shas=[self.SHA],
                        statuses_complete=True,
                        jobs_complete=True,
                    )
        extra = self._baseline()
        extra["unexpected"] = "ignored fields must not be accepted"
        with self.assertRaises(ProvenanceError):
            validate_active_baseline(
                extra,
                workflow,
                run,
                jobs,
                statuses,
                expected_target_sha=self.SHA,
                current_dev_sha=self.SHA,
                first_parent_shas=[self.SHA],
                statuses_complete=True,
                jobs_complete=True,
            )

    def test_active_baseline_rejects_incomplete_or_ambiguous_status_proof(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        common = {
            "expected_target_sha": self.SHA,
            "current_dev_sha": self.SHA,
            "first_parent_shas": [self.SHA],
            "jobs_complete": True,
        }
        for candidate_statuses, complete in (
            (statuses, False),
            ([], True),
            (
                statuses
                + [
                    {
                        **statuses[0],
                        "id": 9101,
                        "target_url": "https://github.com/StrayForest/old_sparky/actions/runs/1234/attempts/1",
                    }
                ],
                True,
            ),
            (
                statuses
                + [
                    {
                        **statuses[0],
                        "id": 9101,
                        "state": "failure",
                        "description": "Production deployment failed",
                    }
                ],
                True,
            ),
        ):
            with self.subTest(complete=complete, rows=len(candidate_statuses)):
                with self.assertRaises(ProvenanceError):
                    validate_active_baseline(
                        self._baseline(),
                        workflow,
                        run,
                        jobs,
                        candidate_statuses,
                        **{**common, "statuses_complete": complete},
                    )

        with self.assertRaises(ProvenanceError):
            validate_active_baseline(
                self._baseline(),
                workflow,
                run,
                jobs,
                statuses,
                **{**common, "statuses_complete": True, "jobs_complete": False},
            )

    def test_no_age_status_timestamp_still_rejects_future_values(self) -> None:
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        self.assertEqual(
            parse_status_timestamp(
                "2026-09-19T10:00:00Z", now=now, max_age=None
            ),
            datetime(2026, 9, 19, 10, tzinfo=timezone.utc),
        )
        with self.assertRaises(ProvenanceError):
            parse_status_timestamp(
                "2026-10-21T10:00:00Z", now=now, max_age=None
            )

    def test_exact_deploy_attempt_is_accepted(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        self.assertEqual(
            validate_deployment_marker(
                workflow,
                run,
                jobs,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
                expected_run_url=run["html_url"],
            ),
            "https://github.com/StrayForest/old_sparky/actions/runs/1234/attempts/2",
        )
        with self.assertRaises(ProvenanceError):
            validate_deployment_marker(
                workflow,
                run,
                jobs,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
                expected_run_url=f"{run['html_url']}/wrong",
            )

    def test_exact_failed_report_deployment_requires_failed_run_job_and_bot_marker(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        run["conclusion"] = "failure"
        jobs[0]["conclusion"] = "failure"
        jobs[0].update(run_id=1234, run_attempt=2, head_sha=self.SHA)
        statuses[0].update(
            state="failure",
            description="Production deployment failed",
        )
        expected_url = f"{run['html_url']}/attempts/{run['run_attempt']}"
        self.assertEqual(
            validate_failed_report_deployment(
                workflow,
                run,
                jobs,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
                expected_run_url=run["html_url"],
            ),
            expected_url,
        )
        documented_job_shape = [
            {key: value for key, value in job.items() if key != "run_attempt"}
            for job in jobs
        ]
        self.assertEqual(
            validate_failed_report_deployment(
                workflow,
                run,
                documented_job_shape,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
                expected_run_url=run["html_url"],
            ),
            expected_url,
        )

        for field, value in (
            ("id", 1235),
            ("run_attempt", 3),
            ("head_sha", "b" * 40),
            ("head_branch", "topic"),
            ("event", "push"),
            ("status", "in_progress"),
            ("conclusion", "success"),
            ("path", ".github/workflows/other.yml"),
        ):
            candidate = copy.deepcopy((workflow, run, jobs, statuses))
            candidate[1][field] = value
            with self.subTest(run_field=field), self.assertRaises(ProvenanceError):
                validate_failed_report_deployment(
                    *candidate,
                    expected_run_id=1234,
                    expected_attempt=2,
                    expected_target_sha=self.SHA,
                )
        for field, value in (
            ("run_id", 1235),
            ("run_attempt", 3),
            ("head_sha", "b" * 40),
        ):
            candidate = copy.deepcopy((workflow, run, jobs, statuses))
            candidate[2][0][field] = value
            with self.subTest(job_field=field), self.assertRaises(ProvenanceError):
                validate_failed_report_deployment(
                    *candidate,
                    expected_run_id=1234,
                    expected_attempt=2,
                    expected_target_sha=self.SHA,
                )

    def test_recovery_security_requires_all_full_gates_and_both_real_runtime_jobs(self) -> None:
        run_id, attempt = 808, 3
        base = f"https://github.com/StrayForest/old_sparky/actions/runs/{run_id}"
        workflow = {
            "id": 91,
            "path": SECURITY_WORKFLOW_PATH,
            "name": SECURITY_WORKFLOW_NAME,
        }
        run = {
            "id": run_id,
            "workflow_id": 91,
            "path": SECURITY_WORKFLOW_PATH,
            "name": SECURITY_WORKFLOW_NAME,
            "display_title": SECURITY_WORKFLOW_NAME,
            "run_attempt": attempt,
            "event": "push",
            "head_branch": "dev",
            "head_sha": self.SHA,
            "status": "completed",
            "conclusion": "success",
            "html_url": base,
            "repository": {
                "full_name": "StrayForest/old_sparky",
                "name": "old_sparky",
                "owner": {"login": "StrayForest"},
            },
        }
        jobs = [
            {
                "id": index,
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "run_id": run_id,
                "run_attempt": attempt,
                "head_sha": self.SHA,
            }
            for index, name in enumerate(sorted(RECOVERY_REQUIRED_SECURITY_JOBS), 1)
        ]
        statuses = [
            {
                "id": 999,
                "context": SECURITY_STATUS_CONTEXT,
                "state": "success",
                "description": SECURITY_SUCCESS_DESCRIPTION,
                "target_url": f"{base}/attempts/{attempt}",
                "updated_at": "2026-10-05T10:00:00Z",
                "creator": {"login": "github-actions[bot]", "type": "Bot", "id": 41898282},
            }
        ]
        self.assertEqual(
            validate_recovery_source_security_gates(
                workflow,
                run,
                jobs,
                statuses,
                expected_run_id=run_id,
                expected_attempt=attempt,
                expected_target_sha=self.SHA,
                now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
            ),
            f"{base}/attempts/{attempt}",
        )
        documented_job_shape = [
            {key: value for key, value in job.items() if key != "run_attempt"}
            for job in jobs
        ]
        self.assertEqual(
            validate_recovery_source_security_gates(
                workflow,
                run,
                documented_job_shape,
                statuses,
                expected_run_id=run_id,
                expected_attempt=attempt,
                expected_target_sha=self.SHA,
                now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
            ),
            f"{base}/attempts/{attempt}",
        )
        for rejected in (
            [{**job, "conclusion": "skipped"} if job["name"] == "Trusted dev immutable release runtime" else job for job in jobs],
            [job for job in jobs if job["name"] != "Conditional release runtime fixture"],
            [{**job, "run_attempt": 2} if job["name"] == "Backend aggregate" else job for job in jobs],
            [{**job, "head_sha": "b" * 40} if job["name"] == "Python quality" else job for job in jobs],
        ):
            with self.subTest(jobs=rejected), self.assertRaises(ProvenanceError):
                validate_recovery_source_security_gates(
                    workflow,
                    run,
                    rejected,
                    statuses,
                    expected_run_id=run_id,
                    expected_attempt=attempt,
                    expected_target_sha=self.SHA,
                )

        for changed_jobs, changed_statuses in (
            ([], statuses),
            ([{**jobs[0], "conclusion": "success"}], statuses),
            (jobs + [dict(jobs[0])], statuses),
            (jobs, [{**statuses[0], "state": "success"}]),
            (jobs, [{**statuses[0], "target_url": "https://github.com/StrayForest/old_sparky/actions/runs/1234"}]),
            (jobs, [{**statuses[0], "creator": {"login": "operator", "type": "User", "id": 1}}]),
            (jobs, [{**statuses[0], "description": "another failure"}]),
        ):
            with self.subTest(jobs=changed_jobs, statuses=changed_statuses), self.assertRaises(ProvenanceError):
                validate_failed_report_deployment(
                    workflow,
                    run,
                    changed_jobs,
                    changed_statuses,
                    expected_run_id=1234,
                    expected_attempt=2,
                    expected_target_sha=self.SHA,
                )

    def test_report_only_remote_diagnostic_accepts_only_closed_plain_text_marker(self) -> None:
        valid = (
            "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed "
            "reason=invalid_marker child_exit=0 observed_bytes=223 dispatcher_exit=2\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "job-logs.txt"

            def write_log(body: str | bytes, *, symlink: bool = False) -> None:
                log_path.write_bytes(body.encode("utf-8") if isinstance(body, str) else body)
                if symlink:
                    log_path.unlink()
                    log_path.symlink_to(Path(__file__))

            write_log(valid)
            self.assertEqual(
                validate_report_only_remote_diagnostic(log_path),
                {
                    "reason": "invalid_marker",
                    "child_exit": 0,
                    "observed_bytes": 223,
                    "dispatcher_exit": 2,
                },
            )
            write_log("2026-10-05T12:34:56.1234567Z " + valid.rstrip("\n") + "\n")
            self.assertEqual(
                validate_report_only_remote_diagnostic(log_path)["observed_bytes"],
                223,
            )

            invalid_markers = (
                valid.replace("child_exit=0", "child_exit=1"),
                valid.replace("dispatcher_exit=2", "dispatcher_exit=0"),
                valid.replace("reason=invalid_marker", "reason=ssh_failure"),
                valid.replace("observed_bytes=223", "observed_bytes=0"),
                valid.replace("observed_bytes=223", "observed_bytes=513"),
                valid.replace("observed_bytes=223", "observed_bytes=not-a-number"),
                valid + "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed reason=other\n",
                valid + valid,
            )
            for body in invalid_markers:
                write_log(body)
                with self.subTest(body=body), self.assertRaises(ProvenanceError):
                    validate_report_only_remote_diagnostic(log_path)

            write_log("ordinary output\n" + valid)
            self.assertEqual(
                validate_report_only_remote_diagnostic(log_path)["dispatcher_exit"],
                2,
            )
            write_log(b"ordinary output\n\xff")
            with self.assertRaises(ProvenanceError):
                validate_report_only_remote_diagnostic(log_path)
            write_log(valid, symlink=True)
            with self.assertRaises(ProvenanceError):
                validate_report_only_remote_diagnostic(log_path)

        # A failed report proof never changes the strict successful marker
        # contract used by normal baseline reconciliation.
        workflow, run, jobs, statuses = self._payload()
        run["conclusion"] = "failure"
        jobs[0]["conclusion"] = "failure"
        statuses[0].update(
            state="failure",
            description="Production deployment failed",
        )
        with self.assertRaises(ProvenanceError):
            validate_deployment_marker(
                workflow,
                run,
                jobs,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
            )

        run["conclusion"] = "success"
        jobs[0]["conclusion"] = "success"
        statuses[0].update(
            state="success",
            description="Production deployment and live smoke passed",
        )
        reconcile_title = (
            f"Platform production deploy mode=baseline-reconcile target={self.SHA} "
            "source=567.1 auto=678.2"
        )
        reconcile_run = copy.deepcopy(run)
        reconcile_run["name"] = reconcile_title
        reconcile_run["display_title"] = reconcile_title
        validate_deployment_marker(
            workflow,
            reconcile_run,
            jobs,
            statuses,
            expected_run_id=1234,
            expected_attempt=2,
            expected_target_sha=self.SHA,
        )
        self.assertTrue(
            validate_deployment_event(
                workflow,
                reconcile_run,
                jobs,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
            )
        )
        for bad_name in (
            reconcile_title.replace(self.SHA, "b" * 40),
            reconcile_title + " extra",
            reconcile_title.replace("source=567.1", "source=0.1"),
        ):
            invalid_reconcile = copy.deepcopy(reconcile_run)
            invalid_reconcile["name"] = bad_name
            invalid_reconcile["display_title"] = bad_name
            with self.subTest(run_name=bad_name), self.assertRaises(ProvenanceError):
                validate_deployment_marker(
                    workflow,
                    invalid_reconcile,
                    jobs,
                    statuses,
                    expected_run_id=1234,
                    expected_attempt=2,
                    expected_target_sha=self.SHA,
                )
        mismatched_run_name = copy.deepcopy(reconcile_run)
        mismatched_run_name["name"] = "Platform production deploy"
        with self.assertRaises(ProvenanceError):
            validate_deployment_marker(
                workflow,
                mismatched_run_name,
                jobs,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
            )
        mismatched_title = copy.deepcopy(reconcile_run)
        mismatched_title["display_title"] = "Platform production deploy"
        with self.assertRaises(ProvenanceError):
            validate_deployment_marker(
                workflow,
                mismatched_title,
                jobs,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
            )

    def test_old_attempt_cannot_authorize_mutation(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        run["run_attempt"] = 1
        with self.assertRaises(ProvenanceError):
            validate_deployment_marker(
                workflow,
                run,
                jobs,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
            )

    def test_run_and_attempt_ids_are_bounded_positive_decimals(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        for field in ("run", "run_attempt"):
            for value in (0, 10**32):
                with self.subTest(field=field, value=value):
                    candidate_run = copy.deepcopy(run)
                    expected_run_id = 1234
                    expected_attempt = 2
                    if field == "run":
                        candidate_run["id"] = value
                        expected_run_id = value
                    else:
                        candidate_run["run_attempt"] = value
                        expected_attempt = value
                    with self.assertRaises(ProvenanceError):
                        validate_deployment_marker(
                            workflow,
                            candidate_run,
                            jobs,
                            statuses,
                            expected_run_id=expected_run_id,
                            expected_attempt=expected_attempt,
                            expected_target_sha=self.SHA,
                        )

    def test_spoofed_status_cannot_authorize_mutation(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        spoof = copy.deepcopy(statuses[0])
        spoof["creator"] = {"login": "attacker", "type": "User", "id": 1}
        statuses.append(spoof)
        statuses[0]["updated_at"] = "2026-09-19T09:00:00Z"
        with self.assertRaises(ProvenanceError):
            validate_deployment_marker(
                workflow,
                run,
                jobs,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
            )
        for label, creator in (
            ("missing", "__missing__"),
            ("null", None),
            ("wrong", {"login": "attacker", "type": "User", "id": 1}),
        ):
            candidate_statuses = copy.deepcopy(self._payload()[3])
            if creator == "__missing__":
                candidate_statuses[0].pop("creator")
            else:
                candidate_statuses[0]["creator"] = creator
            with self.subTest(creator=label):
                with self.assertRaises(ProvenanceError):
                    validate_deployment_marker(
                        workflow,
                        run,
                        jobs,
                        candidate_statuses,
                        expected_run_id=1234,
                        expected_attempt=2,
                        expected_target_sha=self.SHA,
                    )

    def test_wrong_repository_or_workflow_cannot_authorize_mutation(self) -> None:
        for field, value in (
            ("repository", {"full_name": "attacker/repo", "name": "repo", "owner": {"login": "attacker"}}),
            ("workflow", {"id": 77, "path": ".github/workflows/other.yml", "name": DEPLOY_WORKFLOW_NAME}),
        ):
            with self.subTest(field=field):
                workflow, run, jobs, statuses = self._payload()
                if field == "repository":
                    run["repository"] = value
                else:
                    workflow = value
                with self.assertRaises(ProvenanceError):
                    validate_deployment_marker(
                        workflow,
                        run,
                        jobs,
                        statuses,
                        expected_run_id=1234,
                        expected_attempt=2,
                        expected_target_sha=self.SHA,
                    )

    def test_preflight_or_api_failure_is_not_a_deploy_marker(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        run["name"] = "Production preflight"
        jobs[0]["name"] = "Production preflight"
        with self.assertRaises(ProvenanceError):
            validate_deployment_marker(
                workflow,
                run,
                jobs,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
            )
        for malformed_jobs, malformed_statuses in (
            (None, "valid"),
            ("valid", None),
            ([None], "valid"),
            ("valid", [None]),
        ):
            with self.subTest(jobs=malformed_jobs, statuses=malformed_statuses):
                workflow, run, valid_jobs, valid_statuses = self._payload()
                jobs_value = (
                    valid_jobs if malformed_jobs == "valid" else malformed_jobs
                )
                statuses_value = (
                    valid_statuses
                    if malformed_statuses == "valid"
                    else malformed_statuses
                )
                with self.assertRaises(ProvenanceError):
                    validate_deployment_marker(
                        workflow,
                        run,
                        jobs_value,
                        statuses_value,
                        expected_run_id=1234,
                        expected_attempt=2,
                        expected_target_sha=self.SHA,
                    )
        with self.assertRaises(ProvenanceError):
            validate_deployment_marker(
                workflow,
                run,
                [],
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
            )

    def test_status_timestamp_is_strict_bounded_and_ties_fail_closed(self) -> None:
        now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
        base = {
            "id": 9100,
            "context": DEPLOY_STATUS_CONTEXT,
            "state": "success",
            "target_url": "https://github.com/StrayForest/old_sparky/actions/runs/1/attempts/1",
            "creator": {
                "login": "github-actions[bot]",
                "type": "Bot",
                "id": 41898282,
            },
        }
        for timestamp in (
            "2026-09-19T11:00:00zzzz",
            "2026-09-19T13:00:00Z",
            "2020-01-01T00:00:00Z",
        ):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(ProvenanceError):
                    latest_context_status(
                        [{**base, "updated_at": timestamp}],
                        context=DEPLOY_STATUS_CONTEXT,
                        now=now,
                    )
        with self.assertRaisesRegex(ProvenanceError, "ambiguous"):
            latest_context_status(
                [
                    {**base, "updated_at": "2026-09-19T11:00:00Z"},
                    {**base, "updated_at": "2026-09-19T11:00:00Z"},
                ],
                context=DEPLOY_STATUS_CONTEXT,
                now=now,
            )

    def test_status_description_and_target_are_bound_to_the_exact_attempt(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        statuses[0]["description"] = "spoofed description"
        with self.assertRaises(ProvenanceError):
            validate_deployment_marker(
                workflow,
                run,
                jobs,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
            )
        statuses[0]["description"] = "Production deployment and live smoke passed"
        statuses[0]["target_url"] = statuses[0]["target_url"].replace(
            "/attempts/2", "/attempts/1"
        )
        with self.assertRaises(ProvenanceError):
            validate_deployment_marker(
                workflow,
                run,
                jobs,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
            )
        for field, value in (
            ("context", "another-context"),
            ("state", "failure"),
        ):
            candidate_statuses = copy.deepcopy(self._payload()[3])
            candidate_statuses[0][field] = value
            with self.subTest(field=field):
                with self.assertRaises(ProvenanceError):
                    validate_deployment_marker(
                        workflow,
                        run,
                        jobs,
                        candidate_statuses,
                        expected_run_id=1234,
                        expected_attempt=2,
                        expected_target_sha=self.SHA,
                    )

    def test_complete_paginated_status_payload_rejects_truncation(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        self.assertEqual(_payload_rows(statuses, "statuses", "status"), statuses)
        payload = {"total_count": len(statuses), "statuses": statuses}
        self.assertEqual(_payload_rows(payload, "statuses", "status"), statuses)
        with self.assertRaisesRegex(ProvenanceError, "incomplete"):
            _payload_rows(
                {"total_count": len(statuses) + 1, "statuses": statuses},
                "statuses",
                "status",
            )
        for invalid_statuses in (payload, None):
            with self.subTest(statuses=invalid_statuses):
                with self.assertRaises(ProvenanceError):
                    validate_deployment_marker(
                        workflow,
                        run,
                        jobs,
                        invalid_statuses,
                        expected_run_id=1234,
                        expected_attempt=2,
                        expected_target_sha=self.SHA,
                    )
        with self.assertRaises(ProvenanceError):
            _payload_rows(
                {"total_count": True, "statuses": statuses},
                "statuses",
                "status",
            )

    def test_preflight_is_the_only_authenticated_noop(self) -> None:
        workflow, run, jobs, _statuses = self._payload()
        jobs = [
            {
                "id": 9001,
                "name": "Deploy production",
                "status": "completed",
                "conclusion": "skipped",
            },
            {
                "id": 9002,
                "name": "Production preflight",
                "status": "completed",
                "conclusion": "success",
            },
        ]
        self.assertFalse(
            validate_deployment_event(
                workflow,
                run,
                jobs,
                [],
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
                expected_run_url=run["html_url"],
            )
        )

    def test_job_row_ambiguity_and_malformed_api_ids_fail_closed(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        with self.assertRaisesRegex(ProvenanceError, "ambiguous"):
            validate_deployment_marker(
                workflow,
                run,
                [*jobs, {**jobs[0], "id": 9002}],
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
            )
        for field, value in (
            ("workflow", {**workflow, "id": True}),
            ("run", {**run, "id": "1234"}),
            ("run_attempt", {**run, "run_attempt": "2"}),
            ("workflow_id", {**run, "workflow_id": "77"}),
            ("job", [{**jobs[0], "id": "9001"}]),
            ("status", [{**statuses[0], "id": "9100"}]),
        ):
            with self.subTest(field=field):
                candidate_workflow = workflow
                candidate_run = run
                candidate_jobs = jobs
                candidate_statuses = statuses
                if field == "workflow":
                    candidate_workflow = value
                elif field == "job":
                    candidate_jobs = value
                elif field == "status":
                    candidate_statuses = value
                else:
                    candidate_run = value
                with self.assertRaises(ProvenanceError):
                    validate_deployment_marker(
                        candidate_workflow,
                        candidate_run,
                        candidate_jobs,
                        candidate_statuses if field == "status" else statuses,
                        expected_run_id=1234,
                        expected_attempt=2,
                        expected_target_sha=self.SHA,
                    )

    def test_snapshot_digest_detects_a_status_race(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        first = deployment_snapshot_digest(workflow, run, jobs, statuses)
        changed = copy.deepcopy(statuses)
        changed[0]["description"] = "changed after first snapshot"
        second = deployment_snapshot_digest(workflow, run, jobs, changed)
        self.assertNotEqual(first, second)

    def test_consumers_paginate_and_require_adjacent_equal_snapshots(self) -> None:
        for workflow_name in (
            "platform-production-autodeploy.yml",
            "platform-production-deploy.yml",
            "platform-production-content-diagnostics.yml",
            "platform-patch-translation-qa.yml",
        ):
            source = (WORKFLOW_DIR / workflow_name).read_text(encoding="utf-8")
            with self.subTest(workflow=workflow_name):
                self.assertIn("?per_page=100&page=${page}", source)
                self.assertIn("for snapshot in first second", source)
                self.assertIn("if first != second:", source)
                self.assertIn("pagination exceeded its bound", source)
                if workflow_name == "platform-production-autodeploy.yml":
                    self.assertIn(
                        "/commits/${TARGET_SHA}/statuses?per_page=100&page=${page}",
                        source,
                    )
                    self.assertNotIn(
                        "/commits/${TARGET_SHA}/status?per_page=100&page=${page}",
                        source,
                    )
                    self.assertNotIn(
                        "/commits/${TARGET_SHA}/status\"",
                        source,
                    )
                    self.assertIn("status rows contain duplicate or malformed IDs", source)

    def test_preflight_rejects_future_or_tied_deployment_markers(self) -> None:
        workflow, run, _jobs, _statuses = self._payload()
        jobs = [
            {
                "id": 9001,
                "name": "Deploy production",
                "status": "completed",
                "conclusion": "skipped",
            },
            {
                "id": 9002,
                "name": "Production preflight",
                "status": "completed",
                "conclusion": "success",
            },
        ]
        marker = {
            "id": 9100,
            "context": DEPLOY_STATUS_CONTEXT,
            "updated_at": "2026-09-19T13:00:00Z",
        }
        for statuses in (
            [marker],
            [
                {**marker, "updated_at": "2026-09-19T11:00:00Z"},
                {**marker, "id": 9101, "updated_at": "2026-09-19T11:00:00Z"},
            ],
        ):
            with self.subTest(statuses=statuses):
                with self.assertRaises(ProvenanceError):
                    validate_deployment_event(
                        workflow,
                        run,
                        jobs,
                        statuses,
                        expected_run_id=1234,
                        expected_attempt=2,
                        expected_target_sha=self.SHA,
                        expected_run_url=run["html_url"],
                        now=datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc),
                    )

if __name__ == "__main__":
    unittest.main()
