from __future__ import annotations

import copy
from datetime import datetime, timezone
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


TOOLS = Path(__file__).resolve().parents[1] / "tools"
WORKFLOW_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
sys.path.insert(0, str(TOOLS))

from tools.platform_workflow_provenance import (  # noqa: E402
    DEPLOY_WORKFLOW_NAME,
    DEPLOY_WORKFLOW_PATH,
    DEPLOY_STATUS_CONTEXT,
    ProvenanceError,
    _payload_rows,
    _api_get_bytes,
    _api_paginate,
    _stable_api_snapshot,
    deployment_snapshot_digest,
    latest_context_status,
    validate_auto_release_jobs,
    validate_auto_noop_run,
    validate_deployment_event,
    validate_deployment_marker,
    validate_referenced_workflows,
)


class WorkflowProvenanceTests(unittest.TestCase):
    SHA = "a" * 40

    class _Response:
        def __init__(self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
            self.body = body
            self.status = status
            self.headers = headers or {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit: int = -1) -> bytes:
            return self.body

        def close(self) -> None:
            return None

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
                "run_id": run_id,
                "run_attempt": attempt,
                "head_sha": self.SHA,
                "name": "Deploy production",
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-09-19T09:00:00Z",
                "head_branch": "dev",
                "labels": ["ubuntu-latest"],
                "run_url": base,
                "workflow_name": DEPLOY_WORKFLOW_NAME,
                "runner_id": None,
                "runner_name": None,
                "runner_group_id": None,
                "runner_group_name": None,
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

    def test_inherited_workflow_run_event_is_bound_without_opening_other_events(self) -> None:
        workflow, run, jobs, statuses = self._payload()
        run["event"] = "workflow_run"
        self.assertEqual(
            validate_deployment_marker(
                workflow,
                run,
                jobs,
                statuses,
                expected_run_id=1234,
                expected_attempt=2,
                expected_target_sha=self.SHA,
                expected_event="workflow_run",
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
                expected_event="workflow_dispatch",
            )
        run["event"] = "workflow_call"
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



    def test_native_called_workflow_contract_and_result_propagation(self) -> None:
        auto = (WORKFLOW_DIR / "platform-production-autodeploy.yml").read_text(
            encoding="utf-8"
        )
        child = (WORKFLOW_DIR / "platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("uses: ./.github/workflows/platform-production-deploy.yml", auto)
        self.assertIn("needs: validate", auto)
        self.assertIn("needs.validate.outputs.deploy == 'true'", auto)
        for input_name in (
            "mode: ${{ needs.validate.outputs.mode }}",
            "runtime_profile: ${{ needs.validate.outputs.runtime_profile }}",
            "web_compression: ${{ needs.validate.outputs.web_compression }}",
            "target_sha: ${{ needs.validate.outputs.target_sha }}",
            "classifier_run_id: ${{ needs.validate.outputs.classifier_run_id }}",
            "classifier_run_attempt: ${{ needs.validate.outputs.classifier_run_attempt }}",
            "security_run_id: ${{ needs.validate.outputs.security_run_id }}",
            "security_run_attempt: ${{ needs.validate.outputs.security_run_attempt }}",
            "caller_kind: ${{ needs.validate.outputs.caller_kind }}",
            "caller_run_id: ${{ needs.validate.outputs.caller_run_id }}",
            "caller_run_attempt: ${{ needs.validate.outputs.caller_run_attempt }}",
            "caller_workflow_ref: ${{ needs.validate.outputs.caller_workflow_ref }}",
            "caller_workflow_sha: ${{ needs.validate.outputs.caller_workflow_sha }}",
            "caller_sha: ${{ needs.validate.outputs.caller_sha }}",
            "expected_called_workflow_file: ${{ needs.validate.outputs.expected_called_workflow_file }}",
            "expected_called_workflow_ref: ${{ needs.validate.outputs.expected_called_workflow_ref }}",
            "expected_called_workflow_sha: ${{ needs.validate.outputs.expected_called_workflow_sha }}",
        ):
            self.assertIn(input_name, auto)
        self.assertIn('echo "deploy=true" >> "$GITHUB_OUTPUT"', auto)
        self.assertGreaterEqual(auto.count('echo "deploy=false" >> "$GITHUB_OUTPUT"'), 2)
        self.assertIn('ROUTE_CLASS" != "full"', auto)
        self.assertIn("DEPLOY_RESULT: ${{ needs.deploy.result }}", auto)
        self.assertIn("Native production deployment did not succeed", auto)
        self.assertIn('[[ "$DEPLOY_RESULT" != "success" ]]', auto)
        self.assertIn("if: ${{ always() }}", auto)
        self.assertIn("needs.deploy.result", auto)
        self.assertIn("deploy_result", auto.lower())
        self.assertNotIn("actions: write", auto)
        self.assertNotIn("/dispatches", auto)
        self.assertNotIn("dispatch_key", auto)
        self.assertNotIn("DOWNSTREAM_", auto)
        self.assertNotIn("return_run_details", auto)
        self.assertNotIn("/actions/runs/${selected_id}/cancel", auto)
        self.assertIn("cancel-in-progress: false", auto)
        for permission in (
            "actions: read",
            "attestations: write",
            "contents: read",
            "id-token: write",
            "statuses: write",
        ):
            self.assertIn(permission, auto)
        self.assertNotIn("secrets.", auto)

        self.assertIn("workflow_call:", child)
        for input_name in (
            "      mode:\n        description:",
            "      runtime_profile:\n        description:",
            "      web_compression:\n        description:",
            "      target_sha:\n        description:",
            "      classifier_run_id:\n        description:",
            "      classifier_run_attempt:\n        description:",
        ):
            self.assertIn(input_name, child)
        called_inputs = child.split("  workflow_dispatch:", 1)[0]
        self.assertEqual(called_inputs.count("        type: string"), 17)
        self.assertEqual(called_inputs.count("        required: true"), 17)
        for default in (
            "        default: deploy",
            "        default: ready-vote-static-8",
            "        default: enabled",
            '        default: ""',
        ):
            self.assertIn(default, called_inputs)
        self.assertIn("workflow_dispatch:", child)
        self.assertIn("workflow_run)", child)
        self.assertIn("workflow_dispatch)", child)
        self.assertIn("github.event.workflow_run", child)
        self.assertIn("caller_workflow_ref", child)
        self.assertIn("expected_called_workflow_ref", child)
        self.assertIn("Manual production runs must target the dev branch.", child)
        self.assertIn("manual deployment target is not the current dev head", child)
        self.assertIn(
            "github.event_name == 'workflow_dispatch' && github.sha || inputs.target_sha",
            child,
        )
        self.assertIn("concurrency:", child)
        self.assertIn("group: platform-production-deploy", child)
        self.assertIn("cancel-in-progress: false", child)
        self.assertNotIn("github.workflow }}", child)
        self.assertNotIn("dispatch_key", child)
        self.assertNotIn("autodeploy_run_id", child)
        self.assertNotIn("autodeploy_run_attempt", child)
        self.assertNotIn("validate_autodeploy_caller_run", child)
        self.assertNotIn("caller lease", child.lower())
        self.assertIn("environment: production", child)
        self.assertNotIn("secrets.", child.split("environment: production", 1)[0])
        self.assertLess(
            child.index("Validate deployment secrets"),
            child.index("Configure SSH"),
        )
        self.assertIn("permissions:", child)
        self.assertIn("statuses: write", child)
        self.assertIn("- validate-security-provenance", child)
        self.assertIn("target SHA is not the current dev head", child)
        finalizer = child.split("  release-finalizer:", 1)[1]
        job_env = finalizer.split("    steps:", 1)[0]
        self.assertNotIn("job.workflow_", job_env)
        capture = finalizer.split("- name: Capture called workflow identity", 1)[1].split(
            "- name: Checkout exact called workflow source", 1
        )[0]
        for field in (
            "job.workflow_repository",
            "job.workflow_file_path",
            "job.workflow_ref",
            "job.workflow_sha",
        ):
            self.assertIn(field, capture)
        self.assertIn('test -n "$CALLED_WORKFLOW_REPOSITORY"', capture)
        self.assertIn("steps.called-workflow-identity.outputs.sha", finalizer)
        for line in finalizer.splitlines():
            if "job.workflow_" in line:
                self.assertEqual(len(line) - len(line.lstrip()), 10)

    def test_called_workflow_context_is_step_env_only(self) -> None:
        source = (WORKFLOW_DIR / "platform-production-deploy.yml").read_text(encoding="utf-8")
        finalizer = source.split("  release-finalizer:", 1)[1]
        job_header, steps = finalizer.split("    steps:", 1)
        self.assertNotIn("job.workflow_", job_header)
        context_lines = [line for line in steps.splitlines() if "job.workflow_" in line]
        self.assertEqual(len(context_lines), 4)
        self.assertTrue(all(line.startswith("          ") for line in context_lines))
        self.assertIn("id: called-workflow-identity", steps)
        self.assertIn('test -n "$CALLED_WORKFLOW_SHA"', steps)
        self.assertIn(">> \"$GITHUB_OUTPUT\"", steps)

    def test_native_call_jobs_use_only_documented_job_fields_and_final_barrier(self) -> None:
        called_sha = "b" * 40
        jobs = [
            {
                "id": 1,
                "run_id": 1234,
                "run_attempt": 2,
                "head_sha": called_sha,
                "name": "Caller / Deploy production",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 2,
                "run_id": 1234,
                "run_attempt": 2,
                "head_sha": called_sha,
                "name": "Auto-deploy result",
                "status": "completed",
                "conclusion": "success",
            },
        ]
        validate_auto_release_jobs(
            jobs,
            expected_run_id=1234,
            expected_attempt=2,
            expected_head_sha=called_sha,
        )
        jobs[0]["head_sha"] = "c" * 40
        with self.assertRaises(ProvenanceError):
            validate_auto_release_jobs(
                jobs,
                expected_run_id=1234,
                expected_attempt=2,
                expected_head_sha=called_sha,
            )

    def test_auto_jobs_accept_real_reusable_shape_and_reject_synthetic_call_name(self) -> None:
        called_sha = "b" * 40
        optional = {
            "created_at": "2026-09-19T09:00:00Z",
            "head_branch": "dev",
            "labels": ["ubuntu-latest"],
            "run_url": "https://github.com/StrayForest/old_sparky/actions/runs/1234",
            "workflow_name": "Platform production deploy",
            "runner_id": None,
            "runner_name": None,
            "runner_group_id": None,
            "runner_group_name": None,
        }
        jobs = [
            {
                **optional,
                "id": 1,
                "run_id": 1234,
                "run_attempt": 2,
                "head_sha": called_sha,
                "name": "Caller / Deploy production",
                "status": "completed",
                "conclusion": "success",
            },
            {
                **optional,
                "id": 2,
                "run_id": 1234,
                "run_attempt": 2,
                "head_sha": called_sha,
                "name": "Auto-deploy result",
                "status": "completed",
                "conclusion": "success",
            },
        ]
        validate_auto_release_jobs(
            jobs,
            expected_run_id=1234,
            expected_attempt=2,
            expected_head_sha=called_sha,
        )
        with self.assertRaises(ProvenanceError):
            validate_auto_release_jobs(
                [{**jobs[0], "name": "Native production deployment"}, jobs[1]],
                expected_run_id=1234,
                expected_attempt=2,
                expected_head_sha=called_sha,
            )

    def test_invented_jobs_workflow_fields_are_rejected(self) -> None:
        jobs = [{
            "id": 1,
            "name": "Deploy production",
            "status": "completed",
            "conclusion": "success",
            "workflow_file_path": DEPLOY_WORKFLOW_PATH,
        }]
        with self.assertRaisesRegex(ProvenanceError, "undocumented"):
            deployment_snapshot_digest({}, {}, jobs, [])

    def test_referenced_workflow_fixture_binds_path_ref_and_sha(self) -> None:
        run = {
            "referenced_workflows": [{
                "path": f"StrayForest/old_sparky/{DEPLOY_WORKFLOW_PATH}",
                "sha": "b" * 40,
            }]
        }
        matched = validate_referenced_workflows(
            run,
            expected_workflow_ref="StrayForest/old_sparky/.github/workflows/platform-production-deploy.yml@refs/heads/dev",
            expected_workflow_sha="b" * 40,
        )
        self.assertEqual(matched["path"], f"StrayForest/old_sparky/{DEPLOY_WORKFLOW_PATH}")
        run["referenced_workflows"][0]["ref"] = "refs/heads/feature/mutable-locator"
        self.assertIs(
            validate_referenced_workflows(
                run,
                expected_workflow_ref="StrayForest/old_sparky/.github/workflows/platform-production-deploy.yml@refs/heads/dev",
                expected_workflow_sha="b" * 40,
            ),
            matched,
        )
        run["referenced_workflows"][0]["sha"] = "c" * 40
        with self.assertRaises(ProvenanceError):
            validate_referenced_workflows(
                run,
                expected_workflow_ref="StrayForest/old_sparky/.github/workflows/platform-production-deploy.yml@refs/heads/dev",
                expected_workflow_sha="b" * 40,
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
                "run_id": 1234,
                "run_attempt": 2,
                "head_sha": self.SHA,
                "name": "Deploy production",
                "status": "completed",
                "conclusion": "skipped",
            },
            {
                "id": 9002,
                "run_id": 1234,
                "run_attempt": 2,
                "head_sha": self.SHA,
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

    def test_non_deployable_auto_run_is_a_markerless_noop(self) -> None:
        workflow, run, _jobs, statuses = self._payload()
        workflow = {
            "id": 88,
            "path": ".github/workflows/platform-production-autodeploy.yml",
            "name": "Platform production auto-deploy",
        }
        run.update({
            "workflow_id": 88,
            "name": "Platform production auto-deploy",
            "path": ".github/workflows/platform-production-autodeploy.yml",
            "event": "workflow_run",
        })
        optional = {
            "created_at": "2026-09-19T09:00:00Z",
            "head_branch": "dev",
            "labels": [],
            "run_url": run["html_url"],
            "workflow_name": "Platform production deploy",
            "runner_id": None,
            "runner_name": None,
            "runner_group_id": None,
            "runner_group_name": None,
        }
        jobs = [
            {
                **optional,
                "id": 9001,
                "run_id": 1234,
                "run_attempt": 2,
                "head_sha": self.SHA,
                "name": "Caller / Deploy production",
                "status": "completed",
                "conclusion": "skipped",
            },
            {
                **optional,
                "id": 9002,
                "run_id": 1234,
                "run_attempt": 2,
                "head_sha": self.SHA,
                "name": "Auto-deploy result",
                "status": "completed",
                "conclusion": "success",
            },
        ]
        validate_auto_noop_run(
            workflow,
            run,
            jobs,
            [],
            expected_run_id=1234,
            expected_attempt=2,
            expected_target_sha=self.SHA,
            expected_run_url=run["html_url"],
        )
        statuses.append({**statuses[0], "context": DEPLOY_STATUS_CONTEXT, "target_url": f"{run['html_url']}/attempts/2"})
        with self.assertRaises(ProvenanceError):
            validate_auto_noop_run(
                workflow, run, jobs, statuses,
                expected_run_id=1234, expected_attempt=2,
                expected_target_sha=self.SHA, expected_run_url=run["html_url"],
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

    def test_downstream_consumers_bind_receipt_target_and_terminal_status_finalizers(self) -> None:
        patch_qa = (WORKFLOW_DIR / "platform-patch-translation-qa.yml").read_text(encoding="utf-8")
        content = (WORKFLOW_DIR / "platform-production-content-diagnostics.yml").read_text(encoding="utf-8")
        for source in (patch_qa, content):
            self.assertIn("steps.downstream_receipt_provenance.outputs.target_sha", source)
            self.assertNotIn("workflow_run.head_sha", source)
            self.assertIn("finalizer:", source)
            self.assertIn("cancel-in-progress: false", source)
        serializer = patch_qa.split("- name: Serialize closed translation QA handoff", 1)[1]
        self.assertIn("steps.downstream_receipt_provenance.outputs.deploy_ready", serializer)
        self.assertNotIn("steps.deployment_provenance.outputs", serializer)
        content_job = content.split("  content-diagnostics:", 1)[1]
        self.assertIn("TARGET_SHA: ${{ needs.validate-content-provenance.outputs.target_sha }}", content_job)

    def test_artifact_download_is_authenticated_without_echoing_private_token(self) -> None:
        token = "private-token-that-must-not-leak"
        signed_url = "https://productionresultssa0.blob.core.windows.net/actions/9.zip?sig=signed"
        with patch(
            "tools.platform_workflow_provenance._open_no_redirect",
            side_effect=[
                self._Response(b"", status=302, headers={"Location": signed_url}),
                self._Response(b"zip-bytes", status=200),
            ],
        ) as opened:
            self.assertEqual(
                _api_get_bytes(
                    "https://api.github.com",
                    token,
                    "/repos/PrivateOrg/private-repo/actions/artifacts/9/zip",
                ),
                b"zip-bytes",
            )
        api_request = opened.call_args_list[0].args[0]
        artifact_request = opened.call_args_list[1].args[0]
        self.assertEqual(api_request.get_header("Authorization"), f"Bearer {token}")
        self.assertEqual(api_request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(api_request.get_header("X-github-api-version"), "2022-11-28")
        self.assertNotIn(token, api_request.full_url)
        self.assertIsNone(artifact_request.get_header("Authorization"))
        self.assertIsNone(artifact_request.get_header("Cookie"))
        self.assertEqual(artifact_request.get_header("Accept"), "application/octet-stream")
        self.assertNotIn(token, artifact_request.full_url)

        redirect_error = HTTPError(
            "https://api.github.com/repos/PrivateOrg/private-repo/actions/artifacts/9/zip",
            302,
            "found",
            {"Location": signed_url},
            None,
        )
        with patch(
            "tools.platform_workflow_provenance._open_no_redirect",
            side_effect=[redirect_error, self._Response(b"zip-bytes", status=200)],
        ) as opened_error:
            self.assertEqual(
                _api_get_bytes("https://api.github.com", token, "/private"),
                b"zip-bytes",
            )
        self.assertIsNone(opened_error.call_args_list[1].args[0].get_header("Authorization"))

        unauthorized = HTTPError(
            "https://api.github.com/repos/PrivateOrg/private-repo/actions/artifacts/9/zip",
            401,
            "unauthorized",
            {},
            None,
        )
        with patch("tools.platform_workflow_provenance._open_no_redirect", side_effect=unauthorized):
            with self.assertRaises(ProvenanceError) as raised:
                _api_get_bytes("https://api.github.com", token, "/private")
        self.assertNotIn(token, str(raised.exception))
        with self.assertRaisesRegex(ProvenanceError, "authorization is unavailable"):
            _api_get_bytes("https://api.github.com", "", "/private")

    def test_artifact_redirects_are_single_hop_https_and_allowlisted(self) -> None:
        for location in (
            "http://productionresultssa0.blob.core.windows.net/actions/9.zip",
            "https://evil.example/actions/9.zip",
            "https://user:password@objects.githubusercontent.com/actions/9.zip",
            "https://objects.githubusercontent.com",
        ):
            with self.subTest(location=location):
                with patch(
                    "tools.platform_workflow_provenance._open_no_redirect",
                    return_value=self._Response(b"", status=302, headers={"Location": location}),
                ):
                    with self.assertRaises(ProvenanceError):
                        _api_get_bytes("https://api.github.com", "token", "/private")

        with patch(
            "tools.platform_workflow_provenance._open_no_redirect",
            side_effect=[
                self._Response(
                    b"",
                    status=302,
                    headers={"Location": "https://productionresultssa0.blob.core.windows.net/actions/9.zip"},
                ),
                self._Response(
                    b"",
                    status=302,
                    headers={"Location": "https://objects.githubusercontent.com/actions/9.zip"},
                ),
            ],
        ) as opened:
            with self.assertRaises(ProvenanceError):
                _api_get_bytes("https://api.github.com", "token", "/private")
        self.assertEqual(opened.call_count, 2)

    def test_paginated_collections_require_stable_total_exact_cardinality_and_unique_names(self) -> None:
        page_one_rows = [{"id": index, "name": f"artifact-{index}"} for index in range(1, 101)]
        page_two_rows = [{"id": 101, "name": "artifact-101"}]
        page_one = {"total_count": 101, "artifacts": page_one_rows}
        page_two = {"total_count": 101, "artifacts": page_two_rows}
        with patch(
            "tools.platform_workflow_provenance._api_get",
            side_effect=[page_one, page_two],
        ):
            self.assertEqual(
                _api_paginate("https://api.github.com", "token", "/artifacts", "artifacts"),
                page_one_rows + page_two_rows,
            )

        cases = (
            [
                {"total_count": 2, "artifacts": [{"id": 1, "name": "one"}]},
            ],
            [
                {"total_count": 2, "artifacts": [{"id": 1, "name": "one"}]},
                {"total_count": 2, "artifacts": [{"id": 2, "name": "one"}]},
            ],
            [
                {"total_count": 101, "artifacts": page_one_rows},
                {"total_count": 100, "artifacts": page_two_rows},
            ],
        )
        for responses in cases:
            with self.subTest(responses=responses):
                with patch("tools.platform_workflow_provenance._api_get", side_effect=responses):
                    with self.assertRaises(ProvenanceError):
                        _api_paginate("https://api.github.com", "token", "/artifacts", "artifacts")

        first_snapshot = {"total_count": 1, "artifacts": [{"id": 1, "name": "one"}]}
        changed_snapshot = {"total_count": 2, "artifacts": [{"id": 1, "name": "one"}]}
        with patch(
            "tools.platform_workflow_provenance._api_get",
            side_effect=[first_snapshot, changed_snapshot],
        ):
            with self.assertRaisesRegex(ProvenanceError, "count changed|incomplete"):
                _stable_api_snapshot("https://api.github.com", "token", "/artifacts", "artifacts")

    def test_preflight_rejects_future_or_tied_deployment_markers(self) -> None:
        workflow, run, _jobs, _statuses = self._payload()
        jobs = [
            {
                "id": 9001,
                "run_id": 1234,
                "run_attempt": 2,
                "head_sha": self.SHA,
                "name": "Deploy production",
                "status": "completed",
                "conclusion": "skipped",
            },
            {
                "id": 9002,
                "run_id": 1234,
                "run_attempt": 2,
                "head_sha": self.SHA,
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
