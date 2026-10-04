from __future__ import annotations

import copy
import ast
from datetime import datetime, timezone
import importlib
import inspect
import re
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


TOOLS = Path(__file__).resolve().parents[1] / "tools"
WORKFLOW_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
sys.path.insert(0, str(TOOLS))

from tools.platform_workflow_provenance import (  # noqa: E402
    AUTODEPLOY_DISPATCH_STEP_NAME,
    AUTODEPLOY_WORKFLOW_NAME,
    AUTODEPLOY_WORKFLOW_PATH,
    DEPLOY_WORKFLOW_NAME,
    DEPLOY_WORKFLOW_PATH,
    DEPLOY_STATUS_CONTEXT,
    ProvenanceError,
    _payload_rows,
    deployment_snapshot_digest,
    latest_context_status,
    parse_status_timestamp,
    validate_deployment_event,
    validate_deployment_marker,
    validate_autodeploy_dispatch,
    SECURITY_WORKFLOW_NAME,
    SECURITY_WORKFLOW_PATH,
)
from tools.platform_deploy_baseline import (  # noqa: E402
    BASELINE_RUNTIME_GATE_NAMES,
    classify_cumulative_baseline,
    validate_active_baseline,
    validate_baseline_runtime_proof,
    wait_for_autodeploy_completion,
)
from tools.platform_ci_classifier import (  # noqa: E402
    classify,
)


class WorkflowProvenanceTests(unittest.TestCase):
    SHA = "a" * 40

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
        self.assertFalse(incremental["runtime_sensitive"])
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
        self.assertFalse(bootstrap_no_op["manifest"]["runtime_sensitive"])

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
        tampered["runtime_sensitive"] = True
        with self.assertRaises(ProvenanceError):
            classify_cumulative_baseline(
                tampered,
                [incremental_path, "platform/tools/platform_build_release.sh"],
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
