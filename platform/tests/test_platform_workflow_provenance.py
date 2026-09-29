from __future__ import annotations

import copy
from datetime import datetime, timezone
import sys
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1] / "tools"
WORKFLOW_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
sys.path.insert(0, str(TOOLS))

from tools.platform_workflow_provenance import (  # noqa: E402
    DEPLOY_WORKFLOW_NAME,
    DEPLOY_WORKFLOW_PATH,
    DEPLOY_STATUS_CONTEXT,
    ProvenanceError,
    _payload_rows,
    classify_deployment_run,
    deployment_display_title,
    deployment_job_rows,
    deployment_run_rows,
    deployment_snapshot_digest,
    expected_dispatch_key,
    latest_context_status,
    parse_dispatch_response,
    select_new_deployment_run,
    validate_autodeploy_caller_run,
    validate_deployment_job_success,
    validate_deployment_event,
    validate_deployment_marker,
)


class WorkflowProvenanceTests(unittest.TestCase):
    SHA = "a" * 40
    DISPATCH_KEY = expected_dispatch_key(7001, 1, SHA)
    LIVE_SHA = "b13e9d31b789ad91f95a83b4fd4176a1a0278802"
    LIVE_RUN_ID = 36618681426
    LIVE_WORKFLOW_ID = 339062690

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

    def _listing_row(
        self,
        *,
        run_id: int = 1234,
        attempt: int = 1,
        created_at: str = "2026-09-19T10:00:01Z",
        status: str = "queued",
        conclusion: str | None = None,
        dispatch_key: str | None = None,
    ) -> dict[str, object]:
        return {
            "id": run_id,
            "workflow_id": 77,
            "name": DEPLOY_WORKFLOW_NAME,
            "path": DEPLOY_WORKFLOW_PATH,
            "run_attempt": attempt,
            "event": "workflow_dispatch",
            "head_branch": "dev",
            "head_sha": self.SHA,
            "status": status,
            "conclusion": conclusion,
            "created_at": created_at,
            "display_title": deployment_display_title(dispatch_key or self.DISPATCH_KEY),
            "repository": {
                "full_name": "StrayForest/old_sparky",
                "name": "old_sparky",
                "owner": {"login": "StrayForest"},
            },
        }

    def _downstream_run(
        self,
        *,
        status: str = "queued",
        conclusion: str | None = None,
        run_id: int = 1234,
        attempt: int = 1,
    ) -> tuple[dict[str, object], dict[str, object]]:
        workflow, run, _jobs, _statuses = self._payload()
        run = copy.deepcopy(run)
        run.update(
            {
                "id": run_id,
                "run_attempt": attempt,
                "status": status,
                "conclusion": conclusion,
            }
        )
        return workflow, run

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

    def test_downstream_run_discovery_handles_visibility_race_and_exact_snapshot(self) -> None:
        empty = {"total_count": 0, "workflow_runs": []}
        self.assertIsNone(
            select_new_deployment_run(
                empty,
                prior_run_ids=set(),
                dispatch_key=self.DISPATCH_KEY,
                expected_target_sha=self.SHA,
                expected_workflow_id=77,
            )
        )
        visible = {
            "total_count": 1,
            "workflow_runs": [self._listing_row()],
        }
        self.assertEqual(
            select_new_deployment_run(
                visible,
                prior_run_ids=set(),
                dispatch_key=self.DISPATCH_KEY,
                expected_target_sha=self.SHA,
                expected_workflow_id=77,
            ),
            (1234, 1),
        )
        # A run in the pre-dispatch snapshot is not selected again.
        self.assertIsNone(
            select_new_deployment_run(
                visible,
                prior_run_ids={1234},
                dispatch_key=self.DISPATCH_KEY,
                expected_target_sha=self.SHA,
                expected_workflow_id=77,
            )
        )

    def test_downstream_run_discovery_rejects_duplicate_or_stale_candidates(self) -> None:
        duplicate = {
            "total_count": 2,
            "workflow_runs": [
                self._listing_row(run_id=1234),
                self._listing_row(run_id=1235, created_at="2026-09-19T10:00:02Z"),
            ],
        }
        with self.assertRaisesRegex(ProvenanceError, "ambiguous"):
            select_new_deployment_run(
                duplicate,
                prior_run_ids=set(),
                dispatch_key=self.DISPATCH_KEY,
                expected_target_sha=self.SHA,
                expected_workflow_id=77,
            )
        wrong_key = {
            "total_count": 1,
            "workflow_runs": [
                self._listing_row(
                    created_at="2026-09-19T09:59:59Z",
                    dispatch_key=expected_dispatch_key(7002, 1, self.SHA),
                )
            ],
        }
        self.assertIsNone(
            select_new_deployment_run(
                wrong_key,
                prior_run_ids=set(),
                dispatch_key=self.DISPATCH_KEY,
                expected_target_sha=self.SHA,
                expected_workflow_id=77,
            )
        )

    def test_live_run_fixture_uses_the_real_workflow_path(self) -> None:
        # This row is copied from live run 36618681426.  GitHub's API exposes
        # the workflow path without an @ref suffix.
        live = self._listing_row(
            run_id=self.LIVE_RUN_ID,
            attempt=1,
            status="completed",
            conclusion="failure",
        )
        live.update(
            {
                "workflow_id": self.LIVE_WORKFLOW_ID,
                "head_sha": self.LIVE_SHA,
                "display_title": "Platform production deploy",
            }
        )
        rows = deployment_run_rows(
            {
                "total_count": 1,
                "workflow_runs": [live],
            },
            expected_target_sha=self.LIVE_SHA,
            expected_workflow_id=self.LIVE_WORKFLOW_ID,
        )
        self.assertEqual(rows[0]["path"], DEPLOY_WORKFLOW_PATH)
        wrong_path = {**live, "path": f"{DEPLOY_WORKFLOW_PATH}@refs/heads/dev"}
        with self.assertRaises(ProvenanceError):
            deployment_run_rows(
                {"total_count": 1, "workflow_runs": [wrong_path]},
                expected_target_sha=self.LIVE_SHA,
                expected_workflow_id=self.LIVE_WORKFLOW_ID,
            )

    def test_dispatch_200_schema_is_exact_and_204_is_not_a_success_payload(self) -> None:
        run_url = "https://api.github.com/repos/StrayForest/old_sparky/actions/runs/36618681426"
        html_url = "https://github.com/StrayForest/old_sparky/actions/runs/36618681426"
        self.assertEqual(
            parse_dispatch_response(
                {
                    "workflow_run_id": self.LIVE_RUN_ID,
                    "run_url": run_url,
                    "html_url": html_url,
                }
            ),
            (self.LIVE_RUN_ID, None, html_url),
        )
        with self.assertRaises(ProvenanceError):
            parse_dispatch_response({})
        with self.assertRaises(ProvenanceError):
            parse_dispatch_response(
                {
                    "workflow_run_id": self.LIVE_RUN_ID,
                    "run_url": run_url,
                    "html_url": html_url,
                    "unexpected": True,
                }
            )
        with self.assertRaises(ProvenanceError):
            parse_dispatch_response(
                {
                    "workflow_run_id": self.LIVE_RUN_ID,
                    "run_url": run_url,
                    "html_url": html_url,
                    "run_attempt": 2,
                }
            )
        with self.assertRaises(ProvenanceError):
            parse_dispatch_response(None)

    def test_dispatch_key_reconciliation_ignores_clock_skew_and_rejects_attempt_two(self) -> None:
        # Deliberately put the candidate on either side of an arbitrary clock
        # boundary.  Selection is by the API-visible key, never timestamps.
        for created_at in ("2020-01-01T00:00:00Z", "2099-12-31T23:59:59Z"):
            with self.subTest(created_at=created_at):
                selected = select_new_deployment_run(
                    {
                        "total_count": 1,
                        "workflow_runs": [self._listing_row(created_at=created_at)],
                    },
                    prior_run_ids=set(),
                    dispatch_key=self.DISPATCH_KEY,
                    expected_target_sha=self.SHA,
                    expected_workflow_id=77,
                )
                self.assertEqual(selected, (1234, 1))
        same_second_manual = self._listing_row(
            run_id=1235,
            created_at="2026-09-19T10:00:01Z",
            dispatch_key=expected_dispatch_key(7002, 1, self.SHA),
        )
        same_second_manual["display_title"] = DEPLOY_WORKFLOW_NAME
        self.assertEqual(
            select_new_deployment_run(
                {
                    "total_count": 2,
                    "workflow_runs": [self._listing_row(), same_second_manual],
                },
                prior_run_ids=set(),
                dispatch_key=self.DISPATCH_KEY,
                expected_target_sha=self.SHA,
                expected_workflow_id=77,
            ),
            (1234, 1),
        )
        paginated_runs = [self._listing_row()]
        paginated_runs.extend(
            {
                **self._listing_row(run_id=run_id),
                "display_title": DEPLOY_WORKFLOW_NAME,
            }
            for run_id in range(2000, 2101)
        )
        self.assertEqual(
            select_new_deployment_run(
                {"total_count": len(paginated_runs), "workflow_runs": paginated_runs},
                prior_run_ids=set(),
                dispatch_key=self.DISPATCH_KEY,
                expected_target_sha=self.SHA,
                expected_workflow_id=77,
            ),
            (1234, 1),
        )
        attempt_two = self._listing_row(attempt=2)
        with self.assertRaisesRegex(ProvenanceError, "attempt"):
            select_new_deployment_run(
                {"total_count": 1, "workflow_runs": [attempt_two]},
                prior_run_ids=set(),
                dispatch_key=self.DISPATCH_KEY,
                expected_target_sha=self.SHA,
                expected_workflow_id=77,
            )

    def test_auto_deploy_caller_lease_is_bound_and_active(self) -> None:
        workflow = {
            "id": 88,
            "path": ".github/workflows/platform-production-autodeploy.yml",
            "name": "Platform production auto-deploy",
        }
        run_id = 7001
        base = f"https://github.com/StrayForest/old_sparky/actions/runs/{run_id}"
        run = {
            "id": run_id,
            "workflow_id": 88,
            "name": "Platform production auto-deploy",
            "run_attempt": 1,
            "event": "workflow_run",
            "head_branch": "dev",
            "head_sha": self.SHA,
            "status": "in_progress",
            "conclusion": None,
            "html_url": base,
            "repository": {
                "full_name": "StrayForest/old_sparky",
                "name": "old_sparky",
                "owner": {"login": "StrayForest"},
            },
        }
        self.assertEqual(
            validate_autodeploy_caller_run(
                workflow,
                run,
                expected_run_id=run_id,
                expected_attempt=1,
                expected_target_sha=self.SHA,
                dispatch_key=self.DISPATCH_KEY,
            ),
            f"{base}/attempts/1",
        )
        for mutation in (
            {"status": "completed", "conclusion": "cancelled"},
            {"run_attempt": 2},
            {"head_sha": "b" * 40},
        ):
            with self.subTest(mutation=mutation):
                candidate = {**run, **mutation}
                with self.assertRaises(ProvenanceError):
                    validate_autodeploy_caller_run(
                        workflow,
                        candidate,
                        expected_run_id=run_id,
                        expected_attempt=1,
                        expected_target_sha=self.SHA,
                        dispatch_key=self.DISPATCH_KEY,
                    )

    def test_exact_attempt_jobs_require_one_successful_deploy_job(self) -> None:
        def job(job_id: int, name: str, *, conclusion: str = "success") -> dict[str, object]:
            return {
                "id": job_id,
                "run_id": 1234,
                "run_attempt": 1,
                "name": name,
                "workflow_name": DEPLOY_WORKFLOW_NAME,
                "head_branch": "dev",
                "head_sha": self.SHA,
                "status": "completed",
                "conclusion": conclusion,
            }

        deploy = job(1, "Deploy production")
        preflight = job(2, "Production preflight", conclusion="skipped")
        payload = {"total_count": 2, "jobs": [deploy, preflight]}
        rows = deployment_job_rows(payload, expected_run_id=1234, expected_attempt=1)
        validate_deployment_job_success(
            rows,
            expected_run_id=1234,
            expected_attempt=1,
            expected_target_sha=self.SHA,
        )
        paginated_jobs = [deploy]
        paginated_jobs.extend(
            job(index, f"Auxiliary job {index}", conclusion="success")
            for index in range(3, 104)
        )
        paginated_rows = deployment_job_rows(
            {"total_count": len(paginated_jobs), "jobs": paginated_jobs},
            expected_run_id=1234,
            expected_attempt=1,
        )
        validate_deployment_job_success(
            paginated_rows,
            expected_run_id=1234,
            expected_attempt=1,
            expected_target_sha=self.SHA,
        )
        skipped = {**deploy, "conclusion": "skipped"}
        with self.assertRaises(ProvenanceError):
            validate_deployment_job_success(
                [skipped],
                expected_run_id=1234,
                expected_attempt=1,
                expected_target_sha=self.SHA,
            )
        with self.assertRaisesRegex(ProvenanceError, "ambiguous"):
            validate_deployment_job_success(
                [deploy, {**deploy, "id": 3}],
                expected_run_id=1234,
                expected_attempt=1,
                expected_target_sha=self.SHA,
            )
        with self.assertRaises(ProvenanceError):
            deployment_job_rows(
                {"total_count": 2, "jobs": [deploy]},
                expected_run_id=1234,
                expected_attempt=1,
            )
        with self.assertRaises(ProvenanceError):
            deployment_job_rows(
                {"total_count": 2, "jobs": [deploy, {**preflight, "id": 1}]},
                expected_run_id=1234,
                expected_attempt=1,
            )
        with self.assertRaises(ProvenanceError):
            deployment_job_rows(
                {"total_count": 1, "jobs": [{**deploy, "run_attempt": 2}]},
                expected_run_id=1234,
                expected_attempt=1,
            )

    def test_downstream_listing_rejects_wrong_sha_event_workflow_repository_and_api_shape(self) -> None:
        mutations = {
            "sha": {"head_sha": "b" * 40},
            "event": {"event": "push"},
            "branch": {"head_branch": "main"},
            "workflow": {"workflow_id": 78},
            "name": {"name": "Other workflow"},
            "path": {"path": ".github/workflows/other.yml@dev"},
            "repository": {"repository": {"full_name": "attacker/repo"}},
            "rate-limit-shaped": {"message": "API rate limit exceeded"},
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                row = self._listing_row()
                row.update(mutation)
                payload = (
                    mutation
                    if label == "rate-limit-shaped"
                    else {"total_count": 1, "workflow_runs": [row]}
                )
                with self.assertRaises(ProvenanceError):
                    select_new_deployment_run(
                        payload,
                        prior_run_ids=set(),
                        dispatch_key=self.DISPATCH_KEY,
                        expected_target_sha=self.SHA,
                        expected_workflow_id=77,
                    )
        with self.assertRaises(ProvenanceError):
            deployment_run_rows(
                {"total_count": 1, "workflow_runs": None},
                expected_target_sha=self.SHA,
                expected_workflow_id=77,
            )
        with self.assertRaises(ProvenanceError):
            deployment_run_rows(
                {"total_count": True, "workflow_runs": [self._listing_row()]},
                expected_target_sha=self.SHA,
                expected_workflow_id=77,
            )

    def test_downstream_terminal_conclusions_and_attempt_mutations_fail_closed(self) -> None:
        workflow, run = self._downstream_run(status="in_progress")
        for status in ("queued", "requested", "waiting", "pending", "in_progress"):
            with self.subTest(status=status):
                candidate = copy.deepcopy(run)
                candidate["status"] = status
                candidate["conclusion"] = None
                self.assertEqual(
                    classify_deployment_run(
                        workflow,
                        candidate,
                        expected_run_id=1234,
                        expected_attempt=1,
                        expected_target_sha=self.SHA,
                    ),
                    "pending",
                )
        for conclusion in (
            "failure",
            "cancelled",
            "timed_out",
            "action_required",
            "stale",
            "neutral",
            "skipped",
            "startup_failure",
        ):
            with self.subTest(conclusion=conclusion):
                candidate = copy.deepcopy(run)
                candidate["status"] = "completed"
                candidate["conclusion"] = conclusion
                self.assertEqual(
                    classify_deployment_run(
                        workflow,
                        candidate,
                        expected_run_id=1234,
                        expected_attempt=1,
                        expected_target_sha=self.SHA,
                    ),
                    f"terminal:{conclusion}",
                )
        successful = copy.deepcopy(run)
        successful["status"] = "completed"
        successful["conclusion"] = "success"
        self.assertEqual(
            classify_deployment_run(
                workflow,
                successful,
                expected_run_id=1234,
                expected_attempt=1,
                expected_target_sha=self.SHA,
            ),
            "success",
        )
        for mutation in (
            {"run_attempt": 2},
            {"workflow_id": 77.0},
            {"status": "completed", "conclusion": "mystery"},
            {"status": []},
            {"status": "unknown", "conclusion": None},
            {"status": "in_progress", "conclusion": "cancelled"},
        ):
            with self.subTest(mutation=mutation):
                candidate = copy.deepcopy(run)
                candidate.update(mutation)
                with self.assertRaises(ProvenanceError):
                    classify_deployment_run(
                        workflow,
                        candidate,
                        expected_run_id=1234,
                        expected_attempt=1,
                        expected_target_sha=self.SHA,
                    )

    def test_auto_deploy_downstream_contract_is_bounded_and_no_retry(self) -> None:
        source = (WORKFLOW_DIR / "platform-production-autodeploy.yml").read_text(
            encoding="utf-8"
        )
        for marker in (
            'DOWNSTREAM_MAX_POLLS: "60"',
            'DOWNSTREAM_POLL_SECONDS: "10"',
            'DOWNSTREAM_TIMEOUT_SECONDS: "600"',
            "for ((poll=1; poll<=DOWNSTREAM_MAX_POLLS; poll++))",
            '--connect-timeout 10 --max-time "$max_time" --max-filesize 4194304',
            "select_new_deployment_run",
            "classify_deployment_run",
            "return_run_details",
            '"dispatch_key"',
            "dispatch_unknown",
            '[[ "$dispatch_http_status" == "204" ]]',
            '[[ "$dispatch_http_status" == "200" ]]',
            "force-cancel",
            "/attempts/${attempt}/jobs?per_page=100&page=${page}",
            "deployment_job_rows",
            "validate_deployment_job_success",
            "result=timeout",
            "result=ambiguous",
            "no rerun is attempted",
            "terminal:cancelled",
            "terminal:timed_out",
            "terminal:action_required",
            "terminal:stale",
            "steps.downstream.outputs.result",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("--retry", source)
        self.assertNotIn("dispatch_started_at", source)
        self.assertNotIn("platform-production-deploy.yml@dev", source)
        self.assertIn("429|500|502|503|504) return 75", source)
        self.assertIn("*) return 1", source)
        self.assertIn('dispatch_response_state="dispatch_unknown"', source)
        self.assertIn("/actions/runs/${selected_id}/cancel", source)
        self.assertIn("/actions/runs/${selected_id}/force-cancel", source)
        self.assertIn("if: ${{ steps.gate.outputs.deploy == 'true' }}", source)
        self.assertIn('echo "deploy=false" >> "$GITHUB_OUTPUT"', source)

    def test_child_dispatch_key_lease_is_visible_and_precedes_production_secrets(self) -> None:
        source = (WORKFLOW_DIR / "platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("run-name: Platform production deploy [${{ inputs.dispatch_key || 'manual' }}]", source)
        self.assertIn("dispatch_key:", source)
        self.assertIn("autodeploy_run_id:", source)
        self.assertIn("autodeploy_run_attempt:", source)
        lease = source.index("Revalidate active auto-deploy caller lease before production secrets")
        secrets = source.rindex("Validate deployment secrets")
        self.assertLess(lease, secrets)
        self.assertIn("validate_autodeploy_caller_run", source)
        self.assertIn("platform-production-autodeploy.yml", source)

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
