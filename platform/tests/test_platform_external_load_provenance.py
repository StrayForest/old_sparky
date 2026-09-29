from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.platform_external_load_provenance import (
    ExternalLoadProvenanceError,
    attach_report_provenance,
    build_final_provenance,
    build_load_status,
    main,
    validate_artifact_archive,
    validate_artifact_metadata,
    validate_load_status,
    validate_profile_data,
    validate_profile_contract,
    validate_report_provenance,
    validate_final_provenance,
)


SHA = "a" * 40
RUN_ID = "123456"
RUN_ATTEMPT = "2"
ARTIFACT_ID = "987654"
ARTIFACT_NAME = "platform-production-external-load-client-123456-2"
def _archive_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in (
            ("external-load.json", b"{}\n"),
            ("load-status.json", b"{}\n"),
            ("timeout-diagnostic-ids.json", b"{}\n"),
        ):
            info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return buffer.getvalue()


ARCHIVE_BYTES = _archive_bytes()
DIGEST = hashlib.sha256(ARCHIVE_BYTES).hexdigest()
TRUSTED_SHA = "c" * 40
PROFILE_ID = "ready-vote-slo-v2"
REPOSITORY = "StrayForest/old_sparky"
WORKFLOW_FILE = ".github/workflows/platform-production-external-load.yml"


def artifact_metadata() -> dict[str, object]:
    return {
        "id": int(ARTIFACT_ID),
        "name": ARTIFACT_NAME,
        "expired": False,
        "size_in_bytes": len(ARCHIVE_BYTES),
        "digest": f"sha256:{DIGEST}",
        "workflow_run": {
            "id": int(RUN_ID),
            "head_sha": SHA,
            "run_attempt": int(RUN_ATTEMPT),
            "head_repository": {"full_name": REPOSITORY},
            "path": WORKFLOW_FILE,
        },
    }


def load_status() -> dict[str, object]:
    return {
        "schema": 1,
        "status": 0,
        "report_ready": True,
        "target_sha": SHA,
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
    }


class ExternalLoadProvenanceTests(unittest.TestCase):
    def test_valid_artifact_requires_exact_attempt_repository_and_workflow(self) -> None:
        accepted = validate_artifact_metadata(
            artifact_metadata(),
            artifact_id=ARTIFACT_ID,
            artifact_name=ARTIFACT_NAME,
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            target_sha=SHA,
            artifact_digest=f"sha256:{DIGEST}",
            repository=REPOSITORY,
            workflow_file=WORKFLOW_FILE,
        )
        self.assertEqual(accepted["artifact_id"], int(ARTIFACT_ID))

        without_attempt = artifact_metadata()
        del without_attempt["workflow_run"]["run_attempt"]  # type: ignore[index]
        with self.assertRaises(ExternalLoadProvenanceError):
            validate_artifact_metadata(
                without_attempt,
                artifact_id=ARTIFACT_ID,
                artifact_name=ARTIFACT_NAME,
                run_id=RUN_ID,
                run_attempt=RUN_ATTEMPT,
                target_sha=SHA,
                artifact_digest=DIGEST,
                repository=REPOSITORY,
                workflow_file=WORKFLOW_FILE,
            )

    def test_artifact_identity_and_digest_mutations_fail_closed(self) -> None:
        cases = {
            "missing_id": lambda payload: payload.pop("id"),
            "wrong_id": lambda payload: payload.__setitem__("id", 7),
            "wrong_workflow_id": lambda payload: payload["workflow_run"].__setitem__("id", 7),  # type: ignore[index]
            "wrong_name": lambda payload: payload.__setitem__("name", "other"),
            "missing_name": lambda payload: payload.pop("name"),
            "wrong_sha": lambda payload: payload["workflow_run"].__setitem__("head_sha", "c" * 40),  # type: ignore[index]
            "missing_sha": lambda payload: payload["workflow_run"].pop("head_sha"),  # type: ignore[index]
            "wrong_attempt": lambda payload: payload["workflow_run"].__setitem__("run_attempt", 3),  # type: ignore[index]
            "malformed_workflow": lambda payload: payload.__setitem__("workflow_run", []),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                candidate = artifact_metadata()
                mutate(candidate)
                with self.assertRaises(ExternalLoadProvenanceError):
                    validate_artifact_metadata(
                        candidate,
                        artifact_id=ARTIFACT_ID,
                        artifact_name=ARTIFACT_NAME,
                        run_id=RUN_ID,
                        run_attempt=RUN_ATTEMPT,
                        target_sha=SHA,
                        artifact_digest=DIGEST,
                        repository=REPOSITORY,
                        workflow_file=WORKFLOW_FILE,
                    )

    def test_archive_bytes_are_bound_to_api_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "artifact.zip"
            archive.write_bytes(ARCHIVE_BYTES)
            self.assertEqual(
                validate_artifact_archive(
                    artifact_metadata(),
                    archive,
                    artifact_id=ARTIFACT_ID,
                    artifact_name=ARTIFACT_NAME,
                    run_id=RUN_ID,
                    run_attempt=RUN_ATTEMPT,
                    target_sha=SHA,
                    artifact_digest=DIGEST,
                    repository=REPOSITORY,
                    workflow_file=WORKFLOW_FILE,
                )["artifact_digest"],
                DIGEST,
            )
            archive.write_bytes(b"replacement")
            with self.assertRaises(ExternalLoadProvenanceError):
                validate_artifact_archive(
                    artifact_metadata(),
                    archive,
                    artifact_id=ARTIFACT_ID,
                    artifact_name=ARTIFACT_NAME,
                    run_id=RUN_ID,
                    run_attempt=RUN_ATTEMPT,
                    target_sha=SHA,
                    artifact_digest=DIGEST,
                    repository=REPOSITORY,
                    workflow_file=WORKFLOW_FILE,
                )

    def test_artifact_identity_uses_independent_run_metadata_without_rewriting_api_data(self) -> None:
        artifact = artifact_metadata()
        # The artifact endpoint can omit the workflow path/repository fields;
        # the separately fetched run endpoint supplies them.  The validator
        # must compare overlap and leave the API object untouched.
        artifact["workflow_run"] = {
            "id": int(RUN_ID),
            "head_sha": SHA,
            "run_attempt": int(RUN_ATTEMPT),
        }
        run_metadata = artifact_metadata()["workflow_run"]
        accepted = validate_artifact_metadata(
            artifact,
            artifact_id=ARTIFACT_ID,
            artifact_name=ARTIFACT_NAME,
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            target_sha=SHA,
            artifact_digest=DIGEST,
            repository=REPOSITORY,
            workflow_file=WORKFLOW_FILE,
            run_metadata=run_metadata,
        )
        self.assertEqual(accepted["workflow_file"], WORKFLOW_FILE)
        self.assertNotIn("path", artifact["workflow_run"])
        mismatched = deepcopy(run_metadata)
        mismatched["path"] = ".github/workflows/another.yml"
        with self.assertRaises(ExternalLoadProvenanceError):
            validate_artifact_metadata(
                artifact,
                artifact_id=ARTIFACT_ID,
                artifact_name=ARTIFACT_NAME,
                run_id=RUN_ID,
                run_attempt=RUN_ATTEMPT,
                target_sha=SHA,
                artifact_digest=DIGEST,
                repository=REPOSITORY,
                workflow_file=WORKFLOW_FILE,
                run_metadata=mismatched,
            )

    def test_trusted_profile_data_is_the_only_candidate_boundary(self) -> None:
        trusted = {
            "profile_id": PROFILE_ID,
            "profile_version": 2,
            "fixture": {
                "tournament_count": 1,
                "users_per_tournament": 20,
                "setup_concurrency": 4,
                "max_total_users": 20,
            },
            "traffic": {
                "timeout_seconds": 30,
                "spread_seconds": 0,
                "retry": {"max_retries": 0},
            },
            "execution": {
                "dispatchable": True,
                "operator_confirmation": "RUN-PRODUCTION-EXTERNAL-LOAD",
                "require_exact_observer_binding": True,
            },
        }
        contract = validate_profile_data(
            deepcopy(trusted),
            trusted,
            profile_id=PROFILE_ID,
            source_sha=SHA,
            trusted_runner_sha=TRUSTED_SHA,
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
        )
        self.assertEqual(contract["profile_digest"], hashlib.sha256(json.dumps(trusted, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        candidate = deepcopy(trusted)
        candidate["execution"]["sentinel"] = "must-not-run"  # type: ignore[index]
        with self.assertRaises(ExternalLoadProvenanceError):
            validate_profile_data(
                candidate,
                trusted,
                profile_id=PROFILE_ID,
                source_sha=SHA,
                trusted_runner_sha=TRUSTED_SHA,
                run_id=RUN_ID,
                run_attempt=RUN_ATTEMPT,
            )
        candidate = deepcopy(trusted)
        candidate["session_token"] = "secret"  # type: ignore[index]
        with self.assertRaises(ExternalLoadProvenanceError):
            validate_profile_data(
                candidate,
                trusted,
                profile_id=PROFILE_ID,
                source_sha=SHA,
                trusted_runner_sha=TRUSTED_SHA,
                run_id=RUN_ID,
                run_attempt=RUN_ATTEMPT,
            )
        self.assertEqual(
            validate_profile_contract(
                {
                    **contract,
                    "fixture": dict(contract["fixture"]),
                },
                profile_id=PROFILE_ID,
                source_sha=SHA,
                trusted_runner_sha=TRUSTED_SHA,
                run_id=RUN_ID,
                run_attempt=RUN_ATTEMPT,
            )["trusted_runner_sha"],
            TRUSTED_SHA,
        )

    def test_full_report_and_status_binding_rejects_wrong_trusted_or_profile_identity(self) -> None:
        base_report = {
            "source_git_sha": SHA,
            "external_run_id": RUN_ID,
            "authoritative": True,
            "dispatchable": True,
        }
        report = attach_report_provenance(
            base_report,
            target_sha=SHA,
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            trusted_runner_sha=TRUSTED_SHA,
            profile_id=PROFILE_ID,
            profile_version=2,
            profile_digest=DIGEST,
        )
        self.assertEqual(
            validate_report_provenance(
                report,
                target_sha=SHA,
                run_id=RUN_ID,
                run_attempt=RUN_ATTEMPT,
                trusted_runner_sha=TRUSTED_SHA,
                profile_id=PROFILE_ID,
                profile_version=2,
                profile_digest=DIGEST,
            )["trusted_runner_sha"],
            TRUSTED_SHA,
        )
        status = build_load_status(
            status=0,
            report_ready=True,
            target_sha=SHA,
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            trusted_runner_sha=TRUSTED_SHA,
            profile_id=PROFILE_ID,
            profile_version=2,
            profile_digest=DIGEST,
        )
        for field, value in (("trusted_runner_sha", SHA), ("profile_digest", "d" * 64), ("profile_id", "other-v1")):
            mutated = dict(status)
            mutated[field] = value
            with self.subTest(field=field), self.assertRaises(ExternalLoadProvenanceError):
                validate_load_status(
                    mutated,
                    target_sha=SHA,
                    run_id=RUN_ID,
                    run_attempt=RUN_ATTEMPT,
                    trusted_runner_sha=TRUSTED_SHA,
                    profile_id=PROFILE_ID,
                    profile_version=2,
                    profile_digest=DIGEST,
                )
        for digest in ("", "d" * 63, "D" * 64, "sha256:not-a-digest"):
            with self.subTest(digest=digest):
                with self.assertRaises(ExternalLoadProvenanceError):
                    validate_artifact_metadata(
                        artifact_metadata(),
                        artifact_id=ARTIFACT_ID,
                        artifact_name=ARTIFACT_NAME,
                        run_id=RUN_ID,
                        run_attempt=RUN_ATTEMPT,
                        target_sha=SHA,
                        artifact_digest=digest,
                        repository=REPOSITORY,
                        workflow_file=WORKFLOW_FILE,
                    )

    def test_load_status_requires_success_and_exact_run_metadata(self) -> None:
        self.assertEqual(
            validate_load_status(
                load_status(),
                target_sha=SHA,
                run_id=RUN_ID,
                run_attempt=RUN_ATTEMPT,
            )["status"],
            0,
        )
        for field, value in (
            ("run_id", "999"),
            ("run_attempt", "3"),
            ("target_sha", "c" * 40),
            ("status", 1),
            ("report_ready", False),
        ):
            with self.subTest(field=field):
                candidate = load_status()
                candidate[field] = value
                with self.assertRaises(ExternalLoadProvenanceError):
                    validate_load_status(
                        candidate,
                        target_sha=SHA,
                        run_id=RUN_ID,
                        run_attempt=RUN_ATTEMPT,
                    )
        candidate = load_status()
        candidate.pop("run_attempt")
        with self.assertRaises(ExternalLoadProvenanceError):
            validate_load_status(
                candidate,
                target_sha=SHA,
                run_id=RUN_ID,
                run_attempt=RUN_ATTEMPT,
            )

    def test_report_provenance_requires_failure_bearing_metadata(self) -> None:
        report = {
            "source_git_sha": SHA,
            "external_run_id": RUN_ID,
            "authoritative": True,
            "dispatchable": True,
        }
        self.assertEqual(
            validate_report_provenance(report, target_sha=SHA, run_id=RUN_ID)[
                "source_git_sha"
            ],
            SHA,
        )
        for field, value in (
            ("source_git_sha", "c" * 40),
            ("external_run_id", "999"),
            ("authoritative", False),
            ("dispatchable", False),
        ):
            with self.subTest(field=field):
                candidate = deepcopy(report)
                candidate[field] = value
                with self.assertRaises(ExternalLoadProvenanceError):
                    validate_report_provenance(
                        candidate,
                        target_sha=SHA,
                        run_id=RUN_ID,
                    )

        unknown = deepcopy(report)
        unknown["opaque_metadata"] = "eyJhbGciOiJIUzI1NiJ9.secret-like-value"
        with self.assertRaisesRegex(ExternalLoadProvenanceError, "unknown top-level"):
            validate_report_provenance(unknown, target_sha=SHA, run_id=RUN_ID)

    def test_final_provenance_binds_every_published_artifact_and_rejects_mutations(self) -> None:
        artifacts = [
            {
                "artifact_id": 1,
                "artifact_name": f"platform-production-external-load-input-{RUN_ID}-{RUN_ATTEMPT}",
                "artifact_digest": DIGEST,
                "size_in_bytes": len(ARCHIVE_BYTES),
                "repository": REPOSITORY,
                "workflow_file": WORKFLOW_FILE,
            },
            {
                "artifact_id": 2,
                "artifact_name": f"platform-production-external-load-client-{RUN_ID}-{RUN_ATTEMPT}",
                "artifact_digest": DIGEST,
                "size_in_bytes": len(ARCHIVE_BYTES),
                "repository": REPOSITORY,
                "workflow_file": WORKFLOW_FILE,
            },
            {
                "artifact_id": 3,
                "artifact_name": f"platform-production-external-load-origin-{RUN_ID}-{RUN_ATTEMPT}",
                "artifact_digest": DIGEST,
                "size_in_bytes": len(ARCHIVE_BYTES),
                "repository": REPOSITORY,
                "workflow_file": WORKFLOW_FILE,
            },
        ]
        provenance = build_final_provenance(
            artifacts,
            source_sha=SHA,
            trusted_runner_sha=TRUSTED_SHA,
            profile_id=PROFILE_ID,
            profile_version=2,
            profile_digest=DIGEST,
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            repository=REPOSITORY,
            workflow_file=WORKFLOW_FILE,
        )
        self.assertEqual(provenance["trusted_workflow_file"], ".github/workflows/platform-production-external-load-trusted.yml")
        self.assertEqual(len(provenance["artifacts"]), 3)
        self.assertEqual(validate_final_provenance(
            provenance,
            source_sha=SHA,
            trusted_workflow_file=provenance["trusted_workflow_file"],
            trusted_runner_sha=TRUSTED_SHA,
            profile_id=PROFILE_ID,
            profile_version=2,
            profile_digest=DIGEST,
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            repository=REPOSITORY,
            workflow_file=WORKFLOW_FILE,
        ), provenance)
        for field, value in (
            ("source_git_sha", "b" * 40),
            ("trusted_runner_sha", "d" * 40),
            ("workflow_file", ".github/workflows/other.yml"),
        ):
            mutated = deepcopy(provenance)
            mutated[field] = value
            with self.subTest(field=field), self.assertRaises(ExternalLoadProvenanceError):
                validate_final_provenance(
                    mutated,
                    source_sha=SHA,
                    trusted_workflow_file=provenance["trusted_workflow_file"],
                    trusted_runner_sha=TRUSTED_SHA,
                    profile_id=PROFILE_ID,
                    profile_version=2,
                    profile_digest=DIGEST,
                    run_id=RUN_ID,
                    run_attempt=RUN_ATTEMPT,
                    repository=REPOSITORY,
                    workflow_file=WORKFLOW_FILE,
                )

    def test_cli_rejects_malformed_json_and_accepts_exact_handoffs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "metadata.json"
            status = root / "load-status.json"
            report = root / "report.json"
            metadata.write_text(json.dumps(artifact_metadata()), encoding="utf-8")
            status.write_text(json.dumps(load_status()), encoding="utf-8")
            report.write_text(
                json.dumps(
                    {
                        "source_git_sha": SHA,
                        "external_run_id": RUN_ID,
                        "authoritative": True,
                        "dispatchable": True,
                    }
                ),
                encoding="utf-8",
            )
            archive = root / "archive.zip"
            archive.write_bytes(ARCHIVE_BYTES)
            self.assertEqual(
                main(
                    [
                        "artifact",
                        "--metadata",
                        str(metadata),
                        "--artifact-id",
                        ARTIFACT_ID,
                        "--artifact-name",
                        ARTIFACT_NAME,
                        "--run-id",
                        RUN_ID,
                        "--run-attempt",
                        RUN_ATTEMPT,
                        "--target-sha",
                        SHA,
                        "--digest",
                        DIGEST,
                        "--repository",
                        REPOSITORY,
                        "--workflow-file",
                        WORKFLOW_FILE,
                        "--archive",
                        str(archive),
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "load-status",
                        "--path",
                        str(status),
                        "--target-sha",
                        SHA,
                        "--run-id",
                        RUN_ID,
                        "--run-attempt",
                        RUN_ATTEMPT,
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "report",
                        "--path",
                        str(report),
                        "--target-sha",
                        SHA,
                        "--run-id",
                        RUN_ID,
                    ]
                ),
                0,
            )
            metadata.write_text("{\"id\":", encoding="utf-8")
            self.assertEqual(
                main(
                    [
                        "artifact",
                        "--metadata",
                        str(metadata),
                        "--artifact-id",
                        ARTIFACT_ID,
                        "--artifact-name",
                        ARTIFACT_NAME,
                        "--run-id",
                        RUN_ID,
                        "--run-attempt",
                        RUN_ATTEMPT,
                        "--target-sha",
                        SHA,
                        "--digest",
                        DIGEST,
                        "--repository",
                        REPOSITORY,
                        "--workflow-file",
                        WORKFLOW_FILE,
                        "--archive",
                        str(archive),
                    ]
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
