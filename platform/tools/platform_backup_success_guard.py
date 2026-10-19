"""Fail-closed proof that the exact source-bound production backup succeeded.

This module deliberately has no deployment authority.  The auto-deploy
workflow must finish this proof before it reaches its single child-dispatch
step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
import sys
from pathlib import PurePosixPath
import re
import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile
import io


BACKUP_WORKFLOW_PATH = ".github/workflows/platform-production-backup.yml"
SECURITY_WORKFLOW_PATH = ".github/workflows/platform-security.yml"
BACKUP_JOB_NAME = "Create restore-verified backup"
SECURITY_BASELINE_RUNTIME_JOB_NAME = "Authenticate internal baseline runtime proof"
SECURITY_DISPATCH_FINALIZER_JOB_NAME = "Dispatch exact baseline proof finalizer"
SECURITY_CONDITIONAL_RUNTIME_JOB_NAMES = frozenset(
    {
        "Conditional release runtime fixture",
        "Trusted dev immutable release runtime",
    }
)
BACKUP_RECEIPT_NAME = "platform-backup-success-receipt.json"
BACKUP_ARTIFACT_PREFIX = "platform-backup-success-"
MAX_ARTIFACT_ZIP_BYTES = 1_048_576
MAX_RECEIPT_BYTES = 4_096
POLL_INTERVAL_SECONDS = 15.0
POLL_DEADLINE_SECONDS = 9_000.0
ATTEMPT_JOB_SETTLE_DEADLINE_SECONDS = 900.0
ATTEMPT_JOB_PENDING_STATUSES = frozenset(
    {"queued", "in_progress", "requested", "waiting", "pending"}
)

_ID_RE = re.compile(r"[1-9][0-9]{0,19}\Z")
_ATTEMPT_RE = re.compile(r"[1-9][0-9]{0,5}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_API_DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})\Z")
_ARCHIVE_NAME_RE = re.compile(r"platformdb-([0-9]{8}T[0-9]{6}Z)\.dump\Z")

_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "kind",
        "repository",
        "ref",
        "operation",
        "source_security_run_id",
        "source_security_run_attempt",
        "source_sha",
        "backup_workflow_path",
        "backup_run_id",
        "backup_run_attempt",
        "backup_head_sha",
        "archive_name",
        "format_version",
        "archive_sha256",
        "archive_size_bytes",
        "created",
        "restore_verified",
        "alembic_revision_verified",
        "restored_table_count",
        "preexisting_archives_preserved",
        "rotation_mode",
    }
)


class BackupProofError(RuntimeError):
    """An exact source-bound backup proof is missing or invalid."""


@dataclass(frozen=True)
class BackupBinding:
    repository: str
    ref: str
    source_security_run_id: str
    source_security_run_attempt: str
    source_sha: str

    def validate(self) -> None:
        if (
            self.repository != "StrayForest/old_sparky"
            or self.ref != "refs/heads/dev"
            or _ID_RE.fullmatch(self.source_security_run_id) is None
            or _ATTEMPT_RE.fullmatch(self.source_security_run_attempt) is None
            or _SHA_RE.fullmatch(self.source_sha) is None
        ):
            raise BackupProofError("expected source binding is malformed")


class BackupProofApi(Protocol):
    """Small HTTP boundary, injectable in tests without external services."""

    def security_run(self, run_id: str) -> dict[str, Any]: ...

    def security_jobs(self, run_id: str, run_attempt: str) -> list[dict[str, Any]]: ...

    def backup_workflow(self, path: str) -> dict[str, Any]: ...

    def backup_runs(
        self, workflow_id: int, branch: str, event: str, head_sha: str
    ) -> list[dict[str, Any]]: ...

    def backup_jobs(self, run_id: str, run_attempt: str) -> list[dict[str, Any]]: ...

    def backup_artifacts(self, run_id: str) -> list[dict[str, Any]]: ...

    def download_artifact(self, run_id: str, artifact_id: int, max_bytes: int) -> bytes: ...

    def current_dev_sha(self) -> str: ...


def _fail_if(condition: bool, reason: str) -> None:
    if condition:
        raise BackupProofError(reason)


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise BackupProofError(f"{field} is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupProofError(f"{field} is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BackupProofError(f"{field} has no timezone")
    return parsed


def _check_deadline(deadline: float, clock) -> None:
    if clock() >= deadline:
        raise BackupProofError("source-bound backup proof deadline expired")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BackupProofError("receipt contains duplicate JSON keys")
        result[key] = value
    return result


def _validate_receipt(value: Any, expected: BackupBinding, *, backup_run_id: str, backup_run_attempt: str) -> dict[str, Any]:
    expected.validate()
    if not isinstance(value, dict) or frozenset(value) != _RECEIPT_KEYS:
        raise BackupProofError("backup receipt schema is not closed")
    fixed = {
        "schema": 1,
        "kind": "platform_source_bound_backup_success",
        "repository": expected.repository,
        "ref": expected.ref,
        "operation": "create",
        "source_security_run_id": expected.source_security_run_id,
        "source_security_run_attempt": expected.source_security_run_attempt,
        "source_sha": expected.source_sha,
        "backup_workflow_path": BACKUP_WORKFLOW_PATH,
        "backup_run_id": backup_run_id,
        "backup_run_attempt": backup_run_attempt,
        "backup_head_sha": expected.source_sha,
        "format_version": 2,
        "created": True,
        "restore_verified": True,
        "alembic_revision_verified": True,
        "preexisting_archives_preserved": True,
        "rotation_mode": "preserve-existing",
    }
    for key, expected_value in fixed.items():
        if type(value.get(key)) is not type(expected_value) or value.get(key) != expected_value:
            raise BackupProofError(f"backup receipt binding is invalid: {key}")
    if (
        not isinstance(value.get("backup_run_id"), str)
        or _ID_RE.fullmatch(value["backup_run_id"]) is None
        or value["backup_run_id"] != backup_run_id
        or not isinstance(value.get("backup_run_attempt"), str)
        or _ATTEMPT_RE.fullmatch(value["backup_run_attempt"]) is None
        or value["backup_run_attempt"] != backup_run_attempt
    ):
        raise BackupProofError("backup receipt run identity is invalid")
    archive_name = value.get("archive_name")
    name_match = _ARCHIVE_NAME_RE.fullmatch(archive_name) if isinstance(archive_name, str) else None
    if name_match is None:
        raise BackupProofError("backup receipt archive identity is invalid")
    try:
        datetime.strptime(name_match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise BackupProofError("backup receipt archive timestamp is invalid") from exc
    if (
        not isinstance(value.get("archive_sha256"), str)
        or _DIGEST_RE.fullmatch(value["archive_sha256"]) is None
        or type(value.get("archive_size_bytes")) is not int
        or value["archive_size_bytes"] <= 0
        or type(value.get("restored_table_count")) is not int
        or value["restored_table_count"] < 1
    ):
        raise BackupProofError("backup receipt archive verification is invalid")
    return value


def validate_receipt_archive(
    zip_bytes: bytes,
    api_digest: str,
    expected: BackupBinding,
    *,
    backup_run_id: str,
    backup_run_attempt: str,
) -> dict[str, Any]:
    """Validate exact artifact ZIP bytes, its single receipt, and all bindings."""

    expected.validate()
    if (
        not isinstance(zip_bytes, bytes)
        or not 1 <= len(zip_bytes) <= MAX_ARTIFACT_ZIP_BYTES
        or not isinstance(api_digest, str)
    ):
        raise BackupProofError("backup proof artifact is empty or oversized")
    digest_match = _API_DIGEST_RE.fullmatch(api_digest)
    if digest_match is None or hashlib.sha256(zip_bytes).hexdigest() != digest_match.group(1):
        raise BackupProofError("backup proof artifact digest does not match GitHub metadata")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            members = archive.infolist()
            if len(members) != 1:
                raise BackupProofError("backup proof artifact must contain one entry")
            member = members[0]
            mode = (member.external_attr >> 16) & 0xFFFF
            if (
                member.filename != BACKUP_RECEIPT_NAME
                or PurePosixPath(member.filename).is_absolute()
                or ".." in PurePosixPath(member.filename).parts
                or member.is_dir()
                or (mode & 0o170000) not in {0, 0o100000}
                or member.flag_bits & 0x1
                or member.file_size > MAX_RECEIPT_BYTES
                or member.compress_size > MAX_ARTIFACT_ZIP_BYTES
            ):
                raise BackupProofError("backup proof artifact entry is unsafe")
            chunks: list[bytes] = []
            total = 0
            with archive.open(member, "r") as handle:
                while total <= MAX_RECEIPT_BYTES:
                    chunk = handle.read(min(1024, MAX_RECEIPT_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
            if total > MAX_RECEIPT_BYTES:
                raise BackupProofError("backup proof receipt is oversized")
            raw = b"".join(chunks)
    except BackupProofError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        raise BackupProofError("backup proof artifact ZIP is invalid") from exc
    if len(raw) > MAX_RECEIPT_BYTES:
        raise BackupProofError("backup proof receipt is oversized")
    try:
        receipt = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except BackupProofError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupProofError("backup proof receipt JSON is invalid") from exc
    return _validate_receipt(
        receipt,
        expected,
        backup_run_id=backup_run_id,
        backup_run_attempt=backup_run_attempt,
    )


def _expected_title(expected: BackupBinding) -> str:
    return (
        f"Platform backup/probe create s{expected.source_security_run_id}."
        f"{expected.source_security_run_attempt} {expected.source_sha}"
    )


def _validate_security_run(run: Any, expected: BackupBinding) -> tuple[datetime, datetime]:
    if not isinstance(run, dict):
        raise BackupProofError("exact security run response is malformed")
    if (
        type(run.get("id")) is not int
        or run["id"] != int(expected.source_security_run_id)
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] != int(expected.source_security_run_attempt)
        or run.get("path") != SECURITY_WORKFLOW_PATH
        or run.get("event") != "push"
        or run.get("head_branch") != "dev"
        or run.get("head_sha") != expected.source_sha
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
    ):
        raise BackupProofError("exact security run is not successful or source-bound")
    started = _parse_time(run.get("run_started_at"), "security run start")
    updated = _parse_time(run.get("updated_at"), "security run update")
    if started >= updated:
        raise BackupProofError("security run update precedes its start")
    return started, updated


def _validate_attempt_jobs(
    rows: Any,
    *,
    run_id: str,
    run_attempt: str,
    source_sha: str,
    source_event: str,
    run_started: datetime,
    run_updated: datetime,
    allow_nonterminal: bool = False,
) -> datetime | None:
    """Validate all jobs returned by an exact run-attempt endpoint."""

    if not isinstance(rows, list) or not 1 <= len(rows) <= 1_000:
        raise BackupProofError("attempt job list is empty, malformed or excessive")
    ids: set[int] = set()
    latest_completion: datetime | None = None
    has_nonterminal = False
    for row in rows:
        if not isinstance(row, dict):
            raise BackupProofError("attempt job row is malformed")
        job_id = row.get("id")
        if type(job_id) is not int or job_id <= 0 or job_id in ids:
            raise BackupProofError("attempt job IDs are missing, malformed or duplicate")
        ids.add(job_id)
        if type(row.get("run_id")) is not int or row["run_id"] != int(run_id):
            raise BackupProofError("attempt job belongs to a different workflow run")
        # The attempt-specific endpoint is the attempt authority. Job objects
        # may include extra attempt/source fields; if present, bind them too.
        if "run_attempt" in row and (
            type(row["run_attempt"]) is not int or row["run_attempt"] != int(run_attempt)
        ):
            raise BackupProofError("attempt job belongs to a different run attempt")
        if "head_sha" in row and (
            not isinstance(row["head_sha"], str) or row["head_sha"] != source_sha
        ):
            raise BackupProofError("attempt job belongs to a different source SHA")
        status = row.get("status")
        conclusion = row.get("conclusion")
        if not isinstance(status, str):
            raise BackupProofError("attempt job did not reach an allowed terminal result")
        if status in ATTEMPT_JOB_PENDING_STATUSES:
            if not allow_nonterminal or "conclusion" not in row or conclusion is not None:
                raise BackupProofError("attempt job did not reach an allowed terminal result")
            has_nonterminal = True
            continue
        if status != "completed" or not isinstance(conclusion, str) or conclusion not in {"success", "skipped"}:
            raise BackupProofError("attempt job did not reach an allowed terminal result")
        started = _parse_time(row.get("started_at"), "attempt job start")
        completed = _parse_time(row.get("completed_at"), "attempt job completion")
        # On a validated push, these two conditionally dispatched baseline
        # jobs are skipped by their workflow-dispatch/baseline-runtime guards.
        # GitHub reports no executed steps as null or []; the authentication
        # job also has all four runner fields explicitly null in the captured
        # API shape. Only those exact source/job/result shapes may have
        # inverted times. Timestamps remain run-bounded and in the all-job max.
        unexecuted_baseline_dispatch_finalizer = (
            source_event == "push"
            and status == "completed"
            and conclusion == "skipped"
            and row.get("name") == SECURITY_DISPATCH_FINALIZER_JOB_NAME
            and "steps" in row
            and (row["steps"] is None or row["steps"] == [])
        )
        unexecuted_baseline_runtime_guard = (
            source_event == "push"
            and status == "completed"
            and conclusion == "skipped"
            and row.get("name") == SECURITY_BASELINE_RUNTIME_JOB_NAME
            and "steps" in row
            and (row["steps"] is None or row["steps"] == [])
            and all(
                field in row and row[field] is None
                for field in ("runner_id", "runner_name", "runner_group_id", "runner_group_name")
            )
        )
        unexecuted_conditional_runtime_job = (
            source_event == "push"
            and status == "completed"
            and conclusion == "skipped"
            and row.get("name") in SECURITY_CONDITIONAL_RUNTIME_JOB_NAMES
            and "steps" in row
            and row["steps"] == []
            and row.get("labels") == ["ubuntu-latest"]
            and all(
                field in row and row[field] is None
                for field in ("runner_id", "runner_name", "runner_group_id", "runner_group_name")
            )
            and started == completed + timedelta(seconds=1)
        )
        if (
            not run_started <= started <= run_updated
            or not run_started <= completed <= run_updated
        ):
            raise BackupProofError("attempt job timestamps are inconsistent with its exact run")
        if started > completed and not (
            unexecuted_baseline_dispatch_finalizer
            or unexecuted_baseline_runtime_guard
            or unexecuted_conditional_runtime_job
        ):
            raise BackupProofError("attempt job timestamps are inconsistent with its exact run")
        if latest_completion is None or completed > latest_completion:
            latest_completion = completed
    if has_nonterminal:
        return None
    if latest_completion is None:
        raise BackupProofError("attempt job completion bound is unavailable")
    return latest_completion


def _validate_candidate_identity(run: Any, workflow_id: int, expected: BackupBinding) -> tuple[str, str, datetime, datetime]:
    if not isinstance(run, dict):
        raise BackupProofError("backup run response is malformed")
    if (
        type(run.get("id")) is not int
        or run["id"] <= 0
        or type(run.get("workflow_id")) is not int
        or run["workflow_id"] != workflow_id
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] <= 0
        or run.get("path") != BACKUP_WORKFLOW_PATH
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != "dev"
        or run.get("head_sha") != expected.source_sha
        or run.get("display_title") != _expected_title(expected)
    ):
        raise BackupProofError("backup run identity does not match exact source")
    run_id = str(run["id"])
    attempt = str(run["run_attempt"])
    created = _parse_time(run.get("created_at"), "backup run creation")
    started = _parse_time(run.get("run_started_at"), "backup workflow start")
    return run_id, attempt, created, started


def _validate_artifact_metadata(
    artifact: Any,
    *,
    run_id: str,
    attempt: str,
    source_sha: str,
    job_started: datetime,
    job_completed: datetime,
) -> tuple[int, str, int]:
    if not isinstance(artifact, dict):
        raise BackupProofError("backup artifact metadata is malformed")
    artifact_name = f"{BACKUP_ARTIFACT_PREFIX}{run_id}-{attempt}"
    workflow_run = artifact.get("workflow_run")
    if (
        type(artifact.get("id")) is not int
        or artifact["id"] <= 0
        or artifact.get("name") != artifact_name
        or type(artifact.get("expired")) is not bool
        or artifact["expired"] is not False
        or type(artifact.get("size_in_bytes")) is not int
        or not 1 <= artifact["size_in_bytes"] <= MAX_ARTIFACT_ZIP_BYTES
        or not isinstance(workflow_run, dict)
        or type(workflow_run.get("id")) is not int
        or workflow_run["id"] != int(run_id)
        or workflow_run.get("head_sha") != source_sha
        or not isinstance(artifact.get("created_at"), str)
        or not isinstance(artifact.get("digest"), str)
        or _API_DIGEST_RE.fullmatch(artifact["digest"]) is None
    ):
        raise BackupProofError("backup artifact metadata is not exactly bound")
    artifact_created = _parse_time(artifact["created_at"], "backup artifact creation")
    if not job_started <= artifact_created <= job_completed:
        raise BackupProofError("backup artifact was not created during the exact successful job attempt")
    return artifact["id"], artifact["digest"], artifact["size_in_bytes"]


def wait_for_backup_success(
    api: BackupProofApi,
    expected: BackupBinding,
    *,
    deadline_seconds: float = POLL_DEADLINE_SECONDS,
    clock=time.monotonic,
    sleep=time.sleep,
) -> dict[str, Any]:
    """Poll exact GitHub records until one source-bound successful backup exists."""

    expected.validate()
    if not isinstance(deadline_seconds, (int, float)) or isinstance(deadline_seconds, bool) or deadline_seconds <= 0:
        raise BackupProofError("backup proof deadline is invalid")
    deadline = clock() + float(deadline_seconds)
    if hasattr(api, "set_deadline"):
        api.set_deadline(deadline, clock)
    _check_deadline(deadline, clock)
    security_run = api.security_run(expected.source_security_run_id)
    security_started, security_updated = _validate_security_run(security_run, expected)
    security_jobs = api.security_jobs(
        expected.source_security_run_id, expected.source_security_run_attempt
    )
    _check_deadline(deadline, clock)
    last_job_completed_at = _validate_attempt_jobs(
        security_jobs,
        run_id=expected.source_security_run_id,
        run_attempt=expected.source_security_run_attempt,
        source_sha=expected.source_sha,
        source_event=security_run["event"],
        run_started=security_started,
        run_updated=security_updated,
        allow_nonterminal=True,
    )
    security_job_ids = frozenset(row["id"] for row in security_jobs)
    attempt_jobs_deadline = min(deadline, clock() + ATTEMPT_JOB_SETTLE_DEADLINE_SECONDS)
    if last_job_completed_at is None and hasattr(api, "set_deadline"):
        api.set_deadline(attempt_jobs_deadline, clock)
    pending_polls = 0
    while last_job_completed_at is None:
        if clock() >= attempt_jobs_deadline:
            raise BackupProofError("exact Security attempt jobs remained nonterminal until the bounded settle deadline")
        sleep(min(POLL_INTERVAL_SECONDS, max(0.0, attempt_jobs_deadline - clock())))
        if clock() >= attempt_jobs_deadline:
            raise BackupProofError("exact Security attempt jobs remained nonterminal until the bounded settle deadline")
        refreshed_run = api.security_run(expected.source_security_run_id)
        if clock() >= attempt_jobs_deadline:
            raise BackupProofError("exact Security attempt jobs remained nonterminal until the bounded settle deadline")
        refreshed_started, refreshed_updated = _validate_security_run(refreshed_run, expected)
        if refreshed_started != security_started or refreshed_updated < security_updated:
            raise BackupProofError("exact security run identity changed during attempt-job polling")
        security_updated = refreshed_updated
        security_jobs = api.security_jobs(
            expected.source_security_run_id, expected.source_security_run_attempt
        )
        if clock() >= attempt_jobs_deadline:
            raise BackupProofError("exact Security attempt jobs remained nonterminal until the bounded settle deadline")
        last_job_completed_at = _validate_attempt_jobs(
            security_jobs,
            run_id=expected.source_security_run_id,
            run_attempt=expected.source_security_run_attempt,
            source_sha=expected.source_sha,
            source_event=refreshed_run["event"],
            run_started=security_started,
            run_updated=security_updated,
            allow_nonterminal=True,
        )
        refreshed_job_ids = frozenset(row["id"] for row in security_jobs)
        if refreshed_job_ids != security_job_ids:
            raise BackupProofError("exact security attempt job set changed during polling")
        pending_polls += 1
        if last_job_completed_at is None and (pending_polls == 1 or pending_polls % 20 == 0):
            print("Exact Security attempt jobs remain nonterminal; waiting within the bounded settle window.")
    if hasattr(api, "set_deadline"):
        api.set_deadline(deadline, clock)
    _check_deadline(deadline, clock)
    workflow = api.backup_workflow(BACKUP_WORKFLOW_PATH)
    _check_deadline(deadline, clock)
    if (
        not isinstance(workflow, dict)
        or workflow.get("path") != BACKUP_WORKFLOW_PATH
        or type(workflow.get("id")) is not int
        or workflow["id"] <= 0
    ):
        raise BackupProofError("backup workflow identity is unavailable")
    workflow_id = workflow["id"]

    while True:
        _check_deadline(deadline, clock)
        if api.current_dev_sha() != expected.source_sha:
            raise BackupProofError("dev moved away from the exact source SHA")
        _check_deadline(deadline, clock)
        runs = api.backup_runs(
            workflow_id,
            "dev",
            "workflow_dispatch",
            expected.source_sha,
        )
        _check_deadline(deadline, clock)
        if not isinstance(runs, list) or len(runs) > 10_000:
            raise BackupProofError("backup workflow run list is malformed or excessive")
        candidates = [
            row
            for row in runs
            if isinstance(row, dict) and row.get("display_title") == _expected_title(expected)
        ]
        if len(candidates) > 1:
            raise BackupProofError("multiple exact source-bound backup runs are ambiguous")
        if not candidates:
            sleep(min(POLL_INTERVAL_SECONDS, max(0.0, deadline - clock())))
            _check_deadline(deadline, clock)
            continue
        candidate = candidates[0]
        run_id, attempt, created_at, workflow_started = _validate_candidate_identity(
            candidate, workflow_id, expected
        )
        if not (
            security_started
            <= created_at
            <= workflow_started
            < last_job_completed_at
        ):
            raise BackupProofError("backup did not reserve its production slot during the source security run")
        status = candidate.get("status")
        if isinstance(status, str) and status in {
            "queued",
            "in_progress",
            "waiting",
            "pending",
            "requested",
        }:
            sleep(min(POLL_INTERVAL_SECONDS, max(0.0, deadline - clock())))
            _check_deadline(deadline, clock)
            continue
        if status != "completed" or candidate.get("conclusion") != "success":
            raise BackupProofError("exact source-bound backup workflow did not complete successfully")

        jobs = api.backup_jobs(run_id, attempt)
        _check_deadline(deadline, clock)
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= 1_000:
            raise BackupProofError("backup job list is empty, malformed or excessive")
        matching_jobs = [
            job
            for job in jobs
            if isinstance(job, dict) and job.get("name") == BACKUP_JOB_NAME
        ]
        if len(matching_jobs) != 1:
            raise BackupProofError("exact production backup job is missing or ambiguous")
        job = matching_jobs[0]
        if (
            type(job.get("id")) is not int
            or job["id"] <= 0
            or type(job.get("run_id")) is not int
            or job["run_id"] != int(run_id)
            or (
                "run_attempt" in job
                and (type(job["run_attempt"]) is not int or job["run_attempt"] != int(attempt))
            )
            or (
                "head_sha" in job
                and (not isinstance(job["head_sha"], str) or job["head_sha"] != expected.source_sha)
            )
            or job.get("status") != "completed"
            or job.get("conclusion") != "success"
        ):
            raise BackupProofError("exact production backup job did not succeed")
        job_started = _parse_time(job.get("started_at"), "backup production job start")
        job_completed = _parse_time(job.get("completed_at"), "backup production job completion")
        backup_updated = _parse_time(candidate.get("updated_at"), "backup run update")
        if not workflow_started <= job_started <= job_completed <= backup_updated:
            raise BackupProofError("backup job timestamps are inconsistent with its exact run")
        if not (
            security_started
            <= created_at
            <= workflow_started
            <= job_started
            < last_job_completed_at
        ):
            raise BackupProofError("production backup job did not start while security was in progress")

        artifacts = api.backup_artifacts(run_id)
        _check_deadline(deadline, clock)
        if not isinstance(artifacts, list) or len(artifacts) > 10_000:
            raise BackupProofError("backup artifact list is malformed or excessive")
        expected_artifact_name = f"{BACKUP_ARTIFACT_PREFIX}{run_id}-{attempt}"
        matching_artifacts = [
            item
            for item in artifacts
            if isinstance(item, dict) and item.get("name") == expected_artifact_name
        ]
        if not matching_artifacts:
            sleep(min(POLL_INTERVAL_SECONDS, max(0.0, deadline - clock())))
            _check_deadline(deadline, clock)
            continue
        if len(matching_artifacts) != 1:
            raise BackupProofError("source-bound backup artifact is ambiguous")
        artifact_id, digest, expected_size = _validate_artifact_metadata(
            matching_artifacts[0],
            run_id=run_id,
            attempt=attempt,
            source_sha=expected.source_sha,
            job_started=job_started,
            job_completed=job_completed,
        )
        zip_bytes = api.download_artifact(run_id, artifact_id, MAX_ARTIFACT_ZIP_BYTES)
        _check_deadline(deadline, clock)
        if not isinstance(zip_bytes, bytes) or len(zip_bytes) != expected_size:
            raise BackupProofError("backup artifact bytes differ from API size metadata")
        receipt = validate_receipt_archive(
            zip_bytes,
            digest,
            expected,
            backup_run_id=run_id,
            backup_run_attempt=attempt,
        )
        _check_deadline(deadline, clock)
        if api.current_dev_sha() != expected.source_sha:
            raise BackupProofError("dev moved away from the exact source SHA")
        _check_deadline(deadline, clock)
        return receipt


class GitHubBackupProofApi:
    """Bounded GitHub REST adapter used by the workflow's fail-closed guard."""

    def __init__(self, repository: str, api_url: str, token: str) -> None:
        if repository != "StrayForest/old_sparky" or api_url != "https://api.github.com" or not token:
            raise BackupProofError("GitHub proof API configuration is invalid")
        self.repository = repository
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.base = f"{self.api_url}/repos/{self.repository}"
        self._deadline: float | None = None
        self._clock = time.monotonic

    def set_deadline(self, deadline: float, clock) -> None:
        self._deadline = deadline
        self._clock = clock

    def _remaining_timeout(self, maximum: float) -> float:
        if self._deadline is None:
            return maximum
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise BackupProofError("source-bound backup proof deadline expired")
        return min(maximum, remaining)

    def _read_bounded(self, response, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            if self._deadline is not None:
                _check_deadline(self._deadline, self._clock)
            chunk = response.read1(min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > max_bytes:
            raise BackupProofError("GitHub backup proof response is oversized")
        return b"".join(chunks)

    def _request(self, path: str, *, max_bytes: int = 1_048_576) -> bytes:
        if not path.startswith("/") or ".." in path.split("/"):
            raise BackupProofError("GitHub API path is invalid")
        request = Request(
            f"{self.base}{path}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "OldSparky-source-bound-backup-guard",
            },
        )
        try:
            opener = build_opener(_RejectRedirect())
            with opener.open(request, timeout=self._remaining_timeout(15)) as response:
                raw = self._read_bounded(response, max_bytes)
        except BackupProofError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise BackupProofError("GitHub backup proof API request failed") from exc
        return raw

    def _json(self, path: str, *, max_bytes: int = 1_048_576) -> Any:
        try:
            return json.loads(self._request(path, max_bytes=max_bytes))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupProofError("GitHub backup proof API returned invalid JSON") from exc

    def security_run(self, run_id: str) -> dict[str, Any]:
        if _ID_RE.fullmatch(run_id) is None:
            raise BackupProofError("security run ID is invalid")
        return self._json(f"/actions/runs/{run_id}")

    def security_jobs(self, run_id: str, run_attempt: str) -> list[dict[str, Any]]:
        return self._attempt_jobs(run_id, run_attempt)

    def backup_workflow(self, path: str) -> dict[str, Any]:
        if path != BACKUP_WORKFLOW_PATH:
            raise BackupProofError("backup workflow path is invalid")
        return self._json("/actions/workflows/platform-production-backup.yml")

    def backup_runs(
        self, workflow_id: int, branch: str, event: str, head_sha: str
    ) -> list[dict[str, Any]]:
        if branch != "dev" or event != "workflow_dispatch" or _SHA_RE.fullmatch(head_sha) is None:
            raise BackupProofError("backup run query is invalid")
        rows: list[dict[str, Any]] = []
        total: int | None = None
        for page in range(1, 101):
            query = urlencode(
                {"branch": branch, "event": event, "head_sha": head_sha, "per_page": 100, "page": page}
            )
            payload = self._json(
                f"/actions/workflows/{workflow_id}/runs?{query}", max_bytes=4_194_304
            )
            if (
                not isinstance(payload, dict)
                or type(payload.get("total_count")) is not int
                or not 0 <= payload["total_count"] <= 10_000
                or not isinstance(payload.get("workflow_runs"), list)
                or len(payload["workflow_runs"]) > 100
            ):
                raise BackupProofError("backup workflow pagination response is malformed")
            if total is None:
                total = payload["total_count"]
            elif total != payload["total_count"]:
                raise BackupProofError("backup workflow pagination changed during proof")
            remaining = total - len(rows)
            if remaining < 0:
                raise BackupProofError("backup workflow pagination exceeds its reported total")
            page_rows = payload["workflow_runs"]
            if len(page_rows) > remaining:
                raise BackupProofError("backup workflow pagination exceeds its reported total")
            if remaining > 0 and not page_rows:
                raise BackupProofError("backup workflow pagination is incomplete")
            rows.extend(page_rows)
            if len(rows) == total:
                break
        else:
            raise BackupProofError("backup workflow pagination exceeded its bound")
        if total is None or len(rows) != total:
            raise BackupProofError("backup workflow pagination is incomplete")
        ids = [row.get("id") for row in rows if isinstance(row, dict)]
        if len(ids) != len(rows) or any(type(item) is not int or item <= 0 for item in ids) or len(ids) != len(set(ids)):
            raise BackupProofError("backup workflow pagination has duplicate or malformed rows")
        return rows

    def _attempt_jobs(self, run_id: str, run_attempt: str) -> list[dict[str, Any]]:
        if _ID_RE.fullmatch(run_id) is None or _ATTEMPT_RE.fullmatch(run_attempt) is None:
            raise BackupProofError("workflow job query identity is invalid")
        rows: list[dict[str, Any]] = []
        total: int | None = None
        for page in range(1, 11):
            query = urlencode({"per_page": 100, "page": page})
            payload = self._json(
                f"/actions/runs/{run_id}/attempts/{run_attempt}/jobs?{query}",
                max_bytes=2_097_152,
            )
            if (
                not isinstance(payload, dict)
                or type(payload.get("total_count")) is not int
                or not 1 <= payload["total_count"] <= 1_000
                or not isinstance(payload.get("jobs"), list)
                or len(payload["jobs"]) > 100
            ):
                raise BackupProofError("workflow job response is malformed")
            if total is None:
                total = payload["total_count"]
            elif total != payload["total_count"]:
                raise BackupProofError("workflow job pagination changed during proof")
            remaining = total - len(rows)
            expected_page_size = min(100, remaining)
            if expected_page_size <= 0 or len(payload["jobs"]) != expected_page_size:
                raise BackupProofError("workflow job pagination is incomplete")
            if any(not isinstance(job, dict) for job in payload["jobs"]):
                raise BackupProofError("workflow job response contains a malformed row")
            rows.extend(payload["jobs"])
            if len(rows) == total:
                break
        if total is None or len(rows) != total:
            raise BackupProofError("workflow job pagination exceeded its bound")
        ids = [row.get("id") for row in rows]
        run_ids = [row.get("run_id") for row in rows]
        if (
            any(type(job_id) is not int or job_id <= 0 for job_id in ids)
            or len(ids) != len(set(ids))
            or any(type(row_run_id) is not int or row_run_id != int(run_id) for row_run_id in run_ids)
        ):
            raise BackupProofError("backup job pagination has duplicate or mismatched rows")
        return rows

    def backup_jobs(self, run_id: str, run_attempt: str) -> list[dict[str, Any]]:
        return self._attempt_jobs(run_id, run_attempt)

    def backup_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        if _ID_RE.fullmatch(run_id) is None:
            raise BackupProofError("backup artifact query identity is invalid")
        payload = self._json(f"/actions/runs/{run_id}/artifacts?per_page=100&page=1")
        if (
            not isinstance(payload, dict)
            or type(payload.get("total_count")) is not int
            or not 0 <= payload["total_count"] <= 10_000
            or not isinstance(payload.get("artifacts"), list)
            or len(payload["artifacts"]) > 100
            or payload["total_count"] != len(payload["artifacts"])
        ):
            raise BackupProofError("backup artifact response is malformed or paginated")
        return payload["artifacts"]

    def download_artifact(self, run_id: str, artifact_id: int, max_bytes: int) -> bytes:
        if _ID_RE.fullmatch(run_id) is None or type(artifact_id) is not int or artifact_id <= 0:
            raise BackupProofError("backup artifact download identity is invalid")
        path = f"/actions/artifacts/{artifact_id}/zip"
        if max_bytes != MAX_ARTIFACT_ZIP_BYTES:
            raise BackupProofError("backup artifact download limit is invalid")
        request = Request(
            f"{self.base}{path}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "OldSparky-source-bound-backup-guard",
            },
        )

        class _CredentiallessArtifactRedirect(HTTPRedirectHandler):
            redirects = 0

            def redirect_request(self, req, fp, code, msg, headers, newurl):
                self.redirects += 1
                try:
                    parsed = urlsplit(newurl)
                    port = parsed.port
                    hostname = parsed.hostname.lower() if parsed.hostname is not None else None
                except ValueError:
                    raise BackupProofError("backup artifact redirect is unsafe") from None
                if (
                    self.redirects != 1
                    or parsed.scheme != "https"
                    or not hostname
                    or port not in {None, 443}
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.fragment
                    or len(newurl) > 4096
                    or hostname.endswith(".")
                    or not (
                        hostname.endswith(".blob.core.windows.net")
                        or hostname.endswith(".actions.githubusercontent.com")
                    )
                ):
                    raise BackupProofError("backup artifact redirect is unsafe")
                # The signed artifact URL is fetched without the GitHub token.
                return Request(newurl, headers={"Accept": "application/octet-stream"})

        try:
            opener = build_opener(_CredentiallessArtifactRedirect())
            with opener.open(request, timeout=self._remaining_timeout(30)) as response:
                raw = self._read_bounded(response, max_bytes)
        except BackupProofError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise BackupProofError("GitHub backup artifact download failed") from exc
        return raw

    def current_dev_sha(self) -> str:
        value = self._json("/git/ref/heads/dev")
        if not isinstance(value, dict) or not isinstance(value.get("object"), dict):
            raise BackupProofError("current dev ref response is malformed")
        sha = value["object"].get("sha")
        if not isinstance(sha, str) or _SHA_RE.fullmatch(sha) is None:
            raise BackupProofError("current dev ref SHA is malformed")
        return sha


class _RejectRedirect(HTTPRedirectHandler):
    """Refuse redirects on authenticated JSON requests."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BackupProofError("authenticated GitHub API redirects are not accepted")


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    api_url = os.environ.get("GITHUB_API_URL", "")
    token = os.environ.get("GH_TOKEN", "")
    run_id = os.environ.get("SOURCE_SECURITY_RUN_ID", "")
    run_attempt = os.environ.get("SOURCE_SECURITY_RUN_ATTEMPT", "")
    source_sha = os.environ.get("TARGET_SHA", "")
    binding = BackupBinding(
        repository=repository,
        ref="refs/heads/dev",
        source_security_run_id=run_id,
        source_security_run_attempt=run_attempt,
        source_sha=source_sha,
    )
    try:
        proof = wait_for_backup_success(GitHubBackupProofApi(repository, api_url, token), binding)
        output_path = os.environ.get("GITHUB_OUTPUT", "")
        if not output_path:
            raise BackupProofError("GitHub output path is missing")
        with open(output_path, "a", encoding="utf-8") as output:
            output.write("verified=true\n")
            output.write(f"backup_run_id={proof['backup_run_id']}\n")
            output.write(f"backup_run_attempt={proof['backup_run_attempt']}\n")
        print("Exact source-bound restore-verified backup proof passed.")
        return 0
    except BackupProofError as exc:
        print(f"Source-bound backup proof failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
