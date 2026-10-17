"""Fail-closed contracts for the source-bound backup success proof."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import stat
import unittest
import warnings
import zipfile
from typing import Any
from unittest import mock

from tools import platform_backup_success_guard as backup_guard


SECURITY_WORKFLOW = ".github/workflows/platform-security.yml"
BACKUP_WORKFLOW = ".github/workflows/platform-production-backup.yml"
BACKUP_JOB = "Create restore-verified backup"
SECURITY_RUN_ID = 741_002
SECURITY_ATTEMPT = 2
BACKUP_RUN_ID = 741_991
BACKUP_ATTEMPT = 1
SOURCE_SHA = "85b287932efe846e126edf4b6fcdaa9b412cc000"
RECEIPT_MEMBER = "platform-backup-success-receipt.json"


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _binding(**overrides: str) -> backup_guard.BackupBinding:
    values = {
        "repository": "StrayForest/old_sparky",
        "ref": "refs/heads/dev",
        "source_security_run_id": str(SECURITY_RUN_ID),
        "source_security_run_attempt": str(SECURITY_ATTEMPT),
        "source_sha": SOURCE_SHA,
    }
    values.update(overrides)
    return backup_guard.BackupBinding(**values)


def _receipt(**overrides: object) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema": 1,
        "kind": "platform_source_bound_backup_success",
        "repository": "StrayForest/old_sparky",
        "ref": "refs/heads/dev",
        "operation": "create",
        "source_security_run_id": str(SECURITY_RUN_ID),
        "source_security_run_attempt": str(SECURITY_ATTEMPT),
        "source_sha": SOURCE_SHA,
        "backup_workflow_path": BACKUP_WORKFLOW,
        "backup_run_id": str(BACKUP_RUN_ID),
        "backup_run_attempt": str(BACKUP_ATTEMPT),
        "backup_head_sha": SOURCE_SHA,
        "archive_name": "platformdb-20261012T191000Z.dump",
        "format_version": 2,
        "archive_sha256": "a" * 64,
        "archive_size_bytes": 8_192,
        "created": True,
        "restore_verified": True,
        "alembic_revision_verified": True,
        "restored_table_count": 137,
        "preexisting_archives_preserved": True,
        "rotation_mode": "preserve-existing",
    }
    receipt.update(overrides)
    return receipt


def _zip_receipt(
    receipt_bytes: bytes | None,
    *,
    entries: list[tuple[str, bytes, int]] | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=compression) as archive:
        if entries is not None:
            for name, value, mode in entries:
                item = zipfile.ZipInfo(name)
                item.compress_type = compression
                item.external_attr = (mode | 0o600) << 16
                archive.writestr(item, value)
        elif receipt_bytes is not None:
            item = zipfile.ZipInfo(RECEIPT_MEMBER)
            item.compress_type = compression
            item.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(item, receipt_bytes)
    return payload.getvalue()


def _receipt_zip(**overrides: object) -> bytes:
    body = json.dumps(_receipt(**overrides), sort_keys=True, separators=(",", ":")).encode()
    return _zip_receipt(body)


def _gh_time(value: str) -> str:
    return f"2026-10-12T{value}Z"


class FakeBackupProofApi:
    """In-memory GitHub API projection; methods perform no network I/O."""

    def __init__(self, archive: bytes | None = None) -> None:
        self.archive = archive if archive is not None else _receipt_zip()
        self.security = {
            "id": SECURITY_RUN_ID,
            "run_attempt": SECURITY_ATTEMPT,
            "path": SECURITY_WORKFLOW,
            "event": "push",
            "head_branch": "dev",
            "head_sha": SOURCE_SHA,
            "status": "completed",
            "conclusion": "success",
            "run_started_at": _gh_time("18:59:00"),
            "updated_at": _gh_time("19:10:00"),
        }
        self.security_job_rows: list[dict[str, Any]] = [
            {
                "id": 911,
                "run_id": SECURITY_RUN_ID,
                "run_attempt": SECURITY_ATTEMPT,
                "head_sha": SOURCE_SHA,
                "name": "Security gate",
                "status": "completed",
                "conclusion": "success",
                "started_at": _gh_time("19:00:00"),
                "completed_at": _gh_time("19:10:00"),
            },
            {
                "id": 912,
                "run_id": SECURITY_RUN_ID,
                "run_attempt": SECURITY_ATTEMPT,
                "head_sha": SOURCE_SHA,
                "name": "Optional route gate",
                "status": "completed",
                "conclusion": "skipped",
                "started_at": _gh_time("19:00:00"),
                "completed_at": _gh_time("19:01:00"),
            },
        ]
        self.workflow = {"id": 901, "path": BACKUP_WORKFLOW}
        self.runs: list[dict[str, Any]] = [
            {
                "id": BACKUP_RUN_ID,
                "workflow_id": 901,
                "run_attempt": BACKUP_ATTEMPT,
                "path": BACKUP_WORKFLOW,
                "event": "workflow_dispatch",
                "head_branch": "dev",
                "head_sha": SOURCE_SHA,
                "display_title": (
                    f"Platform backup/probe create s{SECURITY_RUN_ID}."
                    f"{SECURITY_ATTEMPT} {SOURCE_SHA}"
                ),
                "status": "completed",
                "conclusion": "success",
                "created_at": _gh_time("19:01:00"),
                "run_started_at": _gh_time("19:02:00"),
                "updated_at": _gh_time("19:10:00"),
            }
        ]
        self.jobs: list[dict[str, Any]] = [
            {
                "id": 902,
                "run_id": BACKUP_RUN_ID,
                "run_attempt": BACKUP_ATTEMPT,
                "head_sha": SOURCE_SHA,
                "name": BACKUP_JOB,
                "started_at": _gh_time("19:03:00"),
                "completed_at": _gh_time("19:09:00"),
                "status": "completed",
                "conclusion": "success",
            }
        ]
        self.artifacts: list[dict[str, Any]] = [
            {
                "id": 903,
                "name": f"platform-backup-success-{BACKUP_RUN_ID}-{BACKUP_ATTEMPT}",
                "expired": False,
                "size_in_bytes": len(self.archive),
                "created_at": _gh_time("19:08:00"),
                "digest": f"sha256:{_digest(self.archive)}",
                "workflow_run": {"id": BACKUP_RUN_ID, "head_sha": SOURCE_SHA},
            }
        ]
        self.dev_sha = SOURCE_SHA
        self.downloads: list[tuple[int, int, int]] = []
        self.calls: list[str] = []
        self.job_queries: list[tuple[str, str, str]] = []

    def security_run(self, run_id: str) -> dict[str, Any]:
        self.calls.append("security_run")
        return dict(self.security)

    def security_jobs(self, run_id: str, attempt: str) -> list[dict[str, Any]]:
        self.calls.append("security_jobs")
        self.job_queries.append(("security", run_id, attempt))
        return [dict(job) for job in self.security_job_rows]

    def backup_workflow(self, path: str) -> dict[str, Any]:
        self.calls.append("backup_workflow")
        return dict(self.workflow)

    def backup_runs(
        self, workflow_id: int, branch: str, event: str, head_sha: str
    ) -> list[dict[str, Any]]:
        self.calls.append("backup_runs")
        return [dict(run) for run in self.runs]

    def backup_jobs(self, run_id: str, attempt: str) -> list[dict[str, Any]]:
        self.calls.append("backup_jobs")
        self.job_queries.append(("backup", run_id, attempt))
        return [dict(job) for job in self.jobs]

    def backup_artifacts(self, run_id: int) -> list[dict[str, Any]]:
        self.calls.append("backup_artifacts")
        return [dict(artifact) for artifact in self.artifacts]

    def download_artifact(self, run_id: int, artifact_id: int, max_bytes: int) -> bytes:
        self.calls.append("download_artifact")
        self.downloads.append((run_id, artifact_id, max_bytes))
        return self.archive

    def current_dev_sha(self) -> str:
        self.calls.append("current_dev_sha")
        return self.dev_sha


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.offset = 0
        self.read_sizes: list[int] = []

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read1(self, size: int) -> bytes:
        self.read_sizes.append(size)
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeRedirectingOpener:
    """Drive urllib's redirect callback without opening a socket."""

    def __init__(self, handler: object, *, locations: tuple[str, ...], body: bytes) -> None:
        self.handler = handler
        self.locations = locations
        self.body = body
        self.initial_request = None
        self.redirected_requests = []
        self.timeout = None

    def open(self, request, *, timeout: float) -> FakeHttpResponse:
        self.initial_request = request
        self.timeout = timeout
        current_request = request
        for location in self.locations:
            current_request = self.handler.redirect_request(
                current_request,
                None,
                302,
                "Found",
                {"Location": location},
                location,
            )
            self.redirected_requests.append(current_request)
        return FakeHttpResponse(self.body)


class BackupSuccessGuardTests(unittest.TestCase):
    def _assert_receipt_rejected(self, receipt: dict[str, object]) -> None:
        body = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        archive = _zip_receipt(body)
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard.validate_receipt_archive(
                archive,
                f"sha256:{_digest(archive)}",
                _binding(),
                backup_run_id=str(BACKUP_RUN_ID),
                backup_run_attempt=str(BACKUP_ATTEMPT),
            )

    def test_exact_bound_receipt_archive_is_accepted(self) -> None:
        archive = _receipt_zip()
        result = backup_guard.validate_receipt_archive(
            archive,
            f"sha256:{_digest(archive)}",
            _binding(),
            backup_run_id=str(BACKUP_RUN_ID),
            backup_run_attempt=str(BACKUP_ATTEMPT),
        )

        self.assertEqual(result, _receipt())

    def test_exact_completed_backup_proof_returns_single_receipt(self) -> None:
        api = FakeBackupProofApi()
        result = backup_guard.wait_for_backup_success(api, _binding())

        self.assertEqual(result, _receipt())
        self.assertEqual(api.downloads, [(str(BACKUP_RUN_ID), 903, 1_048_576)])
        self.assertEqual(api.calls.count("download_artifact"), 1)
        self.assertEqual(
            api.job_queries,
            [
                ("security", str(SECURITY_RUN_ID), str(SECURITY_ATTEMPT)),
                ("backup", str(BACKUP_RUN_ID), str(BACKUP_ATTEMPT)),
            ],
        )

    def test_dev_moving_during_artifact_download_invalidates_the_proof(self) -> None:
        api = FakeBackupProofApi()
        original_download = api.download_artifact

        def move_dev_after_download(run_id: str, artifact_id: int, max_bytes: int) -> bytes:
            payload = original_download(run_id, artifact_id, max_bytes)
            api.dev_sha = "b" * 40
            return payload

        api.download_artifact = move_dev_after_download
        with self.assertRaisesRegex(backup_guard.BackupProofError, "dev moved"):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertEqual(api.calls.count("current_dev_sha"), 2)

    def test_missing_and_malformed_receipt_archives_fail_closed(self) -> None:
        malformed = _zip_receipt(b"{not-json")
        duplicate_json_key = _zip_receipt(
            b'{"schema":1,"schema":1}'
        )
        missing = _zip_receipt(None, entries=[])
        for archive in (malformed, duplicate_json_key, missing, b"not-a-zip"):
            with self.subTest(archive=archive[:12]), self.assertRaises(
                backup_guard.BackupProofError
            ):
                backup_guard.validate_receipt_archive(
                    archive,
                    f"sha256:{_digest(archive)}",
                    _binding(),
                    backup_run_id=str(BACKUP_RUN_ID),
                    backup_run_attempt=str(BACKUP_ATTEMPT),
                )

    def test_duplicate_receipt_entries_and_unexpected_members_fail_closed(self) -> None:
        body = json.dumps(_receipt(), sort_keys=True, separators=(",", ":")).encode()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            duplicate = _zip_receipt(
                None,
                entries=[
                    (RECEIPT_MEMBER, body, stat.S_IFREG),
                    (RECEIPT_MEMBER, body, stat.S_IFREG),
                ],
            )
        extra = _zip_receipt(
            None,
            entries=[
                (RECEIPT_MEMBER, body, stat.S_IFREG),
                ("extra.json", b"{}", stat.S_IFREG),
            ],
        )
        for archive in (duplicate, extra):
            with self.subTest(digest=_digest(archive)), self.assertRaises(
                backup_guard.BackupProofError
            ):
                backup_guard.validate_receipt_archive(
                    archive,
                    f"sha256:{_digest(archive)}",
                    _binding(),
                    backup_run_id=str(BACKUP_RUN_ID),
                    backup_run_attempt=str(BACKUP_ATTEMPT),
                )

    def test_traversal_and_symlink_members_fail_closed(self) -> None:
        body = json.dumps(_receipt(), sort_keys=True, separators=(",", ":")).encode()
        traversal = _zip_receipt(
            None, entries=[("../platform-backup-success-receipt.json", body, stat.S_IFREG)]
        )
        legacy_name = _zip_receipt(
            None, entries=[("backup-success-receipt.json", body, stat.S_IFREG)]
        )
        symlink = _zip_receipt(
            None, entries=[(RECEIPT_MEMBER, body, stat.S_IFLNK)]
        )
        for archive in (legacy_name, traversal, symlink):
            with self.subTest(digest=_digest(archive)), self.assertRaises(
                backup_guard.BackupProofError
            ):
                backup_guard.validate_receipt_archive(
                    archive,
                    f"sha256:{_digest(archive)}",
                    _binding(),
                    backup_run_id=str(BACKUP_RUN_ID),
                    backup_run_attempt=str(BACKUP_ATTEMPT),
                )

    def test_expansion_limit_and_api_digest_mismatch_fail_closed(self) -> None:
        ordinary = _receipt_zip()
        oversized_member = _zip_receipt(
            json.dumps(_receipt(), sort_keys=True, separators=(",", ":")).encode()
            + b" " * 4_096
        )
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard.validate_receipt_archive(
                oversized_member,
                f"sha256:{_digest(oversized_member)}",
                _binding(),
                backup_run_id=str(BACKUP_RUN_ID),
                backup_run_attempt=str(BACKUP_ATTEMPT),
            )
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard.validate_receipt_archive(
                ordinary,
                "sha256:" + "0" * 64,
                _binding(),
                backup_run_id=str(BACKUP_RUN_ID),
                backup_run_attempt=str(BACKUP_ATTEMPT),
            )

        oversized_zip = _zip_receipt(
            b"x" * (backup_guard.MAX_ARTIFACT_ZIP_BYTES + 1),
            compression=zipfile.ZIP_STORED,
        )
        self.assertGreater(len(oversized_zip), backup_guard.MAX_ARTIFACT_ZIP_BYTES)
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard.validate_receipt_archive(
                oversized_zip,
                f"sha256:{_digest(oversized_zip)}",
                _binding(),
                backup_run_id=str(BACKUP_RUN_ID),
                backup_run_attempt=str(BACKUP_ATTEMPT),
            )

    def test_closed_receipt_rejects_missing_extra_and_false_fields(self) -> None:
        cases = (
            {key: value for key, value in _receipt().items() if key != "restore_verified"},
            _receipt(unrecognized="ignored fields are forbidden"),
            _receipt(restore_verified=False),
            _receipt(created=False),
            _receipt(alembic_revision_verified=False),
            _receipt(preexisting_archives_preserved=False),
            _receipt(restored_table_count=0),
            _receipt(archive_size_bytes=0),
        )
        for receipt in cases:
            with self.subTest(keys=tuple(receipt)):
                self._assert_receipt_rejected(receipt)

    def test_receipt_source_and_backup_attempt_bindings_are_exact(self) -> None:
        mismatches = (
            {"repository": "other/repo"},
            {"ref": "refs/heads/feature"},
            {"operation": "verify-existing"},
            {"source_security_run_id": SECURITY_RUN_ID + 1},
            {"source_security_run_attempt": SECURITY_ATTEMPT + 1},
            {"source_sha": "b" * 40},
            {"backup_workflow_path": ".github/workflows/other.yml"},
            {"backup_run_id": BACKUP_RUN_ID + 1},
            {"backup_run_attempt": BACKUP_ATTEMPT + 1},
            {"backup_head_sha": "c" * 40},
            {"archive_name": "platformdb-20261012T191000Z.dump/extra"},
            {"format_version": 1},
            {"rotation_mode": "rotate"},
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                self._assert_receipt_rejected(_receipt(**mismatch))

    def test_current_dev_must_match_the_expected_source(self) -> None:
        api = FakeBackupProofApi()
        api.dev_sha = "d" * 40
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertNotIn("download_artifact", api.calls)

    def test_security_run_path_ref_attempt_and_conclusion_are_bound(self) -> None:
        mutations = (
            {"id": SECURITY_RUN_ID + 1},
            {"path": ".github/workflows/other.yml"},
            {"event": "workflow_dispatch"},
            {"head_branch": "other"},
            {"head_sha": "e" * 40},
            {"run_attempt": SECURITY_ATTEMPT - 1},
            {"status": "in_progress"},
            {"conclusion": "failure"},
            {"updated_at": None},
            {"updated_at": _gh_time("18:58:59")},
        )
        for mutation in mutations:
            api = FakeBackupProofApi()
            api.security.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(
                backup_guard.BackupProofError
            ):
                backup_guard.wait_for_backup_success(api, _binding())
            self.assertNotIn("download_artifact", api.calls)

        job_mutations = (
            {"id": None},
            {"run_id": SECURITY_RUN_ID + 1},
            {"run_attempt": SECURITY_ATTEMPT + 1},
            {"run_attempt": True},
            {"run_attempt": None},
            {"head_sha": "b" * 40},
            {"head_sha": None},
            {"status": "in_progress"},
            {"status": []},
            {"conclusion": "failure"},
            {"conclusion": []},
            {"started_at": None},
            {"started_at": _gh_time("18:58:59")},
            {"completed_at": None},
            {"completed_at": _gh_time("18:59:59")},
            {"completed_at": _gh_time("19:00:00")},
            {"completed_at": _gh_time("19:10:01")},
        )
        for mutation in job_mutations:
            api = FakeBackupProofApi()
            api.security_job_rows[0].update(mutation)
            with self.subTest(security_job=mutation), self.assertRaises(
                backup_guard.BackupProofError
            ):
                backup_guard.wait_for_backup_success(api, _binding())
            self.assertNotIn("download_artifact", api.calls)

        api = FakeBackupProofApi()
        api.security_job_rows[0].pop("run_attempt")
        api.security_job_rows[0].pop("head_sha")
        self.assertEqual(backup_guard.wait_for_backup_success(api, _binding()), _receipt())

        api = FakeBackupProofApi()
        api.security_job_rows.clear()
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertNotIn("download_artifact", api.calls)

        api = FakeBackupProofApi()
        with mock.patch.object(
            api, "security_jobs", side_effect=backup_guard.BackupProofError("API unavailable")
        ), self.assertRaisesRegex(backup_guard.BackupProofError, "API unavailable"):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertNotIn("backup_workflow", api.calls)

        api = FakeBackupProofApi()
        api.security_job_rows.append(dict(api.security_job_rows[0]))
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertNotIn("download_artifact", api.calls)

    def test_unexecuted_baseline_dispatch_finalizer_may_invert_timestamps(self) -> None:
        raw_finalizer = {
            "id": 115_603_232_483,
            "run_id": 38_486_040_017,
            "run_attempt": 1,
            "head_sha": "6a951e4ba7a2e6a71b1f6cd9ec91e07c42893e09",
            "name": backup_guard.SECURITY_DISPATCH_FINALIZER_JOB_NAME,
            "status": "completed",
            "conclusion": "skipped",
            "started_at": "2026-10-13T23:54:50Z",
            "completed_at": "2026-10-13T23:54:49Z",
            "steps": None,
        }
        earlier_executed_job = {
            "id": 115_599_884_616,
            "run_id": 38_486_040_017,
            "run_attempt": 1,
            "head_sha": "6a951e4ba7a2e6a71b1f6cd9ec91e07c42893e09",
            "name": "Security gate",
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-10-13T23:45:00Z",
            "completed_at": "2026-10-13T23:45:25Z",
            "steps": [],
        }
        latest_completion = backup_guard._validate_attempt_jobs(
            [earlier_executed_job, raw_finalizer],
            run_id="38486040017",
            run_attempt="1",
            source_sha="6a951e4ba7a2e6a71b1f6cd9ec91e07c42893e09",
            source_event="push",
            run_started=backup_guard._parse_time("2026-10-13T23:00:00Z", "start"),
            run_updated=backup_guard._parse_time("2026-10-13T23:59:59Z", "update"),
        )
        self.assertEqual(
            latest_completion,
            backup_guard._parse_time("2026-10-13T23:54:49Z", "completion"),
        )
        empty_steps_finalizer = dict(raw_finalizer, id=115_603_232_484, steps=[])
        self.assertEqual(
            backup_guard._validate_attempt_jobs(
                [empty_steps_finalizer],
                run_id="38486040017",
                run_attempt="1",
                source_sha="6a951e4ba7a2e6a71b1f6cd9ec91e07c42893e09",
                source_event="push",
                run_started=backup_guard._parse_time("2026-10-13T23:00:00Z", "start"),
                run_updated=backup_guard._parse_time("2026-10-13T23:59:59Z", "update"),
            ),
            backup_guard._parse_time("2026-10-13T23:54:49Z", "completion"),
        )

        # The public proof path validates the exact push route before using
        # the same no-step exception.
        api = FakeBackupProofApi()
        api.security_job_rows.append(
            {
                "id": 913,
                "run_id": SECURITY_RUN_ID,
                "run_attempt": SECURITY_ATTEMPT,
                "head_sha": SOURCE_SHA,
                "name": backup_guard.SECURITY_DISPATCH_FINALIZER_JOB_NAME,
                "status": "completed",
                "conclusion": "skipped",
                "started_at": _gh_time("19:09:59"),
                "completed_at": _gh_time("19:09:58"),
                "steps": None,
            }
        )

        self.assertEqual(backup_guard.wait_for_backup_success(api, _binding()), _receipt())

        # The exact push route can also skip the conditional release-runtime
        # jobs without starting a runner. GitHub emits these rows with an
        # empty steps list, null runner fields, and a one-second inverted
        # timestamp pair; keep that exception limited to the two known names.
        runtime_run_started = backup_guard._parse_time("2026-10-14T08:07:20Z", "start")
        runtime_run_updated = backup_guard._parse_time("2026-10-14T08:19:10Z", "update")
        runtime_job_base = {
            "run_id": 38_532_904_677,
            "run_attempt": 1,
            "head_sha": "f39830cf1fd2bea7d6df9cd5b909e49d7fc73889",
            "status": "completed",
            "conclusion": "skipped",
            "started_at": "2026-10-14T08:07:21Z",
            "completed_at": "2026-10-14T08:07:20Z",
            "runner_id": None,
            "runner_name": None,
            "runner_group_id": None,
            "runner_group_name": None,
            "labels": ["ubuntu-latest"],
            "steps": [],
        }
        runtime_job_names = (
            "Conditional release runtime fixture",
            "Trusted dev immutable release runtime",
        )
        self.assertEqual(
            frozenset(runtime_job_names), backup_guard.SECURITY_CONDITIONAL_RUNTIME_JOB_NAMES
        )
        for index, name in enumerate(runtime_job_names):
            runtime_job = dict(runtime_job_base, id=115_748_756_510 + index, name=name)
            self.assertEqual(
                backup_guard._validate_attempt_jobs(
                    [
                        {
                            "id": 115_748_756_509 + index,
                            "run_id": 38_532_904_677,
                            "run_attempt": 1,
                            "head_sha": "f39830cf1fd2bea7d6df9cd5b909e49d7fc73889",
                            "name": "Earlier executed security job",
                            "status": "completed",
                            "conclusion": "success",
                            "started_at": "2026-10-14T08:07:20Z",
                            "completed_at": "2026-10-14T08:07:20Z",
                        },
                        runtime_job,
                    ],
                    run_id="38532904677",
                    run_attempt="1",
                    source_sha="f39830cf1fd2bea7d6df9cd5b909e49d7fc73889",
                    source_event="push",
                    run_started=runtime_run_started,
                    run_updated=runtime_run_updated,
                ),
                runtime_run_started,
            )

        rejected_runtime_jobs = [
            dict(runtime_job_base, id=115_748_756_520, name="Unrelated skipped job"),
            dict(
                runtime_job_base,
                id=115_748_756_521,
                name="Conditional release runtime fixture",
                conclusion="success",
            ),
            dict(
                runtime_job_base,
                id=115_748_756_522,
                name="Conditional release runtime fixture",
                steps=[{"name": "unexpected executed step"}],
            ),
            dict(
                runtime_job_base,
                id=115_748_756_523,
                name="Conditional release runtime fixture",
                started_at="2026-10-14T08:07:22Z",
            ),
            dict(
                runtime_job_base,
                id=115_748_756_524,
                name="Conditional release runtime fixture",
                started_at="2026-10-14T08:19:11Z",
            ),
            dict(
                runtime_job_base,
                id=115_748_756_531,
                name="Conditional release runtime fixture",
                completed_at="2026-10-14T08:19:11Z",
            ),
            dict(
                runtime_job_base,
                id=115_748_756_525,
                name="Conditional release runtime fixture",
                runner_name="runner-1",
            ),
            dict(
                runtime_job_base,
                id=115_748_756_527,
                name="Conditional release runtime fixture",
                labels=["ubuntu-24.04"],
            ),
            dict(
                runtime_job_base,
                id=115_748_756_528,
                name="Conditional release runtime fixture",
                labels="ubuntu-latest",
            ),
            dict(
                runtime_job_base,
                id=115_748_756_529,
                name="Conditional release runtime fixture",
                status="in_progress",
            ),
        ]
        missing_steps = dict(
            runtime_job_base,
            id=115_748_756_526,
            name="Conditional release runtime fixture",
        )
        missing_steps.pop("steps")
        rejected_runtime_jobs.append(missing_steps)
        missing_runner = dict(
            runtime_job_base,
            id=115_748_756_530,
            name="Conditional release runtime fixture",
        )
        missing_runner.pop("runner_group_name")
        rejected_runtime_jobs.append(missing_runner)
        for row in rejected_runtime_jobs:
            with self.subTest(runtime_skip=row), self.assertRaises(
                backup_guard.BackupProofError
            ):
                backup_guard._validate_attempt_jobs(
                    [row],
                    run_id="38532904677",
                    run_attempt="1",
                    source_sha="f39830cf1fd2bea7d6df9cd5b909e49d7fc73889",
                    source_event="push",
                    run_started=runtime_run_started,
                    run_updated=runtime_run_updated,
                )
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard._validate_attempt_jobs(
                [runtime_job],
                run_id="38532904677",
                run_attempt="1",
                source_sha="f39830cf1fd2bea7d6df9cd5b909e49d7fc73889",
                source_event="workflow_dispatch",
                run_started=runtime_run_started,
                run_updated=runtime_run_updated,
            )

        # Inverted timestamps remain invalid for executed jobs, including a
        # job that otherwise has the same exact source and successful result.
        executed_api = FakeBackupProofApi()
        executed_api.security_job_rows[0]["completed_at"] = _gh_time("18:59:59")
        with self.assertRaisesRegex(
            backup_guard.BackupProofError,
            "attempt job timestamps are inconsistent with its exact run",
        ):
            backup_guard.wait_for_backup_success(executed_api, _binding())
        self.assertNotIn("download_artifact", executed_api.calls)

        rejected_skips: list[dict[str, Any]] = [
            {
                "id": 913,
                "run_id": SECURITY_RUN_ID,
                "run_attempt": SECURITY_ATTEMPT,
                "head_sha": SOURCE_SHA,
                "name": "status-final",
                "status": "completed",
                "conclusion": "skipped",
                "started_at": _gh_time("19:09:59"),
                "completed_at": _gh_time("19:09:58"),
                "steps": None,
            },
            {
                "id": 914,
                "run_id": SECURITY_RUN_ID,
                "run_attempt": SECURITY_ATTEMPT,
                "head_sha": SOURCE_SHA,
                "name": backup_guard.SECURITY_DISPATCH_FINALIZER_JOB_NAME,
                "status": "completed",
                "conclusion": "skipped",
                "started_at": _gh_time("19:09:59"),
                "completed_at": _gh_time("19:09:58"),
                "steps": [{"name": "unexpected executed step"}],
            },
        ]
        for row in rejected_skips:
            rejected_api = FakeBackupProofApi()
            rejected_api.security_job_rows.append(row)
            with self.subTest(row=row), self.assertRaises(backup_guard.BackupProofError):
                backup_guard.wait_for_backup_success(rejected_api, _binding())
            self.assertNotIn("download_artifact", rejected_api.calls)

        # The skipped dispatch is expected only on the accepted push route.
        api = FakeBackupProofApi()
        api.security_job_rows.append(
            {
                "id": 915,
                "run_id": SECURITY_RUN_ID,
                "name": backup_guard.SECURITY_DISPATCH_FINALIZER_JOB_NAME,
                "status": "completed",
                "conclusion": "skipped",
                "started_at": _gh_time("19:09:59"),
                "completed_at": _gh_time("19:09:58"),
                "steps": None,
            }
        )
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard._validate_attempt_jobs(
                api.security_job_rows,
                run_id=str(SECURITY_RUN_ID),
                run_attempt=str(SECURITY_ATTEMPT),
                source_sha=SOURCE_SHA,
                source_event="workflow_dispatch",
                run_started=backup_guard._parse_time(_gh_time("18:59:00"), "start"),
                run_updated=backup_guard._parse_time(_gh_time("19:10:00"), "update"),
            )

        # Exact captured push row for the conditionally dispatched internal
        # baseline authentication job. Preserve the API's explicit null runner
        # fields and empty steps array; do not infer a no-runner result from
        # missing keys or other falsey values.
        actual_auth_skip = {
            "id": 115_748_756_505,
            "run_id": 38_532_904_677,
            "run_attempt": 1,
            "head_sha": "f39830cf1fd2bea7d6df9cd5b909e49d7fc73889",
            "name": backup_guard.SECURITY_BASELINE_RUNTIME_JOB_NAME,
            "status": "completed",
            "conclusion": "skipped",
            "started_at": "2026-10-14T08:07:21Z",
            "completed_at": "2026-10-14T08:07:20Z",
            "steps": [],
            "runner_id": None,
            "runner_name": None,
            "runner_group_id": None,
            "runner_group_name": None,
        }
        run_started = backup_guard._parse_time("2026-10-14T08:07:20Z", "start")
        run_updated = backup_guard._parse_time("2026-10-14T08:19:10Z", "update")
        older_executed_job = {
            "id": 115_748_756_504,
            "run_id": 38_532_904_677,
            "run_attempt": 1,
            "head_sha": "f39830cf1fd2bea7d6df9cd5b909e49d7fc73889",
            "name": "Earlier executed security job",
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-10-14T08:07:20Z",
            "completed_at": "2026-10-14T08:07:20Z",
        }
        auth_completion = backup_guard._validate_attempt_jobs(
            [older_executed_job, actual_auth_skip],
            run_id="38532904677",
            run_attempt="1",
            source_sha="f39830cf1fd2bea7d6df9cd5b909e49d7fc73889",
            source_event="push",
            run_started=run_started,
            run_updated=run_updated,
        )
        self.assertEqual(
            backup_guard._validate_attempt_jobs(
                [actual_auth_skip],
                run_id="38532904677",
                run_attempt="1",
                source_sha="f39830cf1fd2bea7d6df9cd5b909e49d7fc73889",
                source_event="push",
                run_started=run_started,
                run_updated=run_updated,
            ),
            backup_guard._parse_time("2026-10-14T08:07:20Z", "completion"),
        )
        self.assertEqual(
            auth_completion,
            backup_guard._parse_time("2026-10-14T08:07:20Z", "completion"),
        )
        null_steps_auth_skip = dict(actual_auth_skip, id=115_748_756_506, steps=None)
        self.assertEqual(
            backup_guard._validate_attempt_jobs(
                [null_steps_auth_skip],
                run_id="38532904677",
                run_attempt="1",
                source_sha="f39830cf1fd2bea7d6df9cd5b909e49d7fc73889",
                source_event="push",
                run_started=run_started,
                run_updated=run_updated,
            ),
            backup_guard._parse_time("2026-10-14T08:07:20Z", "completion"),
        )

        security_workflow = (
            Path(__file__).resolve().parents[2] / SECURITY_WORKFLOW
        ).read_text(encoding="utf-8")
        auth_job_source = security_workflow.split("  baseline-runtime-guard:", 1)[1].split(
            "  status-start:", 1
        )[0]
        self.assertIn(
            "if: ${{ github.event_name == 'workflow_dispatch' && inputs.proof_mode == 'baseline-runtime' }}",
            auth_job_source,
        )

        auth_rejections: list[dict[str, Any]] = [
            dict(actual_auth_skip, name="status-final"),
            dict(actual_auth_skip, steps=[{"name": "unexpected step"}]),
            dict(actual_auth_skip, runner_id=0),
            dict(actual_auth_skip, runner_name=""),
            dict(actual_auth_skip, runner_group_id=0),
            dict(actual_auth_skip, runner_group_name=""),
            dict(actual_auth_skip, conclusion="success"),
            dict(actual_auth_skip, status="in_progress"),
            dict(actual_auth_skip, started_at="2026-10-14T08:07:19Z"),
            dict(actual_auth_skip, completed_at="2026-10-14T08:19:11Z"),
        ]
        missing_steps = dict(actual_auth_skip)
        missing_steps.pop("steps")
        auth_rejections.append(missing_steps)
        for runner_field in (
            "runner_id",
            "runner_name",
            "runner_group_id",
            "runner_group_name",
        ):
            missing_runner = dict(actual_auth_skip)
            missing_runner.pop(runner_field)
            auth_rejections.append(missing_runner)
        for row in auth_rejections:
            with self.subTest(auth_skip_row=row), self.assertRaises(
                backup_guard.BackupProofError
            ):
                backup_guard._validate_attempt_jobs(
                    [row],
                    run_id="38532904677",
                    run_attempt="1",
                    source_sha="f39830cf1fd2bea7d6df9cd5b909e49d7fc73889",
                    source_event="push",
                    run_started=run_started,
                    run_updated=run_updated,
                )
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard._validate_attempt_jobs(
                [actual_auth_skip],
                run_id="38532904677",
                run_attempt="1",
                source_sha="f39830cf1fd2bea7d6df9cd5b909e49d7fc73889",
                source_event="workflow_dispatch",
                run_started=run_started,
                run_updated=run_updated,
            )
        with self.assertRaisesRegex(
            backup_guard.BackupProofError,
            "attempt job timestamps are inconsistent with its exact run",
        ):
            backup_guard._validate_attempt_jobs(
                [dict(actual_auth_skip, conclusion="success")],
                run_id="38532904677",
                run_attempt="1",
                source_sha="f39830cf1fd2bea7d6df9cd5b909e49d7fc73889",
                source_event="push",
                run_started=run_started,
                run_updated=run_updated,
            )

    def test_workflow_and_backup_run_attempt_identity_are_bound(self) -> None:
        backup_workflow = (Path(__file__).resolve().parents[2] / BACKUP_WORKFLOW).read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'run-name: "Platform backup/probe ${{ inputs.operation }} '
            's${{ inputs.security_run_id }}.${{ inputs.security_run_attempt }} '
            '${{ inputs.source_sha }}"',
            backup_workflow.splitlines()[1],
        )
        self.assertEqual(
            backup_guard._expected_title(_binding()),
            f"Platform backup/probe create s{SECURITY_RUN_ID}.{SECURITY_ATTEMPT} {SOURCE_SHA}",
        )
        mutations = (
            ("workflow", {"path": ".github/workflows/other.yml"}),
            ("run", {"workflow_id": 902}),
            ("run", {"path": ".github/workflows/other.yml"}),
            ("run", {"event": "push"}),
            ("run", {"head_branch": "other"}),
            ("run", {"head_sha": "e" * 40}),
            ("run", {"run_attempt": BACKUP_ATTEMPT + 1}),
            (
                "run",
                {
                    "display_title": (
                        f"Platform backup create s{SECURITY_RUN_ID}."
                        f"{SECURITY_ATTEMPT} {SOURCE_SHA}"
                    )
                },
            ),
            ("run", {"display_title": "stale backup"}),
            ("run", {"conclusion": "failure"}),
            ("run", {"status": []}),
        )
        for target, mutation in mutations:
            api = FakeBackupProofApi()
            clock = FakeClock()
            if target == "workflow":
                api.workflow.update(mutation)
            else:
                api.runs[0].update(mutation)
            with self.subTest(target=target, mutation=mutation), self.assertRaises(
                backup_guard.BackupProofError
            ):
                backup_guard.wait_for_backup_success(
                    api, _binding(), deadline_seconds=1, clock=clock, sleep=clock.sleep
                )
            self.assertNotIn("download_artifact", api.calls)

        api = FakeBackupProofApi()
        api.runs.append(dict(api.runs[0], id=BACKUP_RUN_ID + 1))
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertNotIn("download_artifact", api.calls)

        # A short nonempty API page is not an end-of-list marker: a second
        # exact candidate on the next page must remain visible to the guard.
        source_api = FakeBackupProofApi()
        hidden_duplicate = dict(source_api.runs[0], id=BACKUP_RUN_ID + 1)
        run_rows = [source_api.runs[0], hidden_duplicate]
        list_api = backup_guard.GitHubBackupProofApi(
            "StrayForest/old_sparky", "https://api.github.com", "gh-token-sentinel"
        )
        listed_pages: list[str] = []

        def list_short_pages(path: str, *, max_bytes: int = 1_048_576) -> dict[str, Any]:
            listed_pages.append(path)
            page = int(path.rsplit("page=", 1)[1])
            start = page - 1
            return {"total_count": 2, "workflow_runs": run_rows[start : start + 1]}

        list_api._json = list_short_pages

        class PagedRunProofApi:
            def __getattr__(self, name: str) -> Any:
                return getattr(source_api, name)

            def backup_runs(
                self, workflow_id: int, branch: str, event: str, head_sha: str
            ) -> list[dict[str, Any]]:
                return list_api.backup_runs(workflow_id, branch, event, head_sha)

        with self.assertRaisesRegex(
            backup_guard.BackupProofError,
            "multiple exact source-bound backup runs are ambiguous",
        ):
            backup_guard.wait_for_backup_success(PagedRunProofApi(), _binding())
        self.assertEqual(len(listed_pages), 2)
        self.assertTrue(listed_pages[-1].endswith("page=2"))
        self.assertNotIn("download_artifact", source_api.calls)

    def test_backup_must_start_before_security_run_completes(self) -> None:
        api = FakeBackupProofApi()
        api.runs[0].update(
            {
                "created_at": _gh_time("19:11:00"),
                "run_started_at": _gh_time("19:11:00"),
                "updated_at": _gh_time("19:12:00"),
            }
        )
        with self.assertRaisesRegex(
            backup_guard.BackupProofError,
            "backup did not reserve its production slot during the source security run",
        ):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertNotIn("download_artifact", api.calls)

        api = FakeBackupProofApi()
        api.runs[0]["created_at"] = _gh_time("18:58:00")
        with self.assertRaisesRegex(
            backup_guard.BackupProofError,
            "backup did not reserve its production slot during the source security run",
        ):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertNotIn("download_artifact", api.calls)

        api = FakeBackupProofApi()
        api.jobs[0]["started_at"] = _gh_time("19:11:00")
        api.jobs[0]["completed_at"] = _gh_time("19:11:00")
        api.runs[0]["updated_at"] = _gh_time("19:12:00")
        with self.assertRaisesRegex(
            backup_guard.BackupProofError,
            "production backup job did not start while security was in progress",
        ):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertNotIn("download_artifact", api.calls)

        api = FakeBackupProofApi()
        api.jobs[0]["started_at"] = _gh_time("19:10:00")
        api.jobs[0]["completed_at"] = _gh_time("19:11:00")
        api.runs[0]["updated_at"] = _gh_time("19:12:00")
        with self.assertRaisesRegex(
            backup_guard.BackupProofError,
            "production backup job did not start while security was in progress",
        ):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertNotIn("download_artifact", api.calls)

        # Reservation equality with the conservative source-job completion
        # bound is rejected, even though the run's later updated_at is not.
        api = FakeBackupProofApi()
        api.runs[0].update(
            {"created_at": _gh_time("19:10:00"), "run_started_at": _gh_time("19:10:00")}
        )
        api.jobs[0].update(
            {"started_at": _gh_time("19:10:00"), "completed_at": _gh_time("19:11:00")}
        )
        api.runs[0]["updated_at"] = _gh_time("19:12:00")
        with self.assertRaisesRegex(
            backup_guard.BackupProofError,
            "backup did not reserve its production slot during the source security run",
        ):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertNotIn("download_artifact", api.calls)

    def test_job_and_artifact_must_be_unique_exact_and_successful(self) -> None:
        job_failures = (
            {"name": "Different job"},
            {"run_attempt": BACKUP_ATTEMPT + 1},
            {"run_attempt": True},
            {"run_attempt": None},
            {"head_sha": "b" * 40},
            {"head_sha": None},
            {"run_id": BACKUP_RUN_ID + 1},
            {"conclusion": "failure"},
        )
        for mutation in job_failures:
            api = FakeBackupProofApi()
            clock = FakeClock()
            api.jobs[0].update(mutation)
            with self.subTest(job=mutation), self.assertRaises(
                backup_guard.BackupProofError
            ):
                backup_guard.wait_for_backup_success(
                    api, _binding(), deadline_seconds=1, clock=clock, sleep=clock.sleep
                )
            self.assertNotIn("download_artifact", api.calls)

        # The endpoint itself binds the requested run attempt. The documented
        # job schema need not repeat run_attempt on every row.
        api = FakeBackupProofApi()
        api.jobs[0].pop("run_attempt")
        self.assertEqual(backup_guard.wait_for_backup_success(api, _binding()), _receipt())

        api = backup_guard.GitHubBackupProofApi(
            "StrayForest/old_sparky", "https://api.github.com", "gh-token-sentinel"
        )
        jobs = [
            {
                "id": job_id,
                "run_id": SECURITY_RUN_ID,
                "head_sha": SOURCE_SHA,
                "status": "completed",
                "conclusion": "success",
                "started_at": _gh_time("19:00:00"),
                "completed_at": _gh_time("19:01:00"),
            }
            for job_id in range(1000, 1101)
        ]
        seen_paths: list[str] = []

        def paged_json(path: str, *, max_bytes: int = 1_048_576) -> dict[str, Any]:
            seen_paths.append(path)
            page = int(path.rsplit("page=", 1)[1])
            start = (page - 1) * 100
            return {"total_count": len(jobs), "jobs": jobs[start : start + 100]}

        with mock.patch.object(api, "_json", side_effect=paged_json):
            self.assertEqual(api.security_jobs(str(SECURITY_RUN_ID), str(SECURITY_ATTEMPT)), jobs)
        self.assertEqual(
            seen_paths,
            [
                f"/actions/runs/{SECURITY_RUN_ID}/attempts/{SECURITY_ATTEMPT}/jobs?per_page=100&page=1",
                f"/actions/runs/{SECURITY_RUN_ID}/attempts/{SECURITY_ATTEMPT}/jobs?per_page=100&page=2",
            ],
        )

        backup_api = backup_guard.GitHubBackupProofApi(
            "StrayForest/old_sparky", "https://api.github.com", "gh-token-sentinel"
        )
        with mock.patch.object(
            backup_api,
            "_json",
            return_value={"total_count": 1, "jobs": [dict(jobs[0], run_id=BACKUP_RUN_ID)]},
        ) as request_json:
            backup_api.backup_jobs(str(BACKUP_RUN_ID), str(BACKUP_ATTEMPT))
        request_json.assert_called_once_with(
            f"/actions/runs/{BACKUP_RUN_ID}/attempts/{BACKUP_ATTEMPT}/jobs?per_page=100&page=1",
            max_bytes=2_097_152,
        )

        run_api = backup_guard.GitHubBackupProofApi(
            "StrayForest/old_sparky", "https://api.github.com", "gh-token-sentinel"
        )
        backup_runs = [
            {"id": 20_000 + index, "display_title": f"run {index}"}
            for index in range(205)
        ]
        run_paths: list[str] = []

        def paged_runs(path: str, *, max_bytes: int = 1_048_576) -> dict[str, Any]:
            run_paths.append(path)
            page = int(path.rsplit("page=", 1)[1])
            page_rows = {
                1: backup_runs[:42],
                2: backup_runs[42:142],
                3: backup_runs[142:],
            }[page]
            return {
                "total_count": len(backup_runs),
                "workflow_runs": page_rows,
            }

        with mock.patch.object(run_api, "_json", side_effect=paged_runs):
            self.assertEqual(
                run_api.backup_runs(901, "dev", "workflow_dispatch", SOURCE_SHA),
                backup_runs,
            )
        self.assertEqual(
            run_paths,
            [
                f"/actions/workflows/901/runs?branch=dev&event=workflow_dispatch&head_sha={SOURCE_SHA}&per_page=100&page=1",
                f"/actions/workflows/901/runs?branch=dev&event=workflow_dispatch&head_sha={SOURCE_SHA}&per_page=100&page=2",
                f"/actions/workflows/901/runs?branch=dev&event=workflow_dispatch&head_sha={SOURCE_SHA}&per_page=100&page=3",
            ],
        )

        with mock.patch.object(
            run_api,
            "_json",
            return_value={"total_count": 0, "workflow_runs": []},
        ):
            self.assertEqual(
                run_api.backup_runs(901, "dev", "workflow_dispatch", SOURCE_SHA), []
            )

        run_pagination_failures = (
            # A short first page must continue; an empty next page is premature.
            (
                {"total_count": 101, "workflow_runs": backup_runs[:99]},
                {"total_count": 101, "workflow_runs": []},
            ),
            # A short final page must not be treated as an end-of-list marker.
            (
                {"total_count": 101, "workflow_runs": backup_runs[:100]},
                {"total_count": 101, "workflow_runs": []},
            ),
            # A final page larger than the remaining count must not be accepted.
            (
                {"total_count": 101, "workflow_runs": backup_runs[:100]},
                {"total_count": 101, "workflow_runs": backup_runs[100:102]},
            ),
            (
                {"total_count": 101, "workflow_runs": backup_runs[:100]},
                {"total_count": 102, "workflow_runs": backup_runs[100:101]},
            ),
            (
                {"total_count": 101, "workflow_runs": backup_runs[:100]},
                {"total_count": 101, "workflow_runs": [backup_runs[0]]},
            ),
        )
        for scenario, pages in enumerate(run_pagination_failures):
            with self.subTest(run_pagination_scenario=scenario):
                with mock.patch.object(run_api, "_json", side_effect=pages), self.assertRaises(
                    backup_guard.BackupProofError
                ):
                    run_api.backup_runs(901, "dev", "workflow_dispatch", SOURCE_SHA)

        malformed_run_responses = (
            {"total_count": True, "workflow_runs": []},
            {"total_count": 1, "workflow_runs": ["not a run object"]},
            {"total_count": 1, "workflow_runs": [{"id": True}]},
        )
        for response in malformed_run_responses:
            with self.subTest(malformed_run_response=response):
                with mock.patch.object(run_api, "_json", return_value=response), self.assertRaises(
                    backup_guard.BackupProofError
                ):
                    run_api.backup_runs(901, "dev", "workflow_dispatch", SOURCE_SHA)

        with mock.patch.object(
            run_api,
            "_json",
            return_value={"total_count": 10_001, "workflow_runs": []},
        ), self.assertRaises(backup_guard.BackupProofError):
            run_api.backup_runs(901, "dev", "workflow_dispatch", SOURCE_SHA)

        bounded_rows = [{"id": 30_000 + index} for index in range(100)]
        bounded_pages = [
            {"total_count": 101, "workflow_runs": [row]} for row in bounded_rows
        ]
        with mock.patch.object(run_api, "_json", side_effect=bounded_pages), self.assertRaisesRegex(
            backup_guard.BackupProofError, "pagination exceeded its bound"
        ):
            run_api.backup_runs(901, "dev", "workflow_dispatch", SOURCE_SHA)

        pagination_failures = (
            lambda rows: ({"total_count": 101, "jobs": rows[:99]}, {"total_count": 101, "jobs": rows[99:]}),
            lambda rows: ({"total_count": 101, "jobs": rows[:100]}, {"total_count": 102, "jobs": rows[100:]}),
            lambda rows: ({"total_count": 101, "jobs": rows[:100]}, {"total_count": 101, "jobs": [rows[0]]}),
        )
        for pages in pagination_failures:
            page1, page2 = pages(jobs)
            api = backup_guard.GitHubBackupProofApi(
                "StrayForest/old_sparky", "https://api.github.com", "gh-token-sentinel"
            )
            with mock.patch.object(api, "_json", side_effect=[page1, page2]), self.subTest(pages=pages):
                with self.assertRaises(backup_guard.BackupProofError):
                    api.security_jobs(str(SECURITY_RUN_ID), str(SECURITY_ATTEMPT))

        malformed_responses = (
            {"total_count": 0, "jobs": []},
            {"total_count": True, "jobs": []},
            {"total_count": 1, "jobs": [dict(jobs[0], id=None)]},
            {"total_count": 1, "jobs": [dict(jobs[0], run_id=SECURITY_RUN_ID + 1)]},
        )
        for response in malformed_responses:
            api = backup_guard.GitHubBackupProofApi(
                "StrayForest/old_sparky", "https://api.github.com", "gh-token-sentinel"
            )
            with mock.patch.object(api, "_json", return_value=response), self.subTest(response=response):
                with self.assertRaises(backup_guard.BackupProofError):
                    api.security_jobs(str(SECURITY_RUN_ID), str(SECURITY_ATTEMPT))

        artifact_failures_before_download = (
            {"name": "old-artifact"},
            {"expired": True},
            {"created_at": None},
            {"created_at": _gh_time("19:02:59")},
            {"created_at": _gh_time("19:09:01")},
            {"workflow_run": {"id": BACKUP_RUN_ID + 1, "head_sha": SOURCE_SHA}},
        )
        for mutation in artifact_failures_before_download:
            api = FakeBackupProofApi()
            clock = FakeClock()
            api.artifacts[0].update(mutation)
            with self.subTest(artifact=mutation), self.assertRaises(
                backup_guard.BackupProofError
            ):
                backup_guard.wait_for_backup_success(
                    api, _binding(), deadline_seconds=1, clock=clock, sleep=clock.sleep
                )
            self.assertNotIn("download_artifact", api.calls)

        api = FakeBackupProofApi()
        api.artifacts[0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertEqual(api.calls.count("download_artifact"), 1)

        api = FakeBackupProofApi()
        api.jobs.append(dict(api.jobs[0], id=904))
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertNotIn("download_artifact", api.calls)

        api = FakeBackupProofApi()
        api.artifacts.append(dict(api.artifacts[0], id=905))
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard.wait_for_backup_success(api, _binding())
        self.assertNotIn("download_artifact", api.calls)

    def test_timeout_without_exact_backup_candidate_fails_closed(self) -> None:
        api = FakeBackupProofApi()
        api.runs.clear()
        clock = FakeClock()
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard.wait_for_backup_success(
                api,
                _binding(),
                deadline_seconds=31,
                clock=clock,
                sleep=clock.sleep,
            )
        self.assertGreaterEqual(clock.now, 31)
        self.assertTrue(clock.sleeps)
        self.assertNotIn("download_artifact", api.calls)

    def test_api_json_request_rejects_redirect_without_exposing_credentials(self) -> None:
        token = "gh-token-sentinel"
        signed_location = "https://account.blob.core.windows.net/a?sig=signed-url-sentinel"
        api = backup_guard.GitHubBackupProofApi(
            "StrayForest/old_sparky", "https://api.github.com", token
        )
        openers: list[FakeRedirectingOpener] = []

        def build_opener(handler: object) -> FakeRedirectingOpener:
            opener = FakeRedirectingOpener(
                handler, locations=(signed_location,), body=b"{}"
            )
            openers.append(opener)
            return opener

        with mock.patch.object(backup_guard, "build_opener", side_effect=build_opener):
            with self.assertRaises(backup_guard.BackupProofError) as caught:
                api._request("/actions/runs/741002")

        opener = openers[0]
        self.assertEqual(
            opener.initial_request.full_url,
            "https://api.github.com/repos/StrayForest/old_sparky/actions/runs/741002",
        )
        self.assertEqual(opener.initial_request.get_header("Authorization"), f"Bearer {token}")
        self.assertEqual(opener.redirected_requests, [])
        self.assertNotIn(token, str(caught.exception))
        self.assertNotIn(signed_location, str(caught.exception))

    def test_artifact_download_uses_documented_endpoint_and_drops_auth_on_redirect(self) -> None:
        token = "gh-token-sentinel"
        signed_location = (
            "https://Runner.BLOB.CORE.WINDOWS.NET:443/container/artifact.zip?"
            "sig=signed-url-sentinel"
        )
        body = b"bounded artifact bytes"
        api = backup_guard.GitHubBackupProofApi(
            "StrayForest/old_sparky", "https://api.github.com", token
        )
        openers: list[FakeRedirectingOpener] = []

        def build_opener(handler: object) -> FakeRedirectingOpener:
            opener = FakeRedirectingOpener(handler, locations=(signed_location,), body=body)
            openers.append(opener)
            return opener

        with mock.patch.object(backup_guard, "build_opener", side_effect=build_opener):
            result = api.download_artifact(
                str(BACKUP_RUN_ID), 903, backup_guard.MAX_ARTIFACT_ZIP_BYTES
            )

        opener = openers[0]
        self.assertEqual(result, body)
        self.assertEqual(
            opener.initial_request.full_url,
            "https://api.github.com/repos/StrayForest/old_sparky/actions/artifacts/903/zip",
        )
        self.assertEqual(opener.initial_request.get_header("Authorization"), f"Bearer {token}")
        self.assertEqual(len(opener.redirected_requests), 1)
        redirected = opener.redirected_requests[0]
        self.assertEqual(redirected.full_url, signed_location)
        self.assertIsNone(redirected.get_header("Authorization"))
        self.assertEqual(redirected.get_header("Accept"), "application/octet-stream")
        self.assertNotIn("Authorization", dict(redirected.header_items()))
        self.assertNotIn("GH_TOKEN", dict(redirected.header_items()))

        api = backup_guard.GitHubBackupProofApi(
            "StrayForest/old_sparky", "https://api.github.com", token
        )
        actions_location = "https://cdn.actions.githubusercontent.com/file?sig=other-signed-url"
        with mock.patch.object(
            backup_guard,
            "build_opener",
            side_effect=lambda handler: FakeRedirectingOpener(
                handler, locations=(actions_location,), body=body
            ),
        ):
            self.assertEqual(
                api.download_artifact(
                    str(BACKUP_RUN_ID), 903, backup_guard.MAX_ARTIFACT_ZIP_BYTES
                ),
                body,
            )

    def test_artifact_redirect_boundary_rejects_untrusted_or_second_hops(self) -> None:
        token = "gh-token-sentinel"
        allowed = "https://account.blob.core.windows.net/archive?sig=signed-url-sentinel"
        invalid_locations = (
            "http://account.blob.core.windows.net/archive?sig=signed-url-sentinel",
            "https://account.blob.core.windows.net:444/archive?sig=signed-url-sentinel",
            "https://user:password@account.blob.core.windows.net/archive?sig=signed-url-sentinel",
            "https://account.blob.core.windows.net./archive?sig=signed-url-sentinel",
            "https://evilblob.core.windows.net/archive?sig=signed-url-sentinel",
            "https://account.blob.core.windows.net.attacker.invalid/archive?sig=signed-url-sentinel",
            "https://account.blob.core.windows.net:bad/archive?sig=signed-url-sentinel",
            "https://account.blob.core.windows.net/archive#fragment",
        )
        location_sets = tuple((location,) for location in invalid_locations) + (
            (allowed, allowed),
        )
        for locations in location_sets:
            with self.subTest(locations=locations):
                api = backup_guard.GitHubBackupProofApi(
                    "StrayForest/old_sparky", "https://api.github.com", token
                )
                opener_holder: list[FakeRedirectingOpener] = []

                def build_opener(handler: object) -> FakeRedirectingOpener:
                    opener = FakeRedirectingOpener(handler, locations=locations, body=b"ok")
                    opener_holder.append(opener)
                    return opener

                with mock.patch.object(
                    backup_guard, "build_opener", side_effect=build_opener
                ):
                    with self.assertRaises(backup_guard.BackupProofError) as caught:
                        api.download_artifact(
                            str(BACKUP_RUN_ID), 903, backup_guard.MAX_ARTIFACT_ZIP_BYTES
                        )
                self.assertNotIn(token, str(caught.exception))
                self.assertNotIn("signed-url-sentinel", str(caught.exception))
                self.assertNotIn("sig=", str(caught.exception))

    def test_authenticated_api_body_over_limit_is_rejected(self) -> None:
        api = backup_guard.GitHubBackupProofApi(
            "StrayForest/old_sparky", "https://api.github.com", "gh-token-sentinel"
        )
        openers: list[FakeRedirectingOpener] = []

        def build_opener(handler: object) -> FakeRedirectingOpener:
            opener = FakeRedirectingOpener(handler, locations=(), body=b"12345")
            openers.append(opener)
            return opener

        with mock.patch.object(backup_guard, "build_opener", side_effect=build_opener):
            with self.assertRaises(backup_guard.BackupProofError):
                api._request("/actions/runs/741002", max_bytes=4)
        self.assertEqual(openers[0].initial_request.get_header("Authorization"), "Bearer gh-token-sentinel")
        self.assertEqual(openers[0].timeout, 15)

        api = backup_guard.GitHubBackupProofApi(
            "StrayForest/old_sparky", "https://api.github.com", "gh-token-sentinel"
        )
        oversized_body = b"x" * (backup_guard.MAX_ARTIFACT_ZIP_BYTES + 1)
        with mock.patch.object(
            backup_guard,
            "build_opener",
            side_effect=lambda handler: FakeRedirectingOpener(
                handler,
                locations=("https://account.blob.core.windows.net/file?sig=private",),
                body=oversized_body,
            ),
        ):
            with self.assertRaises(backup_guard.BackupProofError) as caught:
                api.download_artifact(
                    str(BACKUP_RUN_ID), 903, backup_guard.MAX_ARTIFACT_ZIP_BYTES
                )
        self.assertNotIn("gh-token-sentinel", str(caught.exception))
        self.assertNotIn("sig=private", str(caught.exception))

    def test_proof_deadline_expiring_inside_api_call_cannot_return_success(self) -> None:
        clock = FakeClock()
        api = FakeBackupProofApi()

        class SlowBackupJobsApi(FakeBackupProofApi):
            def backup_jobs(self, run_id: str, attempt: str) -> list[dict[str, Any]]:
                jobs = super().backup_jobs(run_id, attempt)
                clock.now = 2.0
                return jobs

        api = SlowBackupJobsApi()
        with self.assertRaises(backup_guard.BackupProofError):
            backup_guard.wait_for_backup_success(
                api,
                _binding(),
                deadline_seconds=1,
                clock=clock,
                sleep=clock.sleep,
            )
        self.assertIn("backup_jobs", api.calls)
        self.assertNotIn("backup_artifacts", api.calls)
        self.assertNotIn("download_artifact", api.calls)


if __name__ == "__main__":
    unittest.main()
