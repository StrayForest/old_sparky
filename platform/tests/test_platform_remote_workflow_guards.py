"""Static and adversarial contracts for production SSH workflow boundaries."""

from __future__ import annotations

import json
import hashlib
import io
import re
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import textwrap
import unittest
import zipfile
import warnings


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLATFORM_ROOT.parent
WORKFLOW_ROOT = REPO_ROOT / ".github/workflows"
sys.path.insert(0, str(PLATFORM_ROOT))

from tools.platform_workflow_input_guard import (  # noqa: E402
    WorkflowInputError,
    _write_private_json,
    validate_control_email,
    validate_target_sha,
    validate_utc_timestamp,
)
from tools import (  # noqa: E402
    platform_media_migration_diagnostics_summary,
    platform_prepare_artifact_dir,
    platform_live_qa_sequence,
)


WORKFLOWS = {
    "as12": "platform-production-as12-proof.yml",
    "runtime": "platform-production-web-runtime-diagnostics.yml",
    "profile": "platform-production-profile-review-fixture.yml",
    "live": "platform-live-user-qa.yml",
    "translation": "platform-patch-translation-qa.yml",
}


class AutomaticLiveQASequenceContractTests(unittest.TestCase):
    def test_parent_handoff_authorization_binds_both_runs_and_deploy_mode(self) -> None:
        target = "a" * 40
        slug = "gha-12-2-aaaaaaaaaaaa"
        document = {
            "schema": 1,
            "kind": platform_live_qa_sequence.HANDOFF_AUTH_KIND,
            "repository": "StrayForest/old_sparky",
            "deploy_workflow_path": platform_live_qa_sequence.DEPLOY_WORKFLOW_PATH,
            "deploy_run_id": "12",
            "deploy_run_attempt": "2",
            "source_sha": target,
            "mode": "deploy",
            "release_slug": slug,
            "sequence_workflow_path": platform_live_qa_sequence.SEQUENCER_WORKFLOW_PATH,
            "sequence_workflow_id": 77,
            "sequence_run_id": "90",
            "sequence_run_attempt": "1",
        }
        kwargs = {
            "repository": "StrayForest/old_sparky",
            "parent_run_id": 12,
            "parent_attempt": 2,
            "target_sha": target,
            "release_slug": slug,
            "sequence_run_id": 90,
            "sequence_attempt": 1,
            "sequence_workflow_id": 77,
        }
        self.assertEqual(
            platform_live_qa_sequence.validate_handoff_authorization(document, **kwargs),
            document,
        )
        for changes in (
            {"deploy_run_id": "13"},
            {"deploy_run_attempt": "1"},
            {"source_sha": "b" * 40},
            {"mode": "baseline-reconcile"},
            {"sequence_run_id": "91"},
            {"sequence_run_attempt": "2"},
            {"sequence_workflow_id": 78},
            {"unexpected": "field"},
        ):
            with self.subTest(changes=changes), self.assertRaises(
                platform_live_qa_sequence.SequenceError
            ):
                platform_live_qa_sequence.validate_handoff_authorization(
                    {**document, **changes}, **kwargs
                )

    def test_handoff_requires_completed_deploy_job_and_active_parent(self) -> None:
        class FakeActions(platform_live_qa_sequence.GitHubActions):
            def __init__(self, *, run_changes=None, jobs=None, mode="deploy"):
                self.repository = "StrayForest/old_sparky"
                self.run_changes = run_changes or {}
                self.jobs = jobs or [{
                    "name": "Deploy production", "status": "completed", "conclusion": "success",
                }]
                self.mode = mode

            def workflow_id(self, path: str) -> int:
                if path != platform_live_qa_sequence.DEPLOY_WORKFLOW_PATH:
                    raise AssertionError(path)
                return 19

            def request(self, _method: str, path: str) -> object:
                if path == "/actions/workflows/platform-production-deploy.yml":
                    return {"id": 19, "path": platform_live_qa_sequence.DEPLOY_WORKFLOW_PATH, "state": "active"}
                if path == "/actions/runs/12":
                    return {
                        "id": 12, "run_attempt": 2, "workflow_id": 19,
                        "path": platform_live_qa_sequence.DEPLOY_WORKFLOW_PATH,
                        "event": "workflow_dispatch", "head_branch": "dev",
                        "head_sha": "a" * 40, "name": platform_live_qa_sequence.DEPLOY_WORKFLOW_NAME,
                        "display_title": platform_live_qa_sequence.DEPLOY_WORKFLOW_NAME,
                        "status": "in_progress", "conclusion": None,
                        **self.run_changes,
                    }
                if path == "/actions/runs/12/attempts/2/jobs?per_page=100&page=1":
                    return {"total_count": len(self.jobs), "jobs": self.jobs}
                raise AssertionError(path)

            def exact_deployment_receipt(self, **_kwargs: object) -> dict[str, object]:
                return {
                    "verified_mode": self.mode,
                    "release_slug": "gha-12-2-aaaaaaaaaaaa",
                }

            def current_dev_sha(self) -> str:
                return "a" * 40

        good = FakeActions()
        self.assertEqual(
            good.verify_parent_handoff_ready(parent_id=12, attempt=2, target_sha="a" * 40),
            (19, "gha-12-2-aaaaaaaaaaaa"),
        )
        cases = (
            (FakeActions(run_changes={"status": "completed", "conclusion": "success"}), "parent_run_identity_invalid"),
            (FakeActions(run_changes={"id": True}), "parent_run_identity_invalid"),
            (FakeActions(run_changes={"workflow_id": True}), "parent_run_identity_invalid"),
            (FakeActions(jobs=[{"name": "Deploy production", "status": "in_progress"}]), "production_job_not_successful"),
            (FakeActions(jobs=[{"name": "Deploy production", "status": "completed", "conclusion": "failure"}]), "production_job_not_successful"),
            (FakeActions(jobs=[None]), "parent_jobs_invalid"),
            (FakeActions(mode="baseline-reconcile"), "mode_not_normal_deploy"),
        )
        for fake, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(
                (platform_live_qa_sequence.SequenceError, platform_live_qa_sequence.SequenceSkipped)
            ) as caught:
                fake.verify_parent_handoff_ready(parent_id=12, attempt=2, target_sha="a" * 40)
            self.assertEqual(caught.exception.reason, reason)

    def test_dispatch_handoff_binds_acknowledged_child_to_parent_artifact(self) -> None:
        from unittest.mock import patch

        class FakeActions:
            repository = "StrayForest/old_sparky"

            def __init__(self) -> None:
                self.dispatch_args = None

            def verify_parent_handoff_ready(self, **kwargs: object) -> tuple[int, str]:
                self.assert_parent = kwargs
                return 19, "gha-12-2-aaaaaaaaaaaa"

            def workflow_id(self, path: str) -> int:
                if path != platform_live_qa_sequence.SEQUENCER_WORKFLOW_PATH:
                    raise AssertionError(path)
                return 77

            def dispatch(self, path: str, workflow_id: int, inputs: dict[str, str]) -> int:
                self.dispatch_args = (path, workflow_id, inputs)
                return 90

        fake = FakeActions()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "output"
            output.touch()
            env = {
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/dev",
                "GITHUB_REPOSITORY": "StrayForest/old_sparky",
                "GITHUB_RUN_ID": "12",
                "GITHUB_RUN_ATTEMPT": "2",
                "GITHUB_SHA": "a" * 40,
                "GITHUB_API_URL": "https://api.github.com",
                "GH_TOKEN": "test-token",
                "WORKFLOW_MODE": "deploy",
                "RUNNER_TEMP": temp_dir,
                "GITHUB_OUTPUT": str(output),
            }
            with patch.dict("os.environ", env, clear=True), patch.object(
                platform_live_qa_sequence, "GitHubActions", return_value=fake
            ):
                self.assertEqual(platform_live_qa_sequence.dispatch_handoff(), 90)
            self.assertEqual(
                fake.dispatch_args,
                (
                    platform_live_qa_sequence.SEQUENCER_WORKFLOW_PATH,
                    77,
                    {"deploy_run_id": "12", "deploy_run_attempt": "2", "target_sha": "a" * 40},
                ),
            )
            receipt_path = Path(temp_dir) / "postdeploy-handoff-authorization" / platform_live_qa_sequence.HANDOFF_AUTH_FILE
            mode = platform_live_qa_sequence.stat.S_IMODE(receipt_path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            document = json.loads(receipt_path.read_text(encoding="ascii"))
            self.assertEqual(document["sequence_run_id"], "90")
            self.assertEqual(document["deploy_run_attempt"], "2")
            self.assertEqual(document["mode"], "deploy")
            self.assertIn("receipt_path=", output.read_text(encoding="ascii"))

    def test_public_dispatch_waits_for_terminal_success_of_parent_workflow(self) -> None:
        from unittest.mock import patch

        class FakeActions:
            repository = "StrayForest/old_sparky"

            def __init__(self, *, authorization_available: bool = True) -> None:
                self.dispatched: list[str] = []
                self.calls: list[str] = []
                self.authorization_available = authorization_available

            def exact_deployment_receipt(self, **_kwargs: object) -> dict[str, object]:
                self.calls.append("deploy_receipt")
                return {"verified_mode": "deploy", "release_slug": "gha-12-2-aaaaaaaaaaaa"}

            def workflow_id(self, _path: str) -> int:
                return 77

            def exact_handoff_authorization(self, **_kwargs: object) -> dict[str, object]:
                self.calls.append("authorization")
                if not self.authorization_available:
                    raise platform_live_qa_sequence.SequenceError(
                        "authorization", "artifact_unavailable"
                    )
                return {"sequence_run_id": "90"}

            def wait_for_parent_success(self, **_kwargs: object) -> None:
                self.calls.append("parent_wait")
                raise platform_live_qa_sequence.SequenceError("parent", "run_not_successful")

            def dispatch(self, path: str, **_kwargs: object) -> int:
                self.dispatched.append(path)
                return 431

        env = {
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/dev",
            "GITHUB_REPOSITORY": "StrayForest/old_sparky", "GITHUB_SHA": "a" * 40,
            "GITHUB_RUN_ID": "90", "GITHUB_RUN_ATTEMPT": "1",
            "DEPLOY_TARGET_SHA": "a" * 40, "DEPLOY_RUN_ID": "12", "DEPLOY_RUN_ATTEMPT": "2",
        }
        fake = FakeActions()
        with patch.dict("os.environ", env, clear=True), patch.object(
            platform_live_qa_sequence, "GitHubActions", return_value=fake
        ), self.assertRaises(platform_live_qa_sequence.SequenceError) as caught:
            platform_live_qa_sequence.dispatch_public()
        self.assertEqual(caught.exception.reason, "run_not_successful")
        self.assertEqual(fake.calls, ["deploy_receipt", "authorization", "parent_wait"])
        self.assertEqual(fake.dispatched, [])

        missing_receipt = FakeActions(authorization_available=False)
        with patch.dict("os.environ", env, clear=True), patch.object(
            platform_live_qa_sequence, "GitHubActions", return_value=missing_receipt
        ), self.assertRaises(platform_live_qa_sequence.SequenceError) as caught:
            platform_live_qa_sequence.dispatch_public()
        self.assertEqual(caught.exception.reason, "artifact_unavailable")
        self.assertEqual(missing_receipt.calls, ["deploy_receipt", "authorization"])
        self.assertEqual(missing_receipt.dispatched, [])

    def test_dispatch_ack_requires_exact_documented_run_identity(self) -> None:
        self.assertEqual(
            platform_live_qa_sequence.parse_dispatch_ack(
                {
                    "workflow_run_id": 431,
                    "run_url": "https://api.github.com/repos/StrayForest/old_sparky/actions/runs/431",
                    "html_url": "https://github.com/StrayForest/old_sparky/actions/runs/431",
                }
            ),
            431,
        )
        for invalid in (
            {},
            {
                "workflow_run_id": 431,
                "run_url": "https://api.github.com/repos/StrayForest/old_sparky/actions/runs/431?token=x",
                "html_url": "https://github.com/StrayForest/old_sparky/actions/runs/431",
            },
            {
                "workflow_run_id": 431,
                "run_url": "https://api.github.com/repos/StrayForest/old_sparky/actions/runs/432",
                "html_url": "https://github.com/StrayForest/old_sparky/actions/runs/431",
            },
            {
                "workflow_run_id": 431,
                "run_url": "https://api.github.com/repos/StrayForest/old_sparky/actions/runs/431",
                "html_url": "https://github.com/StrayForest/old_sparky/actions/runs/431",
                "unexpected": True,
            },
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                platform_live_qa_sequence.SequenceError
            ):
                platform_live_qa_sequence.parse_dispatch_ack(invalid)

    def test_child_run_must_bind_exact_dispatch_target(self) -> None:
        valid = {
            "id": 431,
            "run_attempt": 1,
            "workflow_id": 55,
            "head_sha": "a" * 40,
            "head_branch": "dev",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
        }
        self.assertEqual(
            platform_live_qa_sequence.validate_run_identity(
                valid, run_id=431, workflow_id=55, target_sha="a" * 40
            ),
            ("completed", "success"),
        )
        for changes in (
            {"head_sha": "b" * 40},
            {"head_branch": "main"},
            {"event": "push"},
            {"run_attempt": 2},
            {"conclusion": "failure"},
        ):
            with self.subTest(changes=changes), self.assertRaises(
                platform_live_qa_sequence.SequenceError
            ):
                platform_live_qa_sequence.validate_run_identity(
                    {**valid, **changes}, run_id=431, workflow_id=55, target_sha="a" * 40
                )

    def test_required_jobs_must_all_be_completed_successfully(self) -> None:
        expected = ("public", "report")
        good = {
            "total_count": 2,
            "jobs": [
                {"name": "public", "status": "completed", "conclusion": "success"},
                {"name": "report", "status": "completed", "conclusion": "success"},
            ],
        }
        platform_live_qa_sequence.validate_required_jobs(good, expected)
        for bad in (
            {"total_count": 1, "jobs": good["jobs"]},
            {
                "total_count": 2,
                "jobs": [good["jobs"][0], {**good["jobs"][1], "conclusion": "skipped"}],
            },
            {"total_count": 2, "jobs": [*good["jobs"], good["jobs"][0]]},
        ):
            with self.subTest(bad=bad), self.assertRaises(
                platform_live_qa_sequence.SequenceError
            ):
                platform_live_qa_sequence.validate_required_jobs(bad, expected)

    def test_child_receipt_binds_role_child_attempt_and_source(self) -> None:
        valid = {
            "schema": 1,
            "kind": "platform-live-qa-child-dispatch",
            "repository": "StrayForest/old_sparky",
            "sequence_workflow_path": ".github/workflows/platform-postdeploy-live-qa.yml",
            "sequence_run_id": "90",
            "sequence_run_attempt": "1",
            "sequence_source_sha": "a" * 40,
            "deploy_run_id": "12",
            "deploy_run_attempt": "2",
            "source_sha": "a" * 40,
            "role": "public",
            "child_workflow_path": ".github/workflows/platform-live-launch.yml",
            "child_workflow_id": 51,
            "child_run_id": "431",
            "child_run_attempt": "1",
            "expected_target_sha": "a" * 40,
            "public_run_id": "431",
            "marker": platform_live_qa_sequence._marker("12", "2", "a" * 40),
        }
        args = {
            "repository": "StrayForest/old_sparky",
            "role": "public",
            "sequence_run_id": 90,
            "sequence_attempt": 1,
            "sequence_source_sha": "a" * 40,
            "deploy_run_id": 12,
            "deploy_attempt": 2,
            "child_run_id": 431,
            "child_attempt": 1,
            "child_workflow_id": 51,
            "target_sha": "a" * 40,
        }
        self.assertEqual(platform_live_qa_sequence.validate_child_receipt(valid, **args), valid)
        for changes in (
            {"role": "user-qa"},
            {"child_run_id": "432"},
            {"child_run_attempt": "2"},
            {"sequence_source_sha": "b" * 40},
            {"marker": "liveqa-auto-000000000000000000000000"},
        ):
            with self.subTest(changes=changes), self.assertRaises(platform_live_qa_sequence.SequenceError):
                platform_live_qa_sequence.validate_child_receipt({**valid, **changes}, **args)

    def test_userqa_receipt_binds_exact_public_run_and_marker(self) -> None:
        document = {
            "schema": 1,
            "kind": "platform-live-qa-child-dispatch",
            "repository": "StrayForest/old_sparky",
            "sequence_workflow_path": ".github/workflows/platform-postdeploy-live-qa.yml",
            "sequence_run_id": "90",
            "sequence_run_attempt": "1",
            "sequence_source_sha": "a" * 40,
            "deploy_run_id": "12",
            "deploy_run_attempt": "2",
            "source_sha": "a" * 40,
            "role": "user-qa",
            "child_workflow_path": ".github/workflows/platform-live-user-qa.yml",
            "child_workflow_id": 52,
            "child_run_id": "432",
            "child_run_attempt": "1",
            "expected_target_sha": "a" * 40,
            "public_run_id": "431",
            "marker": platform_live_qa_sequence._marker("12", "2", "a" * 40),
        }
        self.assertEqual(
            platform_live_qa_sequence.validate_child_receipt(
                document,
                repository="StrayForest/old_sparky",
                role="user-qa",
                sequence_run_id=90,
                sequence_attempt=1,
                sequence_source_sha="a" * 40,
                deploy_run_id=12,
                deploy_attempt=2,
                child_run_id=432,
                child_attempt=1,
                child_workflow_id=52,
                target_sha="a" * 40,
                expected_public_run_id="431",
            )["public_run_id"],
            "431",
        )
        with self.assertRaises(platform_live_qa_sequence.SequenceError):
            platform_live_qa_sequence.validate_child_receipt(
                {**document, "public_run_id": "433"},
                repository="StrayForest/old_sparky",
                role="user-qa",
                sequence_run_id=90,
                sequence_attempt=1,
                sequence_source_sha="a" * 40,
                deploy_run_id=12,
                deploy_attempt=2,
                child_run_id=432,
                child_attempt=1,
                child_workflow_id=52,
                target_sha="a" * 40,
                expected_public_run_id="431",
            )

    def test_child_receipt_artifact_is_source_and_run_bound(self) -> None:
        body = json.dumps(
            {"schema": 1, "kind": "platform-live-qa-child-dispatch"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"

        def make_zip(*members: tuple[str, bytes, int]) -> bytes:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                for name, payload, mode in members:
                    info = zipfile.ZipInfo(name)
                    info.external_attr = mode << 16
                    archive.writestr(info, payload)
            return buffer.getvalue()

        def client_for(*, raw: bytes, archive_override: bytes | None = None,
                       listing_rows: list[dict[str, object]] | None = None,
                       metadata_changes: dict[str, object] | None = None,
                       listing_total: int | None = None):
            archive = archive_override or make_zip(
                (platform_live_qa_sequence.CHILD_RECEIPT_FILE, raw, 0o100600)
            )
            digest = "sha256:" + hashlib.sha256(archive).hexdigest()
            row = {
                "id": 91,
                "name": platform_live_qa_sequence.child_receipt_name("public", 90, 1),
                "expired": False,
                "size_in_bytes": len(archive),
                "digest": digest,
            }
            metadata = {
                "id": 91,
                "name": row["name"],
                "expired": False,
                "size_in_bytes": len(archive),
                "digest": digest,
                "workflow_run": {
                    "id": 90,
                    "run_attempt": None,
                    "head_branch": "dev",
                    "head_sha": "a" * 40,
                },
            }
            metadata.update(metadata_changes or {})

            class FakeActions(platform_live_qa_sequence.GitHubActions):
                def __init__(self) -> None:
                    self.repository = "StrayForest/old_sparky"

                def request(self, _method: str, path: str) -> object:
                    if path == "/actions/runs/90/artifacts?per_page=100&page=1":
                        rows = [row] if listing_rows is None else listing_rows
                        return {"total_count": len(rows) if listing_total is None else listing_total,
                                "artifacts": rows}
                    if path == "/actions/artifacts/91":
                        return metadata
                    raise AssertionError(path)

                def artifact_bytes(self, _path: str) -> bytes:
                    return archive

            return FakeActions()

        good = client_for(raw=body)
        receipt = good.exact_child_receipt(
            sequence_run_id=90, sequence_attempt=1, role="public", target_sha="a" * 40,
        )
        self.assertEqual(receipt["kind"], "platform-live-qa-child-dispatch")

        cases = (
            (client_for(raw=body, listing_rows=[]), "artifact_unavailable"),
            (client_for(raw=body, listing_rows=[
                {"id": 91, "name": platform_live_qa_sequence.child_receipt_name("public", 90, 1)},
                {"id": 92, "name": platform_live_qa_sequence.child_receipt_name("public", 90, 1)},
            ]), "artifact_ambiguous"),
            (client_for(raw=body, metadata_changes={"workflow_run": {
                "id": 90, "run_attempt": "1", "head_branch": "dev", "head_sha": "a" * 40,
            }}), "artifact_metadata_invalid"),
            (client_for(raw=body, metadata_changes={"workflow_run": {
                "id": 90, "run_attempt": None, "head_branch": "dev", "head_sha": "b" * 40,
            }}), "artifact_metadata_invalid"),
            (client_for(raw=body, metadata_changes={"digest": "sha256:" + "0" * 64}), "artifact_metadata_invalid"),
            (client_for(raw=body.replace(b'{"kind":', b'{ "kind":')), "receipt_encoding_invalid"),
        )
        for client, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(
                platform_live_qa_sequence.SequenceError
            ) as caught:
                client.exact_child_receipt(
                    sequence_run_id=90, sequence_attempt=1, role="public", target_sha="a" * 40,
                    timeout_seconds=0,
                )
            self.assertEqual(caught.exception.reason, reason)

        wrong_attempt = client_for(raw=body, metadata_changes={"workflow_run": {
            "id": 90, "run_attempt": 2, "head_branch": "dev", "head_sha": "a" * 40,
        }})
        with self.assertRaises(platform_live_qa_sequence.SequenceError) as caught:
            wrong_attempt.exact_child_receipt(
                sequence_run_id=90, sequence_attempt=1, role="public", target_sha="a" * 40,
                timeout_seconds=0,
            )
        self.assertEqual(caught.exception.reason, "artifact_metadata_invalid")

        extra_member = make_zip(
            (platform_live_qa_sequence.CHILD_RECEIPT_FILE, body, 0o100600),
            ("unexpected.txt", b"x", 0o100600),
        )
        client = client_for(raw=body, archive_override=extra_member)
        with self.assertRaises(platform_live_qa_sequence.SequenceError) as caught:
            client.exact_child_receipt(
                sequence_run_id=90, sequence_attempt=1, role="public", target_sha="a" * 40,
            )
        self.assertEqual(caught.exception.reason, "artifact_member_invalid")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            duplicate_member = make_zip(
                (platform_live_qa_sequence.CHILD_RECEIPT_FILE, body, 0o100600),
                (platform_live_qa_sequence.CHILD_RECEIPT_FILE, body, 0o100600),
            )
        client = client_for(raw=body, archive_override=duplicate_member)
        with self.assertRaises(platform_live_qa_sequence.SequenceError) as caught:
            client.exact_child_receipt(
                sequence_run_id=90, sequence_attempt=1, role="public", target_sha="a" * 40,
            )
        self.assertEqual(caught.exception.reason, "artifact_member_invalid")
        symlink_member = make_zip((
            platform_live_qa_sequence.CHILD_RECEIPT_FILE, body, 0o120777,
        ))
        client = client_for(raw=body, archive_override=symlink_member)
        with self.assertRaises(platform_live_qa_sequence.SequenceError) as caught:
            client.exact_child_receipt(
                sequence_run_id=90, sequence_attempt=1, role="public", target_sha="a" * 40,
            )
        self.assertEqual(caught.exception.reason, "artifact_member_invalid")
        client = good
        client.artifact_bytes = lambda _path: b"x" * len(make_zip((
            platform_live_qa_sequence.CHILD_RECEIPT_FILE, body, 0o100600,
        )))  # type: ignore[method-assign]
        with self.assertRaises(platform_live_qa_sequence.SequenceError) as caught:
            client.exact_child_receipt(
                sequence_run_id=90, sequence_attempt=1, role="public", target_sha="a" * 40,
            )
        self.assertEqual(caught.exception.reason, "artifact_digest_invalid")

    def test_active_sequence_requires_exact_parent_run_job_and_deploy_mode(self) -> None:
        class FakeActions(platform_live_qa_sequence.GitHubActions):
            def __init__(
                self,
                *,
                mode: str = "deploy",
                job_status: str = "in_progress",
                job_name: str = platform_live_qa_sequence.SEQUENCE_JOB_NAME,
                run_changes: dict[str, object] | None = None,
                dev_sha: str = "a" * 40,
            ) -> None:
                self.mode = mode
                self.job_status = job_status
                self.job_name = job_name
                self.run_changes = run_changes or {}
                self.dev_sha = dev_sha
                self.repository = "StrayForest/old_sparky"

            def workflow_id(self, _path: str) -> int:
                return 77

            def request(self, _method: str, path: str) -> object:
                if path == "/actions/runs/90":
                    return {
                        "id": 90, "run_attempt": 1, "workflow_id": 77,
                        "path": platform_live_qa_sequence.SEQUENCER_WORKFLOW_PATH,
                        "event": "workflow_dispatch", "head_branch": "dev", "head_sha": "a" * 40,
                        "name": "Platform post-deploy live QA",
                        "display_title": platform_live_qa_sequence._run_name("12", "2", "a" * 40),
                        "status": "in_progress", "conclusion": None,
                        **self.run_changes,
                    }
                if path.endswith("/jobs?per_page=100&page=1"):
                    return {"total_count": 1, "jobs": [{
                        "name": self.job_name,
                        "status": self.job_status,
                    }]}
                raise AssertionError(path)

            def verify_parent_deploy(self, **_kwargs: object) -> tuple[str, int, str]:
                return self.mode, 12, "gha-12-2-aaaaaaaaaaaa"

            def exact_handoff_authorization(self, **_kwargs: object) -> dict[str, object]:
                return {"sequence_run_id": "90"}

            def current_dev_sha(self) -> str:
                return self.dev_sha

        FakeActions().verify_active_sequence(
            sequence_run_id=90, sequence_attempt=1, target_sha="a" * 40,
            deploy_run_id=12, deploy_attempt=2,
        )
        cases = (
            (FakeActions(mode="recovery-deploy"), "parent_mode_invalid"),
            (FakeActions(job_status="completed"), "sequence_job_not_active"),
            (FakeActions(job_name="Unrelated job"), "sequence_job_not_active"),
            (FakeActions(run_changes={"run_attempt": 2}), "sequence_identity_invalid"),
            (FakeActions(run_changes={"head_sha": "b" * 40}), "sequence_identity_invalid"),
            (FakeActions(dev_sha="b" * 40), "dev_head_mismatch"),
        )
        for fake, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(
                platform_live_qa_sequence.SequenceError
            ) as caught:
                fake.verify_active_sequence(
                    sequence_run_id=90, sequence_attempt=1, target_sha="a" * 40,
                    deploy_run_id=12, deploy_attempt=2,
                )
            self.assertEqual(caught.exception.reason, reason)

    def test_dynamic_run_name_uses_display_title_not_static_name(self) -> None:
        class FakeActions(platform_live_qa_sequence.GitHubActions):
            def __init__(self) -> None:
                self.repository = "StrayForest/old_sparky"
                self.calls = 0

            def workflow_id(self, _path: str) -> int:
                return 77

            def request(self, _method: str, _path: str) -> object:
                self.calls += 1
                if self.calls == 1:
                    return {
                        "id": 90, "run_attempt": 1, "workflow_id": 77,
                        "path": platform_live_qa_sequence.SEQUENCER_WORKFLOW_PATH,
                        "event": "workflow_dispatch", "head_branch": "dev",
                        "head_sha": "a" * 40,
                        "name": "Platform post-deploy live QA",
                        "display_title": platform_live_qa_sequence._run_name("12", "2", "a" * 40),
                    }
                return {"total_count": 1, "workflow_runs": [{
                    "id": 90, "run_attempt": 1, "workflow_id": 77,
                    "path": platform_live_qa_sequence.SEQUENCER_WORKFLOW_PATH,
                    "event": "workflow_dispatch", "head_branch": "dev", "head_sha": "a" * 40,
                    "name": "Platform post-deploy live QA",
                    "display_title": platform_live_qa_sequence._run_name("12", "2", "a" * 40),
                }]}

        fake = FakeActions()
        self.assertTrue(fake.verify_sequence_first_delivery(
            current_run_id=90, current_attempt=1, workflow_source_sha="a" * 40,
            expected_name=platform_live_qa_sequence._run_name("12", "2", "a" * 40),
            parent_run_id="12", parent_attempt="2", target_sha="a" * 40,
            release_slug="gha-12-2-aaaaaaaaaaaa",
        ))
        fake = FakeActions()
        original = fake.request
        def bad_static_name(method: str, path: str) -> object:
            value = original(method, path)
            if isinstance(value, dict) and value.get("id") == 90:
                return {**value, "name": value["display_title"]}
            return value
        fake.request = bad_static_name  # type: ignore[method-assign]
        with self.assertRaises(platform_live_qa_sequence.SequenceError):
            fake.verify_sequence_first_delivery(
                current_run_id=90, current_attempt=1, workflow_source_sha="a" * 40,
                expected_name=platform_live_qa_sequence._run_name("12", "2", "a" * 40),
                parent_run_id="12", parent_attempt="2", target_sha="a" * 40,
                release_slug="gha-12-2-aaaaaaaaaaaa",
            )

    def test_first_delivery_rejects_reruns_and_honors_older_claimed_run(self) -> None:
        class FakeActions(platform_live_qa_sequence.GitHubActions):
            def __init__(self, *, authorized_prior: bool, prior_claimed: bool,
                         prior_conclusion: str = "cancelled") -> None:
                self.repository = "StrayForest/old_sparky"
                self.authorized_prior = authorized_prior
                self.prior_claimed = prior_claimed
                self.prior_conclusion = prior_conclusion
                self.calls: list[str] = []

            def workflow_id(self, _path: str) -> int:
                return 77

            def request(self, _method: str, path: str) -> object:
                self.calls.append(path)
                current = {
                    "id": 90, "run_attempt": 1, "workflow_id": 77,
                    "path": platform_live_qa_sequence.SEQUENCER_WORKFLOW_PATH,
                    "event": "workflow_dispatch", "head_branch": "dev", "head_sha": "a" * 40,
                    "name": "Platform post-deploy live QA",
                    "display_title": platform_live_qa_sequence._run_name("12", "2", "a" * 40),
                }
                if path == "/actions/runs/90":
                    return current
                if path.startswith("/actions/workflows/platform-postdeploy-live-qa.yml/runs?"):
                    prior = {**current, "id": 89}
                    return {"total_count": 2, "workflow_runs": [current, prior]}
                if path == "/actions/runs/89":
                    return {
                        **current,
                        "id": 89,
                        "status": "completed",
                        "conclusion": self.prior_conclusion,
                    }
                raise AssertionError(path)

            def exact_handoff_authorization(self, **_kwargs: object) -> dict[str, object]:
                return {"sequence_run_id": "89" if self.authorized_prior else "90"}

            def _has_prior_child_receipt(self, **kwargs: object) -> bool:
                if kwargs.get("sequence_run_id") != 89:
                    raise AssertionError("prior run identity was not checked")
                return self.prior_claimed

        args = {
            "current_run_id": 90,
            "current_attempt": 1,
            "workflow_source_sha": "a" * 40,
            "expected_name": platform_live_qa_sequence._run_name("12", "2", "a" * 40),
            "parent_run_id": "12",
            "parent_attempt": "2",
            "target_sha": "a" * 40,
            "release_slug": "gha-12-2-aaaaaaaaaaaa",
        }
        prior_claimed = FakeActions(authorized_prior=True, prior_claimed=True)
        self.assertFalse(prior_claimed.verify_sequence_first_delivery(**args))
        self.assertIn("/actions/runs/89", prior_claimed.calls)

        unauthorized_prior = FakeActions(authorized_prior=False, prior_claimed=False)
        self.assertTrue(unauthorized_prior.verify_sequence_first_delivery(**args))

        cancelled_without_receipt = FakeActions(authorized_prior=True, prior_claimed=False)
        with self.assertRaises(platform_live_qa_sequence.SequenceError) as caught:
            cancelled_without_receipt.verify_sequence_first_delivery(**args)
        self.assertEqual(caught.exception.reason, "prior_authorized_without_child_receipt")

        rerun = FakeActions(authorized_prior=False, prior_claimed=False)
        with self.assertRaises(platform_live_qa_sequence.SequenceError) as caught:
            rerun.verify_sequence_first_delivery(**{**args, "current_attempt": 2})
        self.assertEqual(caught.exception.reason, "successor_rerun_refused")
        self.assertEqual(rerun.calls, [])

    def test_child_validation_binds_run_and_dispatch_inputs_not_actor(self) -> None:
        from unittest.mock import patch

        target = "a" * 40
        receipt = {
            "schema": 1,
            "kind": "platform-live-qa-child-dispatch",
            "repository": "StrayForest/old_sparky",
            "sequence_workflow_path": ".github/workflows/platform-postdeploy-live-qa.yml",
            "sequence_run_id": "90",
            "sequence_run_attempt": "1",
            "sequence_source_sha": target,
            "deploy_run_id": "12",
            "deploy_run_attempt": "2",
            "source_sha": target,
            "role": "public",
            "child_workflow_path": platform_live_qa_sequence.PUBLIC_WORKFLOW_PATH,
            "child_workflow_id": 51,
            "child_run_id": "431",
            "child_run_attempt": "1",
            "expected_target_sha": target,
            "public_run_id": "431",
            "marker": platform_live_qa_sequence._marker("12", "2", target),
        }

        class FakeActions:
            repository = "StrayForest/old_sparky"

            def __init__(self, *, run_changes: dict[str, object] | None = None) -> None:
                self.run_changes = run_changes or {}

            def workflow_id(self, _path: str) -> int:
                return 51

            def request(self, _method: str, path: str) -> object:
                if not re.fullmatch(r"/actions/runs/[0-9]+", path):
                    raise AssertionError(path)
                return {
                    "id": 431, "run_attempt": 1, "workflow_id": 51,
                    "path": platform_live_qa_sequence.PUBLIC_WORKFLOW_PATH,
                    "event": "workflow_dispatch", "head_branch": "dev", "head_sha": target,
                    "status": "in_progress", "conclusion": None,
                    "actor": {"login": "github-actions[bot]"},
                    **self.run_changes,
                }

            def verify_active_sequence(self, **_kwargs: object) -> None:
                return None

            def exact_child_receipt(self, **_kwargs: object) -> dict[str, object]:
                return receipt

        env = {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/dev",
            "GITHUB_REPOSITORY": "StrayForest/old_sparky",
            "GITHUB_ACTOR": "github-actions[bot]",
            "GITHUB_RUN_ID": "431",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SHA": target,
            "GITHUB_API_URL": "https://api.github.com",
            "GH_TOKEN": "test-token",
            "EXPECTED_TARGET_SHA": target,
            "SEQUENCE_RUN_ID": "90",
            "SEQUENCE_RUN_ATTEMPT": "1",
            "SEQUENCE_DEPLOY_RUN_ID": "12",
            "SEQUENCE_DEPLOY_RUN_ATTEMPT": "2",
            "SEQUENCE_SOURCE_SHA": target,
            "SEQUENCE_CHILD_ROLE": "public",
            "LIVE_PROVISION": "true",
            "LIVE_MARKER": receipt["marker"],
        }
        fake = FakeActions()
        with patch.dict("os.environ", env, clear=True), patch.object(
            platform_live_qa_sequence, "GitHubActions", return_value=fake
        ):
            platform_live_qa_sequence.validate_active_child_from_environment()

        # Actor metadata is not dispatch authority. The parent sequence
        # artifact and exact child run binding establish that authority.
        arbitrary_actor = FakeActions(run_changes={"actor": {"login": "untrusted-user"}})
        with patch.dict(
            "os.environ", {**env, "GITHUB_ACTOR": "untrusted-user"}, clear=True
        ), patch.object(
            platform_live_qa_sequence, "GitHubActions", return_value=arbitrary_actor
        ):
            platform_live_qa_sequence.validate_active_child_from_environment()

        cases = (
            ({"EXPECTED_TARGET_SHA": "b" * 40}, {}, "child_event_invalid"),
            ({"SEQUENCE_RUN_ID": "0"}, {}, "run_id_invalid"),
            ({}, {"head_sha": "b" * 40}, "child_run_identity_invalid"),
            ({"GITHUB_RUN_ID": "432"}, {}, "child_run_identity_invalid"),
            ({"SEQUENCE_CHILD_ROLE": "user-qa"}, {}, "child_run_identity_invalid"),
            ({"SEQUENCE_SOURCE_SHA": "b" * 40}, {}, "receipt_identity_invalid"),
        )
        for env_changes, run_changes, reason in cases:
            with self.subTest(reason=reason):
                fake = FakeActions(run_changes=run_changes)
                with patch.dict("os.environ", {**env, **env_changes}, clear=True), patch.object(
                    platform_live_qa_sequence, "GitHubActions", return_value=fake
                ), self.assertRaises(platform_live_qa_sequence.SequenceError) as caught:
                    platform_live_qa_sequence.validate_active_child_from_environment()
                self.assertEqual(caught.exception.reason, reason)

    def test_redirect_origins_and_duplicate_json_keys_fail_closed(self) -> None:
        with self.assertRaises(platform_live_qa_sequence.SequenceError):
            platform_live_qa_sequence._api_root("https://api.github.com/org")
        with self.assertRaises(ValueError):
            platform_live_qa_sequence._strict_json(b'{"id":1,"id":2}')
        client = platform_live_qa_sequence.GitHubActions(
            "https://api.github.com", "StrayForest/old_sparky", "test-token"
        )
        self.assertTrue(any(isinstance(item, platform_live_qa_sequence._NoRedirect) for item in client.opener.handlers))

    def test_child_workflows_gate_host_effects_and_hold_parent_lock(self) -> None:
        production = (WORKFLOW_ROOT / "platform-production-deploy.yml").read_text(encoding="utf-8")
        launch = (WORKFLOW_ROOT / "platform-live-launch.yml").read_text(encoding="utf-8")
        userqa = (WORKFLOW_ROOT / "platform-live-user-qa.yml").read_text(encoding="utf-8")
        successor = (WORKFLOW_ROOT / "platform-postdeploy-live-qa.yml").read_text(encoding="utf-8")
        for source in (launch, userqa):
            validator = source.index("platform_live_qa_sequence.py validate-child")
            first_live_operation = source.index("Authenticate and validate live-launch handoff artifact") if source == launch else source.index("Run live-user QA on the production server")
            self.assertLess(validator, first_live_operation)
            self.assertIn("sequence_run_id", source)
            self.assertIn("platform-live-qa-sequenced-child-", source)
        self.assertIn("group: platform-live-qa", successor)
        self.assertIn("workflow_dispatch:", successor)
        self.assertNotIn("workflow_run:", successor)
        self.assertIn("queue: max", successor)
        self.assertIn("cancel-in-progress: false", successor)
        sequence_job = _job_block(successor, "sequence")
        ordered_steps = (
            "Dispatch public production QA",
            "Create exact public child receipt",
            "Upload exact public child receipt",
            "Require exact public success and dispatch UserQA",
            "Create exact UserQA child receipt",
            "Upload exact UserQA child receipt",
            "Require exact UserQA and cleanup success",
        )
        positions = [sequence_job.index(f"- name: {name}") for name in ordered_steps]
        self.assertEqual(positions, sorted(positions))
        for source in (launch, userqa):
            self.assertIn("queue: max", source)
            self.assertEqual(source.count("queue:"), 1)
            self.assertIn("cancel-in-progress: false", source)
        self.assertEqual(successor.count("queue:"), 1)
        self.assertIn("platform-postdeploy-live-qa-child-public-", successor)
        self.assertIn("platform-postdeploy-live-qa-child-user-qa-", successor)
        handoff_job = _job_block(production, "postdeploy-handoff")
        self.assertIn("!cancelled()", handoff_job)
        self.assertIn("needs.production.result == 'success'", handoff_job)
        self.assertIn("inputs.mode == 'deploy'", handoff_job)
        self.assertIn("continue-on-error: true", handoff_job)
        self.assertIn("Record bounded QA handoff outcome", handoff_job)
        self.assertIn("actions: write", handoff_job)
        self.assertIn("contents: read", handoff_job)
        self.assertNotIn("environment: production", handoff_job)
        self.assertIn("dispatch-handoff", handoff_job)
        self.assertIn("platform-postdeploy-live-qa-authorization-", handoff_job)

    def test_public_failure_or_dev_advance_never_dispatches_userqa(self) -> None:
        from unittest.mock import patch

        class FakeActions:
            def __init__(self, advance_after_public: bool) -> None:
                self.dev_reads = 0
                self.advance_after_public = advance_after_public
                self.dispatches: list[str] = []

            def verify_parent_deploy(self, **_kwargs: object) -> tuple[str, int, str]:
                return "deploy", 12, "gha-12-2-aaaaaaaaaaaa"

            def exact_deployment_receipt(self, **_kwargs: object) -> dict[str, object]:
                return {"verified_mode": "deploy", "release_slug": "gha-12-2-aaaaaaaaaaaa"}

            def exact_handoff_authorization(self, **_kwargs: object) -> dict[str, object]:
                return {"sequence_run_id": "90"}

            def wait_for_parent_success(self, **_kwargs: object) -> None:
                return None

            def verify_sequence_first_delivery(self, **_kwargs: object) -> bool:
                return True

            def current_dev_sha(self) -> str:
                self.dev_reads += 1
                return "b" * 40 if self.advance_after_public and self.dev_reads > 1 else "a" * 40

            def workflow_id(self, path: str) -> int:
                return 51 if path.endswith("platform-live-launch.yml") else 52

            def wait_for_run(self, **_kwargs: object) -> None:
                if not self.advance_after_public:
                    raise platform_live_qa_sequence.SequenceError("child", "run_not_successful")

            def dispatch(self, path: str, _workflow_id: int, _inputs: dict[str, str]) -> int:
                self.dispatches.append(path)
                return 431

        env = {
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/dev",
            "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "90", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_REPOSITORY": "StrayForest/old_sparky", "GITHUB_API_URL": "https://api.github.com",
            "GH_TOKEN": "test-token", "DEPLOY_TARGET_SHA": "a" * 40,
            "DEPLOY_RUN_ID": "12", "DEPLOY_RUN_ATTEMPT": "2",
            "PUBLIC_CHILD_RUN_ID": "431",
        }
        cases = ((False, "run_not_successful"), (True, "dev_head_mismatch"))
        for advances, reason in cases:
            fake = FakeActions(advances)
            with patch.dict("os.environ", env, clear=True), patch.object(
                platform_live_qa_sequence, "GitHubActions", return_value=fake
            ), self.assertRaises(platform_live_qa_sequence.SequenceError) as caught:
                platform_live_qa_sequence.finish_public_and_dispatch_userqa()
            self.assertEqual(caught.exception.reason, reason)
            self.assertEqual(fake.dispatches, [])


def _job_block(source: str, name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"workflow job is missing: {name}")
    return match.group("body")


def _media_report_payload() -> dict[str, object]:
    return {
        "ok": True,
        "mode": "check",
        "mutated": False,
        "inventory_before": {
            "legacy_upload_references": 2,
            "packaged_asset_references": 3,
            "manual_conflicts": 0,
        },
        "inventory_after": {
            "legacy_upload_references": 0,
            "packaged_asset_references": 3,
            "manual_conflicts": 0,
        },
        "source_locations": {"r2": 1},
        "operations": {"r2_gets": 4},
    }


def _media_projection_script() -> str:
    """Extract the runner-side scalar projection for deterministic tests."""

    workflow = (
        WORKFLOW_ROOT / "platform-media-migration-diagnostics.yml"
    ).read_text(encoding="utf-8")
    start = workflow.index("      - name: Inspect production legacy media sources")
    end = workflow.index("      - name: Remove private media diagnostic capture", start)
    block = workflow[start:end]
    marker = (
        "/usr/bin/python3 - \"$public_line\" \"$precondition_line\" "
        '\"$remote_status\" \"$remote_stderr_bytes\" '
        '\"$TARGET_SHA\" \"$deployed_sha\" <<\'PY\'\n'
    )
    script = block.split(marker, 1)[1].split("\n          PY", 1)[0]
    return textwrap.dedent(script)


def _run_media_projection(
    public_line: str,
    *,
    remote_status: str = "0",
    remote_stderr_bytes: str = "0",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-",
            public_line,
            "",
            remote_status,
            remote_stderr_bytes,
            "a" * 40,
            "a" * 40,
        ],
        input=_media_projection_script(),
        text=True,
        capture_output=True,
        check=False,
    )


def _media_summary_script() -> str:
    """Extract the final safe step-summary renderer for adversarial tests."""

    workflow = (
        WORKFLOW_ROOT / "platform-media-migration-diagnostics.yml"
    ).read_text(encoding="utf-8")
    block = workflow[workflow.index("      - name: Write media inventory summary") :]
    marker = (
        "/usr/bin/python3 - \"$MEDIA_SUMMARY\" \"$TARGET_SHA\" "
        ">> \"$GITHUB_STEP_SUMMARY\" <<'PY'\n"
    )
    script = block.split(marker, 1)[1].split("\n          PY", 1)[0]
    return textwrap.dedent(script)


class RemoteWorkflowGuardContractTests(unittest.TestCase):
    def _source(self, key: str) -> str:
        return (WORKFLOW_ROOT / WORKFLOWS[key]).read_text(encoding="utf-8")

    def test_secret_jobs_are_fresh_no_checkout_jobs_with_data_only_handoffs(self) -> None:
        secret_jobs = {
            "as12": "proof",
            "runtime": "collect",
            "profile": "create-fixture",
            "live": "live-user-qa",
            "translation": "regression",
        }
        for key, job_name in secret_jobs.items():
            with self.subTest(workflow=key):
                job = _job_block(self._source(key), job_name)
                self.assertIn("environment: production", job)
                self.assertNotIn("actions/checkout@", job)
                self.assertIn("closed", job.lower())
                self.assertNotIn("platform_live_launch_report.py", job)
                self.assertNotIn("platform/tools/platform_workflow_input_guard.py", job)
                if key == "live":
                    # The secret consumer authenticates the exact current-run
                    # artifact through the API and hashes the downloaded ZIP
                    # itself. It must not execute candidate code or rely on
                    # download-artifact's weaker action-side digest output.
                    self.assertNotIn("actions/download-artifact@", job)
                    self.assertIn("HANDOFF_ARTIFACT_ID", job)
                    self.assertIn("/actions/runs/", job)
                    self.assertIn("/actions/artifacts/", job)
                    self.assertIn("metadata.get(\"digest\")", job)
                    self.assertIn("hashlib.sha256(archive_bytes).hexdigest()", job)
                    self.assertIn("len(matches) != 1", job)
                    self.assertIn(
                        'entries[0].filename != "live-user-qa-input.json"', job
                    )
                    self.assertIn("os.O_EXCL", job)
                    self.assertIn(
                        "/root/.oldsparky/liveqa/platform_workflow_remote_dispatch.py",
                        job,
                    )
                else:
                    self.assertIn("actions/download-artifact@", job)

        for key, validator_job in {
            "as12": "validate-proof-inputs",
            "runtime": "validate-runtime-inputs",
            "profile": "validate-fixture-inputs",
            "live": "validate-live-user-inputs",
            "translation": "validate-translation-inputs",
        }.items():
            with self.subTest(validator=key):
                validator = _job_block(self._source(key), validator_job)
                self.assertIn("actions/checkout@", validator)
                self.assertNotIn("secrets.PROD_SSH_", validator)
                self.assertIn("persist-credentials: false", validator)

    def test_handoffs_have_strict_schema_and_no_shell_newline_surfaces(self) -> None:
        for key in WORKFLOWS:
            with self.subTest(workflow=key):
                source = self._source(key)
                self.assertIn('"schema": 1', source)
                self.assertIn("set(payload)", source)
                self.assertIn("test ! -L", source)
                self.assertIn("install -m 600", source)
                self.assertRegex(source, r"fullmatch\(r?['\"]\[0-9a-f\]\{40\}")
                if key == "runtime":
                    self.assertIn("utc-timestamp", source)
                    self.assertIn("timestamp_re", source)
                elif key == "live":
                    # The live handoff has both a legacy same-source schema
                    # and the exact receipt-backed no-op schema. Require the
                    # closed key sets, canonical JSON, bounded member, and
                    # exclusive private-file creation in the inline verifier.
                    self.assertIn("base_keys = {", source)
                    self.assertIn("set(payload) != base_keys", source)
                    self.assertIn("binding_keys = {", source)
                    self.assertIn("set(binding) != binding_keys", source)
                    self.assertIn("raw[:-1] != canonical(payload)", source)
                    self.assertIn("len(binding_bytes) > MAX_BINDING", source)
                    self.assertIn("archive.open(info)", source)
                    self.assertIn("os.O_EXCL", source)
                else:
                    self.assertIn("handoff schema is invalid", source)

    def test_installed_sha_and_runtime_window_are_checked_before_lock(self) -> None:
        for key in WORKFLOWS:
            with self.subTest(workflow=key):
                source = self._source(key)
                if key == "live":
                    # The secret workflow enters through the fixed C2 remote
                    # dispatcher. Its manifest-bound app helper verifies the
                    # installed app payload before re-entering under the
                    # canonical lock, then compares the receipt tuple before
                    # starting any browser/mailbox helper.
                    self.assertIn(
                        "/root/.oldsparky/liveqa/platform_workflow_remote_dispatch.py",
                        source,
                    )
                    remote_dispatcher = (
                        PLATFORM_ROOT / "tools/platform_workflow_remote_dispatch.py"
                    ).read_text(encoding="utf-8")
                    self.assertIn(
                        'LIVE_USER_QA_HELPER = ACTIVE_TOOLS_DIR / "platform_live_user_qa_dispatch.py"',
                        remote_dispatcher,
                    )
                    self.assertIn('if arguments == ["live-user-qa"]:', remote_dispatcher)
                    dispatch_start = remote_dispatcher.index(
                        '        if arguments == ["live-user-qa"]:',
                        remote_dispatcher.index("payload = load_stdin_payload(mode=mode)"),
                    )
                    dispatch_end_match = re.search(
                        r"(?m)^        app_target_sha, _expected_baseline, source_arguments = _source_binding_context\(payload\)$",
                        remote_dispatcher[dispatch_start + 1 :],
                    )
                    self.assertIsNotNone(dispatch_end_match)
                    dispatch_end = dispatch_start + 1 + dispatch_end_match.start()
                    live_user_route = remote_dispatcher[dispatch_start:dispatch_end]
                    self.assertIn("return _run_live_user_qa_sudo(", live_user_route)
                    self.assertIn(
                        "[payload[\"target_sha\"], *source_arguments]", live_user_route
                    )
                    self.assertIn("expected_sha=payload[\"target_sha\"]", live_user_route)
                    self.assertIn("expected_app_sha=app_target_sha", live_user_route)
                    trusted_helper = (
                        PLATFORM_ROOT / "tools/platform_live_user_qa_dispatch.py"
                    ).read_text(encoding="utf-8")
                    main = trusted_helper[trusted_helper.index("def main(") :]
                    verify_position = main.index("manifest = _verify_install(app_target_sha)")
                    lock_position = main.index("str(RELEASE_LOCK_EXEC)")
                    self.assertLess(verify_position, lock_position)
                    self.assertLess(
                        main.index("_validate_source_binding_schema("), lock_position
                    )
                    supervisor_position = main.index("_require_release_lock_supervisor()")
                    tuple_position = main.index("_validate_source_binding_under_lock(")
                    helper_exec_position = main.index('wrapper = payload / "platform/tools/platform_live_user_qa.sh"')
                    self.assertLess(lock_position, supervisor_position)
                    self.assertLess(supervisor_position, tuple_position)
                    self.assertLess(tuple_position, helper_exec_position)
                    continue
                guard_positions = [
                    position
                    for position in range(len(source))
                    if source.startswith('/bin/bash "$guard"', position)
                ]
                self.assertTrue(guard_positions)
                first_guard = guard_positions[0]
                self.assertLess(source.index('test -f "$guard"'), first_guard)
                self.assertLess(source.index('test ! -L "$guard"'), first_guard)
                sha_check = re.search(
                    r'test "\$active_sha" = "\$(?:expected_sha|target_sha)"',
                    source,
                )
                self.assertIsNotNone(sha_check)
                self.assertLess(sha_check.start(), first_guard)
                self.assertIn('"source_git_commit"', source)
        runtime = self._source("runtime")
        self.assertLess(runtime.index("utc-timestamp"), runtime.index('/bin/bash "$guard"'))
        self.assertLess(runtime.index("diagnostic window is invalid"), runtime.index('/bin/bash "$guard"'))

    def test_every_remote_helper_path_is_regular_and_not_a_symlink(self) -> None:
        helper_paths = {
            "as12": ("$input_guard", "$edge_policy_tool"),
            "runtime": ("$input_guard", "$summary_tool", "$nginx_summary_tool", "$nginx_error_log"),
            "profile": ("$input_guard", "$runtime_common", "$seed_tool"),
            "live": ("$input_guard", "$qa_script", "$reviewed_helper", "$installed_helper"),
            "translation": ("$input_guard", "$runtime_common", "$translation_summary"),
        }
        for key, paths in helper_paths.items():
            with self.subTest(workflow=key):
                source = self._source(key)
                if key == "live":
                    self.assertIn(
                        "/root/.oldsparky/liveqa/platform_workflow_remote_dispatch.py",
                        source,
                    )
                    self.assertNotIn("/opt/oldsparky/platform/current/tools/", source)
                    remote_dispatcher = (
                        PLATFORM_ROOT / "tools/platform_workflow_remote_dispatch.py"
                    ).read_text(encoding="utf-8")
                    self.assertIn(
                        'LIVE_USER_QA_HELPER = ACTIVE_TOOLS_DIR / "platform_live_user_qa_dispatch.py"',
                        remote_dispatcher,
                    )
                    installed_dispatcher = (
                        PLATFORM_ROOT / "tools/platform_live_user_qa_dispatch.py"
                    ).read_text(encoding="utf-8")
                    self.assertIn(
                        '(QA_HELPER, 0o755, "platform/tools/platform_live_user_qa_trusted.sh")',
                        installed_dispatcher,
                    )
                    self.assertIn("for path, mode, relative in bound_paths", installed_dispatcher)
                    self.assertIn("_regular(path, mode=mode", installed_dispatcher)
                    continue
                for path in paths:
                    self.assertIn(f'test -f "{path}"', source, path)
                    self.assertIn(f'test ! -L "{path}"', source, path)
        for key in WORKFLOWS:
            source = self._source(key)
            if key == "live":
                continue
            self.assertRegex(source, r'test -f "\$guard" && test ! -L "\$guard" && test -x "\$guard"')

    def test_adversarial_values_and_symlink_handoffs_fail_closed(self) -> None:
        for value in (
            "a" * 39,
            "A" * 40,
            "a" * 40 + "\n",
            "a" * 20 + "\n" + "a" * 19,
            "$(id)",
            "a" * 39 + "\x00",
        ):
            with self.subTest(sha=repr(value)):
                with self.assertRaises(WorkflowInputError):
                    validate_target_sha(value)
        for value in (
            "2026-09-09T09:33:00Z\n",
            "2026-09-09T09:33:00+00:00",
            "2026-09-09T09:33:00Z$(id)",
            "2026-09-09T09:33:00Z\x00",
        ):
            with self.subTest(timestamp=repr(value)):
                with self.assertRaises(WorkflowInputError):
                    validate_utc_timestamp(value)
        for value in ("operator@example.com\n", "operator@example.com$(id)", "é@example.com"):
            with self.subTest(email=repr(value)):
                with self.assertRaises(WorkflowInputError):
                    validate_control_email(value)

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            real = root / "real.json"
            real.write_text("{}\n", encoding="utf-8")
            symlink = root / "handoff.json"
            symlink.symlink_to(real)
            with self.assertRaises(WorkflowInputError):
                _write_private_json(symlink, {"schema": "1"})

    def test_missing_helper_contracts_fail_before_remote_side_effects(self) -> None:
        for key in WORKFLOWS:
            with self.subTest(workflow=key):
                source = self._source(key)
                if key == "live":
                    self.assertIn(
                        "/root/.oldsparky/liveqa/platform_workflow_remote_dispatch.py",
                        source,
                    )
                    self.assertNotIn("bash -s", source)
                    remote_dispatcher = (
                        PLATFORM_ROOT / "tools/platform_workflow_remote_dispatch.py"
                    ).read_text(encoding="utf-8")
                    self.assertIn(
                        'LIVE_USER_QA_HELPER = ACTIVE_TOOLS_DIR / "platform_live_user_qa_dispatch.py"',
                        remote_dispatcher,
                    )
                    installed_dispatcher = (
                        PLATFORM_ROOT / "tools/platform_live_user_qa_dispatch.py"
                    ).read_text(encoding="utf-8")
                    self.assertIn(
                        "installed live-QA trusted entrypoint is not bound to the manifest",
                        installed_dispatcher,
                    )
                    continue
                self.assertRegex(
                    source,
                    r"missing(?: or unsafe|)\"?",
                )
                self.assertNotIn("test -x \"$guard\" ||", source)
                self.assertIn("test -f \"$guard\" && test ! -L \"$guard\"", source)

    def test_remote_artifact_directory_uses_atomic_nofollow_helper(self) -> None:
        source = (PLATFORM_ROOT / "tools/platform_workflow_remote_dispatch.py").read_text(
            encoding="utf-8"
        )
        helper = (PLATFORM_ROOT / "tools/platform_prepare_artifact_dir.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ARTIFACT_DIR_HELPER", source)
        self.assertNotIn('"/usr/bin/install"', source)
        self.assertIn("os.mkdir(leaf, 0o700, dir_fd=parent_fd)", helper)
        self.assertIn("os.O_NOFOLLOW", helper)
        self.assertIn("os.fstat(child_fd)", helper)

        run_id = str(os.getpid())
        path = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-1")
        if path.exists() or path.is_symlink():
            self.skipTest("test artifact directory name is already occupied")
        try:
            platform_prepare_artifact_dir.prepare(str(path))
            metadata = path.lstat()
            self.assertTrue(metadata.st_mode & 0o700 == 0o700)
            self.assertFalse(path.is_symlink())
            marker = path / platform_prepare_artifact_dir.OWNER_MARKER_NAME
            marker_content = marker.read_text(encoding="ascii")
            self.assertTrue(
                marker_content.startswith(
                    platform_prepare_artifact_dir.OWNER_MARKER_PREFIX.decode("ascii")
                    + " dev="
                )
            )
            self.assertIn(f" ino={metadata.st_ino}\n", marker_content)
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        finally:
            if path.is_dir() and not path.is_symlink():
                marker = path / platform_prepare_artifact_dir.OWNER_MARKER_NAME
                if marker.is_file() and not marker.is_symlink():
                    marker.unlink()
                path.rmdir()

        existing = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-4")
        existing.mkdir(mode=0o700)
        try:
            sentinel = existing / "sentinel"
            sentinel.write_bytes(b"preserve\n")
            with self.assertRaises((OSError, RuntimeError)):
                platform_prepare_artifact_dir.prepare(str(existing))
            self.assertEqual(sentinel.read_bytes(), b"preserve\n")
        finally:
            if existing.is_dir() and not existing.is_symlink():
                sentinel = existing / "sentinel"
                if sentinel.is_file() and not sentinel.is_symlink():
                    sentinel.unlink()
                existing.rmdir()

        target = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-2")
        if target.exists() or target.is_symlink():
            self.skipTest("test symlink artifact directory name is already occupied")
        target.mkdir()
        link = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-3")
        try:
            link.symlink_to(target)
            with self.assertRaises((OSError, RuntimeError)):
                platform_prepare_artifact_dir.prepare(str(link))
            self.assertTrue(link.is_symlink())
        finally:
            if link.is_symlink():
                link.unlink()
            if target.is_dir() and not target.is_symlink():
                target.rmdir()

    def test_media_remote_exit_and_stderr_bytes_are_actual_values(self) -> None:
        report = platform_media_migration_diagnostics_summary.public_summary(
            payload=_media_report_payload(),
            producer_exit_code=1,
            stderr_bytes=17,
            remote_exit_code=1,
            remote_stderr_bytes=23,
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["producer_exit_code"], 1)
        self.assertEqual(report["stderr_bytes"], 17)
        self.assertEqual(report["remote_exit_code"], 1)
        self.assertEqual(report["remote_stderr_bytes"], 23)
        self.assertFalse(report["mutated"])
        self.assertEqual(report["inventory_before_legacy_upload_references"], 2)
        self.assertNotIn('"remote_exit_code":255', json.dumps(report))

    def test_media_transport_exit_255_is_distinct_from_precondition(self) -> None:
        report = platform_media_migration_diagnostics_summary.public_summary(
            payload=None,
            producer_exit_code=None,
            stderr_bytes=None,
            remote_exit_code=255,
            remote_stderr_bytes=31,
            precondition_error_class="remote_precondition",
            parse_ok=False,
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error_class"], "remote_or_transport")
        self.assertIsNone(report["producer_exit_code"])
        self.assertIsNone(report["stderr_bytes"])
        self.assertEqual(report["remote_exit_code"], 255)
        self.assertEqual(report["remote_stderr_bytes"], 31)

    def test_media_remote_precondition_exit_is_explicit(self) -> None:
        report = platform_media_migration_diagnostics_summary.public_summary(
            payload=None,
            producer_exit_code=None,
            stderr_bytes=None,
            remote_exit_code=1,
            remote_stderr_bytes=7,
            precondition_error_class="remote_precondition",
            parse_ok=False,
        )

        self.assertEqual(report["error_class"], "remote_precondition")
        self.assertEqual(report["remote_exit_code"], 1)
        self.assertEqual(report["remote_stderr_bytes"], 7)
        self.assertIsNone(report["producer_exit_code"])
        self.assertIsNone(report["stderr_bytes"])

    def test_media_projection_rejects_malformed_old_schema_and_boundary_values(self) -> None:
        valid_report = platform_media_migration_diagnostics_summary.public_summary(
            payload=_media_report_payload(),
            producer_exit_code=0,
            stderr_bytes=0,
            remote_exit_code=0,
            remote_stderr_bytes=0,
        )
        valid_line = "MEDIA_INVENTORY " + json.dumps(
            valid_report,
            separators=(",", ":"),
        )
        accepted = _run_media_projection(valid_line)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(len(accepted.stdout.splitlines()), 1)

        old_schema = dict(valid_report)
        old_schema.pop("remote_exit_code")
        old_schema.pop("remote_stderr_bytes")
        cases: dict[str, str] = {
            "old-schema": "MEDIA_INVENTORY " + json.dumps(old_schema),
            "malformed": 'MEDIA_INVENTORY {"secret":"must-not-leak"',
        }
        for field, value in (
            ("schema", True),
            ("producer_exit_code", False),
            ("stderr_bytes", None),
            ("remote_exit_code", 256),
            ("remote_stderr_bytes", -1),
            ("processed_count", 1_000_001),
            ("mutated", 1),
            ("status", "unknown"),
        ):
            invalid = dict(valid_report)
            invalid[field] = value
            cases[f"invalid-{field}"] = "MEDIA_INVENTORY " + json.dumps(
                invalid,
                separators=(",", ":"),
            )

        for name, public_line in cases.items():
            with self.subTest(case=name):
                rejected = _run_media_projection(public_line)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertNotIn("must-not-leak", rejected.stdout)
                self.assertEqual(len(rejected.stdout.splitlines()), 1)
                safe_report = json.loads(rejected.stdout.split(" ", 1)[1])
                self.assertEqual(safe_report["status"], "failed")
                self.assertEqual(safe_report["remote_exit_code"], 0)
                self.assertEqual(safe_report["remote_stderr_bytes"], 0)

        for status, stderr_bytes in (
            ("256", "0"),
            ("-1", "0"),
            ("0", "1000001"),
            ("0", "-1"),
        ):
            with self.subTest(remote_status=status, remote_stderr=stderr_bytes):
                rejected = _run_media_projection(
                    valid_line,
                    remote_status=status,
                    remote_stderr_bytes=stderr_bytes,
                )
                self.assertNotEqual(rejected.returncode, 0)
                safe_report = json.loads(rejected.stdout.split(" ", 1)[1])
                self.assertEqual(safe_report["status"], "failed")
                self.assertEqual(safe_report["error_class"], "internal")
                self.assertIsNone(safe_report["remote_exit_code"])
                self.assertIsNone(safe_report["remote_stderr_bytes"])

        transport = _run_media_projection(
            valid_line,
            remote_status="255",
            remote_stderr_bytes="31",
        )
        self.assertNotEqual(transport.returncode, 0)
        transport_report = json.loads(transport.stdout.split(" ", 1)[1])
        self.assertEqual(transport_report["status"], "failed")
        self.assertEqual(transport_report["error_class"], "remote_or_transport")
        self.assertEqual(transport_report["remote_exit_code"], 255)
        self.assertEqual(transport_report["remote_stderr_bytes"], 31)

    def test_media_workflow_is_manual_sha_locked_and_safe(self) -> None:
        workflow = (
            WORKFLOW_ROOT / "platform-media-migration-diagnostics.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("  workflow_dispatch:\n    inputs:\n      expected_sha:", workflow)
        self.assertIn("        required: true", workflow)
        self.assertNotIn("\n  push:", workflow)
        self.assertNotIn("statuses: write", workflow)
        self.assertNotIn("platform-media-inventory", workflow)
        self.assertNotIn("/statuses/", workflow)
        self.assertNotIn("Mark media inventory", workflow)
        self.assertNotIn("GH_TOKEN", workflow)
        self.assertNotIn("curl --fail-with-body", workflow)
        self.assertIn("GITHUB_STEP_SUMMARY", workflow)
        self.assertIn("Write media inventory summary", workflow)
        self.assertIn("MEDIA_SUMMARY", workflow)
        self.assertIn("Expected SHA:", workflow)
        self.assertIn("Deployed SHA:", workflow)
        self.assertIn("Aggregate counter", workflow)
        self.assertIn("group: platform-production-deploy", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn('test -n "${PROD_SSH_USER:-}"', workflow)
        self.assertIn(
            '[[ "$PROD_SSH_USER" =~ ^[A-Za-z_][A-Za-z0-9._-]{0,31}$ ]]',
            workflow,
        )
        self.assertNotIn('echo "$PROD_SSH_USER"', workflow)
        self.assertNotIn('printf \'%s\\n\' "$PROD_SSH_USER"', workflow)

        sha_read = workflow.index(
            'active_sha="$(/usr/bin/python3.12 -I - "$release_json"'
        )
        sha_compare = workflow.index(
            'test "$active_sha" = "$expected_sha" || precondition'
        )
        helper_check = workflow.index('test -f "$lock_guard"')
        lock_call = workflow.index('/bin/bash "$lock_guard"')
        producer_call = workflow.index('producer_exit="$?"')
        self.assertLess(sha_read, sha_compare)
        self.assertLess(sha_compare, helper_check)
        self.assertLess(helper_check, lock_call)
        self.assertLess(lock_call, producer_call)
        self.assertIn('"source_git_commit"', workflow)
        self.assertIn("remote_precondition", workflow)
        self.assertIn(
            'test -f "$producer_tool" && test ! -L "$producer_tool" && test -x "$producer_tool" || precondition',
            workflow,
        )
        self.assertIn(
            'test -f "$summary_tool" && test ! -L "$summary_tool" || precondition',
            workflow,
        )
        self.assertIn('remote_status="$?"', workflow)
        self.assertIn('remote_stderr_bytes="$(wc -c <"$remote_error"', workflow)
        self.assertIn('"remote_exit_code"', workflow)
        self.assertIn('"remote_stderr_bytes"', workflow)
        self.assertIn(
            'rm -f -- "$private_report" "$private_error" "$projector_error"',
            workflow,
        )
        self.assertIn('rm -f -- "$public_report"', workflow)
        self.assertIn('if: ${{ always() }}', workflow)
        self.assertIn('"mutated"', workflow)
        self.assertIn('"inventory_before_legacy_upload_references"', workflow)
        self.assertNotIn('"producer_exit_code":255', workflow)
        self.assertNotIn('"producer_exit_code":0,"stderr_bytes":0', workflow)

        hostile_summary = json.dumps(
            {
                "expected_sha": "a" * 40,
                "deployed_sha": "b" * 40,
                "status": "passed",
                "error_class": "none",
                "producer_exit_code": None,
                "stderr_bytes": None,
                "remote_exit_code": None,
                "remote_stderr_bytes": None,
                "failed_count": 10**40,
                "raw_stderr": "secret-stderr",
                "ssh_user": "operator",
                "ssh_host": "production.example.test",
            },
            separators=(",", ":"),
        )
        rendered = subprocess.run(
            [sys.executable, "-", hostile_summary, "a" * 40],
            input=_media_summary_script(),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn("Status: **failed**", rendered.stdout)
        self.assertIn("Error class: `internal`", rendered.stdout)
        self.assertNotIn("secret-stderr", rendered.stdout)
        self.assertNotIn("operator", rendered.stdout)
        self.assertNotIn("production.example.test", rendered.stdout)


if __name__ == "__main__":
    unittest.main()
