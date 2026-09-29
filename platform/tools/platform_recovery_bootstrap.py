#!/usr/bin/env python3
"""Build, validate, install and run the retained-release recovery bootstrap.

The recovery bootstrap is a deliberately small, release-independent control
plane.  It is not an application artifact: it contains no application code,
Python dependencies, environment files, credentials or Alembic runner.  A
trusted CI job builds a deterministic archive, while the secret-bearing
workflow only validates that archive and asks the host to install one
content-addressed generation.  The generation's fixed entrypoint is the only
recovery command executed after installation.

The old release recorded in ``.release-operation.json`` remains the source of
service units, Nginx configuration and runtime configuration.  This module
owns receipt validation, lock/transaction/systemd control and the corrected
live-QA reconciliation helper used during retained recovery.
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import signal
import stat
import subprocess
import sys
import zipfile

try:
    from .platform_release_systemd_state import INITIAL_SYSTEMD_UNITS
    from .platform_release_transaction import (
        INITIAL_RECOVERY_PHASES,
        INITIAL_SYSTEMD_PHASES,
        MIGRATION_OUTCOME_UNCERTAIN_PHASES,
    )
except ImportError:  # The immutable recovery generation runs this file directly.
    _systemd_state_globals = runpy.run_path(
        str(Path(__file__).resolve().with_name("platform_release_systemd_state.py"))
    )
    _transaction_globals = runpy.run_path(
        str(Path(__file__).resolve().with_name("platform_release_transaction.py"))
    )
    INITIAL_SYSTEMD_UNITS = _systemd_state_globals["INITIAL_SYSTEMD_UNITS"]
    INITIAL_RECOVERY_PHASES = _transaction_globals["INITIAL_RECOVERY_PHASES"]
    INITIAL_SYSTEMD_PHASES = _transaction_globals["INITIAL_SYSTEMD_PHASES"]
    MIGRATION_OUTCOME_UNCERTAIN_PHASES = _transaction_globals[
        "MIGRATION_OUTCOME_UNCERTAIN_PHASES"
    ]


SCHEMA = 1
CAPABILITY = "abort_retained_only"
RECOVER_PENDING_CAPABILITY = "recover_pending"
ENTRYPOINT = "platform_abort_retained_only.sh"
MEMBER_ROOT = "platform-recovery-bootstrap"
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
ATTEMPT_RE = RUN_ID_RE
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORKFLOW_RE = re.compile(r"^[A-Za-z0-9_. -]{1,128}$")
JOB_RE = re.compile(r"^[A-Za-z0-9_. -]{1,128}$")
MAX_FILE_BYTES = 768 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_MEMBER_BYTES = 8 * 1024 * 1024
# The producer archive is a one-member upload-artifact ZIP.  Keep the
# decompression policy closed: upload-artifact currently uses DEFLATED, while
# STORED remains accepted for deterministic fixtures and older producers.
# A member may not expand by more than this bounded factor, even when its
# uncompressed size is below MAX_ARCHIVE_BYTES.
MAX_MEMBER_COMPRESSION_RATIO = 100
MAX_FILES = 32
MAX_MANIFEST_BYTES = 256 * 1024
MAX_PROVENANCE_BYTES = 64 * 1024
MAX_PUBLISH_PAGE_ROWS = 100
RECOVERY_SUBPROCESS_TIMEOUT_SECONDS = 120.0
RECOVERY_CHILD_TERMINATION_GRACE_SECONDS = 5.0
EXECUTABLE_MODE = 0o555
DATA_MODE = 0o444

# This is intentionally closed.  Do not add application, dependency, env,
# secret, migration-runner or source-checkout files here.
RECOVERY_FILES = (
    ENTRYPOINT,
    "platform_recovery_bootstrap.py",
    "platform_release_lock.sh",
    "platform_release_transaction.py",
    "platform_release_restore_runtime.sh",
    "platform_release_systemd_state.py",
    "platform_live_qa_guard.py",
    "platform_live_qa_runtime_install.py",
    "platform_recover_pending.sh",
)


class RecoveryBootstrapError(ValueError):
    """The recovery bootstrap archive or host state is not safe."""


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryBootstrapError("recovery bootstrap JSON has duplicate keys")
        result[key] = value
    return result


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_source_file(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RecoveryBootstrapError("recovery source file is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_size > MAX_FILE_BYTES
    ):
        raise RecoveryBootstrapError("recovery source file metadata is unsafe")
    return metadata


def _safe_source_root(path: Path) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RecoveryBootstrapError("recovery source root is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_nlink < 2
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or resolved != path
    ):
        raise RecoveryBootstrapError("recovery source root metadata is unsafe")


def _read_source_file(path: Path) -> bytes:
    before = _safe_source_file(path)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RecoveryBootstrapError("recovery source file cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size != before.st_size
        ):
            raise RecoveryBootstrapError("recovery source file changed")
        data = bytearray()
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(descriptor, MAX_FILE_BYTES + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_nlink != 1
            or after.st_size != opened.st_size
            or len(data) != after.st_size
            or len(data) > MAX_FILE_BYTES
        ):
            raise RecoveryBootstrapError("recovery source file changed")
        return bytes(data)
    except OSError as exc:
        raise RecoveryBootstrapError("recovery source file cannot be read") from exc
    finally:
        os.close(descriptor)


def _metadata_matches(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare all file facts that can change while a descriptor is read."""

    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_mode == right.st_mode
        and left.st_uid == right.st_uid
        and left.st_gid == right.st_gid
        and left.st_nlink == right.st_nlink
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _read_stable_file(
    path: Path,
    *,
    maximum: int,
    label: str,
    require_root: bool = False,
    mode: int | None = None,
    allowed_modes: set[int] | None = None,
) -> bytes:
    """Read one regular file through one no-follow descriptor.

    Validation, hashing and the returned bytes must describe the same inode.
    In particular, no caller may validate a pathname and then use a second
    pathname lookup for the trusted contents.
    """

    try:
        before = path.lstat()
    except OSError as exc:
        raise RecoveryBootstrapError(f"{label} is unavailable") from exc
    before_mode = stat.S_IMODE(before.st_mode)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (require_root and (before.st_uid != 0 or before.st_gid != 0))
        or (mode is not None and before_mode != mode)
        or (allowed_modes is not None and before_mode not in allowed_modes)
        or before.st_size > maximum
    ):
        raise RecoveryBootstrapError(f"{label} metadata is unsafe")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RecoveryBootstrapError(f"{label} cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if not _metadata_matches(opened, before):
            raise RecoveryBootstrapError(f"{label} changed during validation")
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(descriptor, maximum + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if (
            not _metadata_matches(after, opened)
            or len(data) != after.st_size
            or len(data) > maximum
        ):
            raise RecoveryBootstrapError(f"{label} changed during read")
        return bytes(data)
    except OSError as exc:
        raise RecoveryBootstrapError(f"{label} cannot be read") from exc
    finally:
        os.close(descriptor)


def _validate_source_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or name not in RECOVERY_FILES
        or PurePosixPath(name).is_absolute()
        or "\\" in name
        or any(part in {"", ".", ".."} for part in PurePosixPath(name).parts)
    ):
        raise RecoveryBootstrapError("recovery closure contains an invalid path")


def _source_path(source_root: Path, name: str) -> Path:
    _validate_source_name(name)
    path = source_root / "platform" / "tools" / name
    tools_root = source_root / "platform" / "tools"
    if path.parent != tools_root:
        raise RecoveryBootstrapError("recovery source path escaped tools")
    return path


def _validate_source_tree(source_root: Path) -> None:
    _safe_source_root(source_root)
    _safe_source_root(source_root / "platform")
    _safe_source_root(source_root / "platform" / "tools")
    for name in RECOVERY_FILES:
        _safe_source_file(_source_path(source_root, name))


def _provenance_schema(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise RecoveryBootstrapError("recovery provenance is not an object")
    expected = {
        "repository",
        "workflow",
        "job",
        "run_id",
        "run_attempt",
        "recovery_workflow_sha",
        "source_sha",
        "artifact_name",
        "artifact_sha256",
        "deployable",
    }
    if set(payload) != expected:
        raise RecoveryBootstrapError("recovery provenance schema is not closed")
    repository = payload.get("repository")
    workflow = payload.get("workflow")
    job = payload.get("job")
    run_id = payload.get("run_id")
    run_attempt = payload.get("run_attempt")
    recovery_workflow_sha = payload.get("recovery_workflow_sha")
    source_sha = payload.get("source_sha")
    artifact_name = payload.get("artifact_name")
    artifact_sha256 = payload.get("artifact_sha256")
    if not isinstance(repository, str) or REPOSITORY_RE.fullmatch(repository) is None:
        raise RecoveryBootstrapError("recovery repository provenance is invalid")
    if not isinstance(workflow, str) or WORKFLOW_RE.fullmatch(workflow) is None:
        raise RecoveryBootstrapError("recovery workflow provenance is invalid")
    if not isinstance(job, str) or JOB_RE.fullmatch(job) is None:
        raise RecoveryBootstrapError("recovery job provenance is invalid")
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise RecoveryBootstrapError("recovery run provenance is invalid")
    if not isinstance(run_attempt, str) or ATTEMPT_RE.fullmatch(run_attempt) is None:
        raise RecoveryBootstrapError("recovery attempt provenance is invalid")
    if not isinstance(recovery_workflow_sha, str) or SOURCE_SHA_RE.fullmatch(recovery_workflow_sha) is None:
        raise RecoveryBootstrapError("recovery workflow provenance SHA is invalid")
    if not isinstance(source_sha, str) or SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise RecoveryBootstrapError("recovery source provenance is invalid")
    if (
        not isinstance(artifact_name, str)
        or not (
            artifact_name.startswith("platform-ci-route-")
            or artifact_name.startswith("platform-recovery-bootstrap-")
        )
    ):
        raise RecoveryBootstrapError("recovery artifact provenance is invalid")
    if not isinstance(artifact_sha256, str) or HEX64_RE.fullmatch(artifact_sha256) is None:
        raise RecoveryBootstrapError("recovery artifact digest provenance is invalid")
    if payload.get("deployable") is not False:
        raise RecoveryBootstrapError("recovery bootstrap must be non-deployable")
    return payload


def _read_bounded_json(
    path: Path, *, maximum: int, label: str, require_root: bool = False
) -> object:
    try:
        raw = _read_stable_file(
            path,
            maximum=maximum,
            label=label,
            require_root=require_root,
            allowed_modes={0o400, 0o444, 0o600},
        )
    except RecoveryBootstrapError:
        raise
    try:
        return json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, RecoveryBootstrapError) as exc:
        raise RecoveryBootstrapError(f"{label} is invalid") from exc


def _read_publish_json(path: Path, *, label: str) -> object:
    """Read one bounded GitHub API response without a pathname TOCTOU."""

    try:
        raw = _read_stable_file(
            path,
            maximum=MAX_ARCHIVE_BYTES,
            label=label,
            allowed_modes={0o400, 0o440, 0o444, 0o600, 0o640, 0o644},
        )
        return json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, RecoveryBootstrapError) as exc:
        if isinstance(exc, RecoveryBootstrapError):
            raise
        raise RecoveryBootstrapError(f"{label} is invalid") from exc


def _publish_id(value: object, *, label: str) -> int:
    if type(value) is int:
        candidate = str(value)
    elif isinstance(value, str):
        candidate = value
    else:
        candidate = ""
    if RUN_ID_RE.fullmatch(candidate) is None:
        raise RecoveryBootstrapError(f"{label} is invalid")
    return int(candidate)


def _publish_string(value: object, *, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise RecoveryBootstrapError(f"{label} is invalid")
    return value


def _publish_rows(payload: object, *, key: str, label: str) -> list[object]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise RecoveryBootstrapError(f"{label} response is malformed")
    total_count = payload.get("total_count")
    rows = payload[key]
    if (
        type(total_count) is not int
        or total_count < 0
        or total_count != len(rows)
        or total_count > MAX_PUBLISH_PAGE_ROWS
    ):
        raise RecoveryBootstrapError(f"{label} response is not a complete page")
    return rows


def _publish_job_match(
    rows: list[object],
    *,
    name: str,
    run_id: int,
    run_attempt: int,
    expected_head_sha: str,
    label: str,
) -> dict[str, object]:
    matches = [
        row
        for row in rows
        if (
            isinstance(row, dict)
            and type(row.get("id")) is int
            and row.get("id") > 0
            and row.get("name") == name
            and row.get("run_id") == run_id
            and row.get("run_attempt") == run_attempt
            and row.get("status") == "completed"
            and row.get("conclusion") == "success"
            and row.get("head_sha") == expected_head_sha
        )
    ]
    if len(matches) != 1:
        raise RecoveryBootstrapError(f"exact successful {label} job is missing")
    return matches[0]


def _publish_route_artifact(
    rows: list[object],
    *,
    repository: str,
    run_id: int,
    run_attempt: int,
    source_sha: str,
) -> dict[str, object]:
    artifact_name = f"platform-ci-route-{run_id}-{run_attempt}"
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("name") == artifact_name
        and row.get("expired") is False
    ]
    if len(matches) != 1:
        raise RecoveryBootstrapError("exact security route artifact is missing")
    artifact = matches[0]
    if type(artifact.get("id")) is not int or artifact.get("id") <= 0:
        raise RecoveryBootstrapError("security route artifact id is invalid")
    workflow_run = artifact.get("workflow_run")
    if (
        not isinstance(workflow_run, dict)
        or workflow_run.get("id") != run_id
        or workflow_run.get("head_sha") != source_sha
        or (
            "run_attempt" in workflow_run
            and workflow_run.get("run_attempt") != run_attempt
        )
        or (
            "repository" in workflow_run
            and (workflow_run.get("repository") or {}).get("full_name") != repository
        )
    ):
        raise RecoveryBootstrapError("security artifact provenance is not exact")
    digest = artifact.get("digest")
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise RecoveryBootstrapError("security artifact digest is invalid")
    return artifact


def validate_publish_metadata(
    metadata: Path,
    *,
    repository: str,
    source_sha: str,
    security_run_id: str,
    security_run_attempt: str,
    security_workflow: str,
    security_workflow_path: str,
    security_job: str,
    recovery_run_id: str,
    recovery_run_attempt: str,
    recovery_workflow_sha: str,
    recovery_job: str,
    github_ref: str,
) -> dict[str, object]:
    """Validate the completed security run and completed bundle producer.

    This is intentionally independent of GitHub's event payload.  The
    workflow_run event is only a routing hint; every security/job/artifact
    identity used for publication is re-read from the exact attempt API
    responses and bound into the closed provenance object.
    """

    if github_ref != "refs/heads/dev":
        raise RecoveryBootstrapError("recovery publication ref is invalid")
    repository = _publish_string(repository, pattern=REPOSITORY_RE, label="repository")
    source_sha = _publish_string(source_sha, pattern=SOURCE_SHA_RE, label="source SHA")
    security_run = _publish_id(security_run_id, label="security run id")
    security_attempt = _publish_id(security_run_attempt, label="security run attempt")
    recovery_run = _publish_id(recovery_run_id, label="recovery run id")
    recovery_attempt = _publish_id(recovery_run_attempt, label="recovery run attempt")
    recovery_workflow_sha = _publish_string(
        recovery_workflow_sha, pattern=SOURCE_SHA_RE, label="recovery workflow SHA"
    )
    security_workflow = _publish_string(
        security_workflow, pattern=WORKFLOW_RE, label="security workflow"
    )
    security_workflow_path = _publish_string(
        security_workflow_path,
        pattern=re.compile(r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$"),
        label="security workflow path",
    )
    security_job = _publish_string(security_job, pattern=JOB_RE, label="security job")
    recovery_job = _publish_string(recovery_job, pattern=JOB_RE, label="recovery job")
    run = _read_publish_json(metadata / "run.json", label="security run")
    jobs = _read_publish_json(metadata / "jobs.json", label="security jobs")
    artifacts = _read_publish_json(metadata / "artifacts.json", label="security artifacts")
    recovery_jobs = _read_publish_json(
        metadata / "recovery-jobs.json", label="recovery producer jobs"
    )
    recovery_run_metadata = _read_publish_json(
        metadata / "recovery-run.json", label="recovery producer run"
    )
    if not isinstance(run, dict) or any(
        (
            run.get("id") != security_run,
            run.get("run_attempt") != security_attempt,
            run.get("head_sha") != source_sha,
            run.get("head_branch") != "dev",
            run.get("event") != "push",
            run.get("status") != "completed",
            run.get("conclusion") != "success",
            run.get("name") != security_workflow,
            run.get("path") != security_workflow_path,
            (run.get("repository") or {}).get("full_name") != repository,
        )
    ):
        raise RecoveryBootstrapError("security run provenance is not exact")
    security_job_row = _publish_job_match(
        _publish_rows(jobs, key="jobs", label="security jobs"),
        name=security_job,
        run_id=security_run,
        run_attempt=security_attempt,
        expected_head_sha=source_sha,
        label="security verification",
    )
    route_artifact = _publish_route_artifact(
        _publish_rows(artifacts, key="artifacts", label="security artifacts"),
        repository=repository,
        run_id=security_run,
        run_attempt=security_attempt,
        source_sha=source_sha,
    )
    if not isinstance(recovery_run_metadata, dict) or any(
        (
            recovery_run_metadata.get("id") != recovery_run,
            recovery_run_metadata.get("run_attempt") != recovery_attempt,
            recovery_run_metadata.get("head_sha") != recovery_workflow_sha,
            recovery_run_metadata.get("head_branch") != "dev",
            recovery_run_metadata.get("event") != "workflow_run",
            recovery_run_metadata.get("status") != "completed",
            recovery_run_metadata.get("conclusion") != "success",
            recovery_run_metadata.get("name") != "Platform production recovery bootstrap build",
            recovery_run_metadata.get("path") != ".github/workflows/platform-production-recovery-bootstrap-build.yml",
            (recovery_run_metadata.get("repository") or {}).get("full_name") != repository,
        )
    ):
        raise RecoveryBootstrapError("recovery producer run provenance is not exact")
    producer_job = _publish_job_match(
        _publish_rows(recovery_jobs, key="jobs", label="recovery producer jobs"),
        name=recovery_job,
        run_id=recovery_run,
        run_attempt=recovery_attempt,
        expected_head_sha=recovery_workflow_sha,
        label="recovery producer",
    )
    provenance = {
        "repository": repository,
        "workflow": security_workflow,
        "job": security_job,
        "run_id": str(security_run),
        "run_attempt": str(security_attempt),
        "recovery_workflow_sha": recovery_workflow_sha,
        "source_sha": source_sha,
        "artifact_name": f"platform-ci-route-{security_run}-{security_attempt}",
        "artifact_sha256": str(route_artifact["digest"])[len("sha256:") :],
        "deployable": False,
    }
    _provenance_schema(provenance)
    return {
        "provenance": provenance,
        "security_job_id": security_job_row["id"],
        "route_artifact_id": route_artifact["id"],
        "recovery_job_id": producer_job["id"],
    }


def validate_publish_artifact_metadata(
    metadata: Path,
    *,
    expected_name: str,
    expected_run_id: str,
    expected_run_attempt: str,
    expected_source_sha: str,
    expected_workflow_sha: str,
) -> int:
    """Select exactly one non-expired bundle artifact from one exact attempt."""

    expected_run = _publish_id(expected_run_id, label="recovery run id")
    expected_attempt = _publish_id(expected_run_attempt, label="recovery run attempt")
    expected_source_sha = _publish_string(
        expected_source_sha, pattern=SOURCE_SHA_RE, label="source SHA"
    )
    expected_workflow_sha = _publish_string(
        expected_workflow_sha, pattern=SOURCE_SHA_RE, label="recovery workflow SHA"
    )
    expected_name = _publish_string(
        expected_name,
        pattern=re.compile(
            rf"platform-recovery-bootstrap-{expected_source_sha}-[1-9][0-9]{{0,31}}-[1-9][0-9]{{0,31}}-{expected_run_id}-{expected_run_attempt}\.zip"
        ),
        label="recovery artifact name",
    )
    rows = _publish_rows(
        _read_publish_json(metadata, label="recovery artifacts"),
        key="artifacts",
        label="recovery artifacts",
    )
    matches = []
    for row in rows:
        if not isinstance(row, dict) or row.get("name") != expected_name:
            continue
        if row.get("expired") is not False or type(row.get("id")) is not int:
            continue
        workflow_run = row.get("workflow_run")
        if (
            not isinstance(workflow_run, dict)
            or workflow_run.get("id") != expected_run
            or workflow_run.get("head_sha") != expected_workflow_sha
            or (
                "run_attempt" in workflow_run
                and workflow_run.get("run_attempt") != expected_attempt
            )
        ):
            continue
        matches.append(row)
    if len(matches) != 1:
        raise RecoveryBootstrapError("exact recovery bundle artifact is missing")
    return _publish_id(matches[0]["id"], label="recovery bundle artifact id")


def _publisher_producer_run(
    run: object,
    *,
    repository: str,
    run_id: int,
    run_attempt: int,
    workflow: str,
    workflow_path: str,
) -> str:
    """Validate the completed producer identity used by the publisher.

    A ``workflow_run`` event is only a routing hint.  Re-reading this exact
    run/attempt is what prevents an in-progress run, a rerun, or a branch
    movement from being silently replaced by a latest-by-SHA lookup.
    """

    if not isinstance(run, dict) or any(
        (
            run.get("id") != run_id,
            run.get("run_attempt") != run_attempt,
            run.get("status") != "completed",
            run.get("conclusion") != "success",
            run.get("event") != "workflow_run",
            run.get("head_branch") != "dev",
            run.get("name") != workflow,
            run.get("path") != workflow_path,
            (run.get("repository") or {}).get("full_name") != repository,
        )
    ):
        raise RecoveryBootstrapError("recovery producer run provenance is not exact")
    return _publish_string(
        run.get("head_sha"), pattern=SOURCE_SHA_RE, label="recovery producer workflow SHA"
    )


def _publisher_bundle_candidate(
    rows: list[object],
    *,
    repository: str,
    run_id: int,
    run_attempt: int,
    producer_workflow_sha: str,
) -> tuple[dict[str, object], dict[str, str]]:
    pattern = re.compile(
        rf"^platform-recovery-bootstrap-([0-9a-f]{{40,64}})-([1-9][0-9]{{0,31}})-"
        rf"([1-9][0-9]{{0,31}})-{run_id}-{run_attempt}\.zip$"
    )
    matches: list[tuple[dict[str, object], re.Match[str]]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("expired") is not False:
            continue
        if type(row.get("id")) is not int or row["id"] <= 0:
            continue
        parsed = pattern.fullmatch(str(row.get("name", "")))
        workflow_run = row.get("workflow_run")
        digest = row.get("digest")
        if (
            parsed is None
            or not isinstance(workflow_run, dict)
            or workflow_run.get("id") != run_id
            or workflow_run.get("head_sha") != producer_workflow_sha
            or (
                "run_attempt" in workflow_run
                and workflow_run.get("run_attempt") != run_attempt
            )
            or (
                "repository" in workflow_run
                and (workflow_run.get("repository") or {}).get("full_name") != repository
            )
            or not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            continue
        matches.append((row, parsed))
    if len(matches) != 1:
        raise RecoveryBootstrapError("exact recovery producer bundle artifact is missing")
    row, parsed = matches[0]
    source_sha, security_run_id, security_attempt = parsed.groups()
    return row, {
        "bundle_name": str(row["name"]),
        "source_sha": source_sha,
        "security_run_id": security_run_id,
        "security_run_attempt": security_attempt,
        "artifact_sha256": str(row["digest"])[len("sha256:") :],
    }


def publisher_bundle_artifact_name(
    *,
    source_sha: str,
    security_run_id: str,
    security_run_attempt: str,
    producer_run_id: str,
    producer_run_attempt: str,
    publisher_run_id: str,
    publisher_run_attempt: str,
) -> str:
    """Return the content-address-independent outer publisher artifact name.

    The producer's inner filename remains bound to B.  The upload-artifact
    envelope is owned by publisher run C, so each publisher rerun gets a
    distinct artifact name even when it republishes the same B bytes.
    """

    source = _publish_string(source_sha, pattern=SOURCE_SHA_RE, label="source SHA")
    security_id = _publish_id(security_run_id, label="security run id")
    security_attempt = _publish_id(
        security_run_attempt, label="security run attempt"
    )
    producer_id = _publish_id(producer_run_id, label="producer run id")
    producer_attempt = _publish_id(
        producer_run_attempt, label="producer run attempt"
    )
    publisher_id = _publish_id(publisher_run_id, label="publisher run id")
    publisher_attempt = _publish_id(
        publisher_run_attempt, label="publisher run attempt"
    )
    return (
        f"platform-recovery-bootstrap-publisher-{source}-{security_id}-"
        f"{security_attempt}-{producer_id}-{producer_attempt}-"
        f"{publisher_id}-{publisher_attempt}.zip"
    )


def validate_publisher_artifact_metadata(
    metadata: Path,
    *,
    expected_name: str,
    expected_run_id: str,
    expected_run_attempt: str,
    expected_workflow_sha: str,
) -> dict[str, object]:
    """Select the exact C-side outer bundle artifact and API digest."""

    expected_run = _publish_id(expected_run_id, label="publisher run id")
    expected_attempt = _publish_id(
        expected_run_attempt, label="publisher run attempt"
    )
    expected_workflow_sha = _publish_string(
        expected_workflow_sha,
        pattern=SOURCE_SHA_RE,
        label="publisher workflow SHA",
    )
    expected_name = _publish_string(
        expected_name,
        pattern=re.compile(
            r"^platform-recovery-bootstrap-publisher-[0-9a-f]{40,64}-"
            r"[1-9][0-9]{0,31}-[1-9][0-9]{0,31}-"
            r"[1-9][0-9]{0,31}-[1-9][0-9]{0,31}-"
            r"[1-9][0-9]{0,31}-[1-9][0-9]{0,31}\.zip$"
        ),
        label="publisher bundle artifact name",
    )
    rows = _publish_rows(
        _read_publish_json(metadata, label="publisher artifacts"),
        key="artifacts",
        label="publisher artifacts",
    )
    matches: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        workflow_run = row.get("workflow_run")
        digest = row.get("digest")
        if (
            row.get("name") != expected_name
            or row.get("expired") is not False
            or type(row.get("id")) is not int
            or row["id"] <= 0
            or not isinstance(workflow_run, dict)
            or workflow_run.get("id") != expected_run
            or workflow_run.get("head_sha") != expected_workflow_sha
            or (
                "run_attempt" in workflow_run
                and workflow_run.get("run_attempt") != expected_attempt
            )
            or not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            continue
        matches.append(row)
    if len(matches) != 1:
        raise RecoveryBootstrapError("exact publisher bundle artifact is missing")
    row = matches[0]
    return {
        "publisher_bundle_name": expected_name,
        "publisher_bundle_artifact_id": _publish_id(
            row["id"], label="publisher bundle artifact id"
        ),
        "publisher_bundle_artifact_sha256": str(row["digest"])[len("sha256:") :],
    }


def select_publisher_bundle_metadata(
    metadata: Path,
    *,
    repository: str,
    producer_run_id: str,
    producer_run_attempt: str,
    producer_workflow: str,
    producer_workflow_path: str,
) -> dict[str, object]:
    """Select the one exact producer bundle before any security API lookup."""

    repository = _publish_string(repository, pattern=REPOSITORY_RE, label="repository")
    run_id = _publish_id(producer_run_id, label="producer run id")
    attempt = _publish_id(producer_run_attempt, label="producer run attempt")
    workflow = _publish_string(producer_workflow, pattern=WORKFLOW_RE, label="producer workflow")
    workflow_path = _publish_string(
        producer_workflow_path,
        pattern=re.compile(r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$"),
        label="producer workflow path",
    )
    run = _read_publish_json(metadata / "producer-run.json", label="producer run")
    producer_sha = _publisher_producer_run(
        run,
        repository=repository,
        run_id=run_id,
        run_attempt=attempt,
        workflow=workflow,
        workflow_path=workflow_path,
    )
    jobs = _publish_rows(
        _read_publish_json(metadata / "producer-jobs.json", label="producer jobs"),
        key="jobs",
        label="producer jobs",
    )
    producer_job_name = "Build retained-release recovery bootstrap evidence"
    producer_job = _publish_job_match(
        jobs,
        name=producer_job_name,
        run_id=run_id,
        run_attempt=attempt,
        expected_head_sha=producer_sha,
        label="recovery producer",
    )
    artifacts = _publish_rows(
        _read_publish_json(metadata / "producer-artifacts.json", label="producer artifacts"),
        key="artifacts",
        label="producer artifacts",
    )
    artifact, fields = _publisher_bundle_candidate(
        artifacts,
        repository=repository,
        run_id=run_id,
        run_attempt=attempt,
        producer_workflow_sha=producer_sha,
    )
    result = {
        "producer_workflow_sha": producer_sha,
        "producer_job_id": producer_job["id"],
        "bundle_artifact_id": artifact["id"],
        **fields,
    }
    result["bundle_artifact_sha256"] = result["artifact_sha256"]
    return result


def validate_publisher_metadata(
    metadata: Path,
    *,
    repository: str,
    producer_run_id: str,
    producer_run_attempt: str,
    producer_workflow: str,
    producer_workflow_path: str,
    security_workflow: str,
    security_workflow_path: str,
    security_job: str,
    publisher_workflow_sha: str,
    publisher_run_id: str,
    publisher_run_attempt: str,
    publisher_job_id: str,
    github_ref: str,
) -> dict[str, object]:
    """Validate a completed producer and its exact security provenance.

    The publisher owns no production secrets.  It proves the producer run is
    complete, selects its exact bundle, then independently revalidates the
    security run/attempt and route artifact encoded in the closed bundle name.
    ``publisher_workflow_sha`` is C and is kept separate from producer B and
    security/bundle source A.
    """

    if github_ref != "refs/heads/dev":
        raise RecoveryBootstrapError("recovery publisher ref is invalid")
    repository = _publish_string(repository, pattern=REPOSITORY_RE, label="repository")
    publisher_sha = _publish_string(
        publisher_workflow_sha, pattern=SOURCE_SHA_RE, label="publisher workflow SHA"
    )
    publisher_run = _publish_id(publisher_run_id, label="publisher run id")
    publisher_attempt = _publish_id(publisher_run_attempt, label="publisher run attempt")
    publisher_job = _publish_id(publisher_job_id, label="publisher job id")
    selected = select_publisher_bundle_metadata(
        metadata,
        repository=repository,
        producer_run_id=producer_run_id,
        producer_run_attempt=producer_run_attempt,
        producer_workflow=producer_workflow,
        producer_workflow_path=producer_workflow_path,
    )
    source_sha = str(selected["source_sha"])
    security_run_id = str(selected["security_run_id"])
    security_attempt = str(selected["security_run_attempt"])
    run = _read_publish_json(metadata / "security-run.json", label="security run")
    jobs = _read_publish_json(metadata / "security-jobs.json", label="security jobs")
    artifacts = _read_publish_json(metadata / "security-artifacts.json", label="security artifacts")
    security_run = _publish_id(security_run_id, label="security run id")
    security_attempt_int = _publish_id(security_attempt, label="security run attempt")
    if not isinstance(run, dict) or any(
        (
            run.get("id") != security_run,
            run.get("run_attempt") != security_attempt_int,
            run.get("head_sha") != source_sha,
            run.get("head_branch") != "dev",
            run.get("event") != "push",
            run.get("status") != "completed",
            run.get("conclusion") != "success",
            run.get("name") != security_workflow,
            run.get("path") != security_workflow_path,
            (run.get("repository") or {}).get("full_name") != repository,
        )
    ):
        raise RecoveryBootstrapError("security run provenance is not exact")
    security_job_row = _publish_job_match(
        _publish_rows(jobs, key="jobs", label="security jobs"),
        name=security_job,
        run_id=security_run,
        run_attempt=security_attempt_int,
        expected_head_sha=source_sha,
        label="security verification",
    )
    route = _publish_route_artifact(
        _publish_rows(artifacts, key="artifacts", label="security artifacts"),
        repository=repository,
        run_id=security_run,
        run_attempt=security_attempt_int,
        source_sha=source_sha,
    )
    provenance = {
        "repository": repository,
        "workflow": security_workflow,
        "job": security_job,
        "run_id": str(security_run),
        "run_attempt": str(security_attempt_int),
        "recovery_workflow_sha": str(selected["producer_workflow_sha"]),
        "source_sha": source_sha,
        "artifact_name": f"platform-ci-route-{security_run}-{security_attempt_int}",
        "artifact_sha256": str(route["digest"])[len("sha256:") :],
        "deployable": False,
    }
    _provenance_schema(provenance)
    return {
        "provenance": provenance,
        "source_sha": source_sha,
        "security_run_id": str(security_run),
        "security_run_attempt": str(security_attempt_int),
        "security_job_id": security_job_row["id"],
        "route_artifact_id": route["id"],
        "producer_run_id": str(_publish_id(producer_run_id, label="producer run id")),
        "producer_run_attempt": str(_publish_id(producer_run_attempt, label="producer run attempt")),
        "producer_workflow_sha": selected["producer_workflow_sha"],
        "producer_job_id": selected["producer_job_id"],
        "bundle_name": selected["bundle_name"],
        "bundle_artifact_id": selected["bundle_artifact_id"],
        "bundle_artifact_sha256": selected["artifact_sha256"],
        "publisher_workflow_sha": publisher_sha,
        "publisher_run_id": str(publisher_run),
        "publisher_run_attempt": str(publisher_attempt),
        "publisher_job_id": str(publisher_job),
    }


def extract_publish_bundle(
    archive: Path,
    output: Path,
    *,
    expected_name: str,
    expected_sha: str,
    expected_archive_sha: str | None = None,
) -> None:
    """Extract one exact bundle member and bind its bytes to the API digest."""

    expected_sha = _publish_string(expected_sha, pattern=HEX64_RE, label="bundle SHA")
    expected_name = _publish_string(
        expected_name,
        pattern=re.compile(r"^platform-recovery-bootstrap-[0-9a-f]{40,64}-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}\.zip$"),
        label="bundle artifact member",
    )
    raw = _read_stable_file(
        archive,
        maximum=MAX_ARCHIVE_BYTES,
        label="recovery bundle artifact archive",
        allowed_modes={0o400, 0o440, 0o444, 0o600, 0o640, 0o644},
    )
    if expected_archive_sha is not None:
        expected_archive_sha = _publish_string(
            expected_archive_sha,
            pattern=HEX64_RE,
            label="producer artifact SHA",
        )
        if _sha256(raw) != expected_archive_sha:
            raise RecoveryBootstrapError("producer artifact digest is invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(raw), mode="r", allowZip64=False) as package:
            infos = package.infolist()
            matches = [item for item in infos if item.filename == expected_name]
            if len(infos) != 1 or len(matches) != 1:
                raise RecoveryBootstrapError("recovery bundle artifact member is not exact")
            item = matches[0]
            mode = (item.external_attr >> 16) & 0o177777
            compressed_size = item.compress_size
            uncompressed_size = item.file_size
            if compressed_size < 0 or compressed_size > MAX_ARCHIVE_BYTES:
                raise RecoveryBootstrapError("recovery bundle artifact member is too large")
            if uncompressed_size < 0 or uncompressed_size > MAX_ARCHIVE_BYTES:
                raise RecoveryBootstrapError("recovery bundle artifact member is too large")
            if compressed_size == 0:
                ratio_ok = uncompressed_size == 0
            else:
                ratio_ok = (
                    uncompressed_size
                    <= compressed_size * MAX_MEMBER_COMPRESSION_RATIO
                )
            if (
                item.create_system != 3
                or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or item.is_dir()
                or not stat.S_ISREG(mode)
                or item.flag_bits & 0x1
                or not ratio_ok
            ):
                raise RecoveryBootstrapError("recovery bundle artifact member is unsafe")
            bundle = package.read(item)
            if len(bundle) != uncompressed_size:
                raise RecoveryBootstrapError("recovery bundle artifact member size is invalid")
            # zipfile validates CRC while reading; repeat the check against
            # the bytes obtained from that same in-memory archive so a
            # malformed header cannot be treated as a trusted bundle.
            if (binascii.crc32(bundle) & 0xFFFFFFFF) != item.CRC:
                raise RecoveryBootstrapError("recovery bundle artifact member CRC is invalid")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        if isinstance(exc, RecoveryBootstrapError):
            raise
        raise RecoveryBootstrapError("recovery bundle artifact archive is invalid") from exc
    if _sha256(bundle) != expected_sha:
        raise RecoveryBootstrapError("recovery bundle digest is invalid")
    if output.exists() or output.is_symlink():
        raise RecoveryBootstrapError("recovery bundle output already exists")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise RecoveryBootstrapError("recovery bundle output staging path exists")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(bundle)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise RecoveryBootstrapError("recovery bundle output could not be written") from exc


def build_publish_evidence(
    provenance: dict[str, object],
    *,
    bundle_name: str,
    bundle_sha: str,
    bundle_artifact_sha256: str | None = None,
    recovery_run_id: str,
    recovery_run_attempt: str,
    recovery_job_id: str,
    publisher_workflow_sha: str | None = None,
    publisher_run_id: str | None = None,
    publisher_run_attempt: str | None = None,
    publisher_job_id: str | None = None,
    publisher_bundle_name: str | None = None,
    publisher_bundle_artifact_id: str | None = None,
    publisher_bundle_artifact_sha256: str | None = None,
) -> dict[str, object]:
    """Build the closed, non-deployable recovery evidence payload."""

    provenance = _provenance_schema(provenance)
    if provenance["artifact_name"] != (
        f"platform-ci-route-{provenance['run_id']}-{provenance['run_attempt']}"
    ):
        raise RecoveryBootstrapError("recovery route artifact provenance is invalid")
    bundle_sha = _publish_string(bundle_sha, pattern=HEX64_RE, label="bundle SHA")
    recovery_run = _publish_id(recovery_run_id, label="recovery run id")
    recovery_attempt = _publish_id(recovery_run_attempt, label="recovery run attempt")
    recovery_job = _publish_id(recovery_job_id, label="recovery job id")
    bundle_name = _publish_string(
        bundle_name,
        pattern=re.compile(
            rf"platform-recovery-bootstrap-{provenance['source_sha']}-{provenance['run_id']}-{provenance['run_attempt']}-{recovery_run_id}-{recovery_run_attempt}\.zip"
        ),
        label="bundle name",
    )
    legacy_publisher = all(
        value is None
        for value in (
            publisher_workflow_sha,
            publisher_run_id,
            publisher_run_attempt,
            publisher_job_id,
            publisher_bundle_name,
            publisher_bundle_artifact_id,
            publisher_bundle_artifact_sha256,
        )
    )
    if legacy_publisher and bundle_artifact_sha256 is not None:
        raise RecoveryBootstrapError("legacy publisher evidence cannot carry artifact metadata")
    if not legacy_publisher and bundle_artifact_sha256 is None:
        raise RecoveryBootstrapError("publisher artifact digest is missing")
    if bundle_artifact_sha256 is not None:
        bundle_artifact_sha256 = _publish_string(
            bundle_artifact_sha256,
            pattern=HEX64_RE,
            label="producer artifact SHA",
        )
    if not legacy_publisher and any(
        value is None
        for value in (
            publisher_workflow_sha,
            publisher_run_id,
            publisher_run_attempt,
            publisher_job_id,
            publisher_bundle_name,
            publisher_bundle_artifact_id,
            publisher_bundle_artifact_sha256,
        )
    ):
        raise RecoveryBootstrapError("publisher evidence identity is incomplete")
    publisher_bundle_name_value: str | None = None
    publisher_bundle_artifact_id_value: str | None = None
    publisher_bundle_artifact_sha256_value: str | None = None
    if not legacy_publisher:
        publisher_run = _publish_id(str(publisher_run_id), label="publisher run id")
        publisher_attempt = _publish_id(
            str(publisher_run_attempt), label="publisher run attempt"
        )
        publisher_bundle_name_value = _publish_string(
            str(publisher_bundle_name),
            pattern=re.compile(
                rf"platform-recovery-bootstrap-publisher-{provenance['source_sha']}-"
                rf"{provenance['run_id']}-{provenance['run_attempt']}-"
                rf"{recovery_run}-{recovery_attempt}-"
                rf"{publisher_run}-{publisher_attempt}\.zip"
            ),
            label="publisher bundle name",
        )
        publisher_bundle_artifact_id_value = str(
            _publish_id(
                str(publisher_bundle_artifact_id),
                label="publisher bundle artifact id",
            )
        )
        publisher_bundle_artifact_sha256_value = _publish_string(
            str(publisher_bundle_artifact_sha256),
            pattern=HEX64_RE,
            label="publisher bundle artifact SHA",
        )
    payload: dict[str, object] = {
        "schema": 1 if legacy_publisher else 3,
        "capability": "recovery_bootstrap",
        "capabilities": [CAPABILITY, RECOVER_PENDING_CAPABILITY],
        "deployable": False,
        "bundle_name": bundle_name,
        "bundle_sha256": bundle_sha,
        "recovery_run_id": str(recovery_run),
        "recovery_run_attempt": str(recovery_attempt),
        "recovery_job_id": str(recovery_job),
        "provenance": provenance,
    }
    if not legacy_publisher:
        payload["bundle_artifact_sha256"] = bundle_artifact_sha256
    if not legacy_publisher:
        payload.update(
            {
                "publisher_workflow_sha": _publish_string(
                    str(publisher_workflow_sha),
                    pattern=SOURCE_SHA_RE,
                    label="publisher workflow SHA",
                ),
                "publisher_run_id": str(publisher_run),
                "publisher_run_attempt": str(publisher_attempt),
                "publisher_job_id": str(
                    _publish_id(str(publisher_job_id), label="publisher job id")
                ),
                "publisher_bundle_name": publisher_bundle_name_value,
                "publisher_bundle_artifact_id": publisher_bundle_artifact_id_value,
                "publisher_bundle_artifact_sha256": publisher_bundle_artifact_sha256_value,
            }
        )
    return payload


def _manifest(
    *, source_sha: str, provenance: dict[str, object], records: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "capability": CAPABILITY,
        "capabilities": [CAPABILITY, RECOVER_PENDING_CAPABILITY],
        "entrypoint": ENTRYPOINT,
        "source_sha": source_sha,
        "deployable": False,
        "provenance": provenance,
        "limits": {
            "max_archive_bytes": MAX_ARCHIVE_BYTES,
            "max_file_bytes": MAX_FILE_BYTES,
            "max_total_member_bytes": MAX_TOTAL_MEMBER_BYTES,
            "max_files": MAX_FILES,
        },
        "files": records,
    }


def _zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(f"{MEMBER_ROOT}/{name}")
    info.date_time = (1980, 1, 1, 0, 0, 0)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.flag_bits = 0
    return info


def build_bundle(
    source_root: Path,
    *,
    source_sha: str,
    provenance: dict[str, object],
    output: Path,
) -> dict[str, object]:
    """Build a deterministic immutable archive and verify it before returning."""

    if SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise RecoveryBootstrapError("recovery source SHA is invalid")
    provenance = _provenance_schema(provenance)
    if provenance["source_sha"] != source_sha:
        raise RecoveryBootstrapError("recovery source SHA does not match provenance")
    _validate_source_tree(source_root)
    if len(RECOVERY_FILES) > MAX_FILES:
        raise RecoveryBootstrapError("recovery closure exceeds its bound")
    contents: dict[str, bytes] = {}
    records: list[dict[str, object]] = []
    for name in RECOVERY_FILES:
        data = _read_source_file(_source_path(source_root, name))
        mode = EXECUTABLE_MODE if name.endswith(".sh") else DATA_MODE
        contents[name] = data
        records.append({"path": name, "sha256": _sha256(data), "mode": mode})
    records.sort(key=lambda value: str(value["path"]))
    manifest = _manifest(source_sha=source_sha, provenance=provenance, records=records)
    manifest_bytes = _canonical_json(manifest)
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise RecoveryBootstrapError("recovery manifest exceeds its bound")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w+b") as stream:
            with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
                for name in sorted(RECOVERY_FILES):
                    archive.writestr(_zip_info(name, EXECUTABLE_MODE if name.endswith(".sh") else DATA_MODE), contents[name])
                archive.writestr(_zip_info("manifest.json", DATA_MODE), manifest_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        metadata = temporary.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_ARCHIVE_BYTES
        ):
            raise RecoveryBootstrapError("recovery archive metadata is unsafe")
        os.replace(temporary, output)
        directory_fd = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        if isinstance(exc, RecoveryBootstrapError):
            raise
        raise RecoveryBootstrapError("recovery archive could not be written") from exc
    return verify_bundle(output, expected_source_sha=source_sha, expected_provenance=provenance)


def _safe_member(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if (
        not name.startswith(f"{MEMBER_ROOT}/")
        or name.count("/") != 1
        or "\\" in name
        or info.is_dir()
        or info.compress_type != zipfile.ZIP_STORED
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise RecoveryBootstrapError("recovery archive member path is unsafe")
    mode = (info.external_attr >> 16) & 0o177777
    if not stat.S_ISREG(mode):
        raise RecoveryBootstrapError("recovery archive member type is unsafe")
    return name.split("/", 1)[1]


def _validate_manifest(
    manifest: object,
    *,
    expected_source_sha: str | None,
    expected_provenance: dict[str, object] | None,
) -> dict[str, object]:
    if not isinstance(manifest, dict):
        raise RecoveryBootstrapError("recovery manifest is not an object")
    expected_keys = {"schema", "capability", "capabilities", "entrypoint", "source_sha", "deployable", "provenance", "limits", "files"}
    if set(manifest) != expected_keys:
        raise RecoveryBootstrapError("recovery manifest schema is not closed")
    if manifest.get("schema") != SCHEMA or manifest.get("capability") != CAPABILITY or manifest.get("entrypoint") != ENTRYPOINT:
        raise RecoveryBootstrapError("recovery manifest capability is invalid")
    if manifest.get("capabilities") != [CAPABILITY, RECOVER_PENDING_CAPABILITY]:
        raise RecoveryBootstrapError("recovery manifest capabilities are invalid")
    if manifest.get("deployable") is not False:
        raise RecoveryBootstrapError("recovery manifest is deployable")
    source_sha = manifest.get("source_sha")
    if not isinstance(source_sha, str) or SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise RecoveryBootstrapError("recovery manifest source SHA is invalid")
    if expected_source_sha is not None and source_sha != expected_source_sha:
        raise RecoveryBootstrapError("recovery manifest source SHA does not match")
    provenance = _provenance_schema(manifest.get("provenance"))
    if provenance["source_sha"] != source_sha:
        raise RecoveryBootstrapError("recovery provenance source SHA does not match")
    if expected_provenance is not None and provenance != expected_provenance:
        raise RecoveryBootstrapError("recovery provenance does not match")
    limits = manifest.get("limits")
    if limits != {
        "max_archive_bytes": MAX_ARCHIVE_BYTES,
        "max_file_bytes": MAX_FILE_BYTES,
        "max_total_member_bytes": MAX_TOTAL_MEMBER_BYTES,
        "max_files": MAX_FILES,
    }:
        raise RecoveryBootstrapError("recovery manifest limits are invalid")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != len(RECOVERY_FILES):
        raise RecoveryBootstrapError("recovery manifest inventory is invalid")
    expected = set(RECOVERY_FILES)
    actual: list[str] = []
    for record in files:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "mode"}:
            raise RecoveryBootstrapError("recovery manifest file record is invalid")
        path = record.get("path")
        digest = record.get("sha256")
        mode = record.get("mode")
        if (
            not isinstance(path, str)
            or path not in expected
            or path in actual
            or not isinstance(digest, str)
            or HEX64_RE.fullmatch(digest) is None
            or type(mode) is not int
            or mode != (EXECUTABLE_MODE if path.endswith(".sh") else DATA_MODE)
        ):
            raise RecoveryBootstrapError("recovery manifest file record is invalid")
        actual.append(path)
    if actual != sorted(expected):
        raise RecoveryBootstrapError("recovery manifest inventory ordering is invalid")
    return manifest


def verify_bundle(
    bundle: Path,
    *,
    expected_source_sha: str | None = None,
    expected_provenance: dict[str, object] | None = None,
) -> dict[str, object]:
    try:
        metadata = bundle.lstat()
    except OSError as exc:
        raise RecoveryBootstrapError("recovery archive is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_ARCHIVE_BYTES
    ):
        raise RecoveryBootstrapError("recovery archive metadata is unsafe")
    try:
        descriptor = os.open(
            bundle,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RecoveryBootstrapError("recovery archive cannot be opened") from exc
    stream = None
    try:
        opened = os.fstat(descriptor)
        if not _metadata_matches(opened, metadata):
            raise RecoveryBootstrapError("recovery archive changed during validation")
        stream = os.fdopen(descriptor, "rb", closefd=False)
        with zipfile.ZipFile(stream, mode="r", allowZip64=False) as archive:
            infos = archive.infolist()
            if len(infos) != len(RECOVERY_FILES) + 1:
                raise RecoveryBootstrapError("recovery archive member count is invalid")
            members: dict[str, bytes] = {}
            modes: dict[str, int] = {}
            total_member_bytes = 0
            for info in infos:
                name = _safe_member(info)
                if (
                    name in members
                    or info.file_size > MAX_FILE_BYTES
                    or total_member_bytes > MAX_TOTAL_MEMBER_BYTES - info.file_size
                ):
                    raise RecoveryBootstrapError("recovery archive member is invalid")
                data = archive.read(info)
                if len(data) != info.file_size or len(data) > MAX_FILE_BYTES:
                    raise RecoveryBootstrapError("recovery archive member size is invalid")
                members[name] = data
                modes[name] = (info.external_attr >> 16) & 0o7777
                total_member_bytes += len(data)
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while total <= MAX_ARCHIVE_BYTES:
            chunk = os.read(descriptor, MAX_ARCHIVE_BYTES + 1 - total)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        if (
            not _metadata_matches(after, opened)
            or total != after.st_size
            or total > MAX_ARCHIVE_BYTES
        ):
            raise RecoveryBootstrapError("recovery archive changed during read")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        if isinstance(exc, RecoveryBootstrapError):
            raise
        raise RecoveryBootstrapError("recovery archive is invalid") from exc
    finally:
        if stream is not None:
            stream.close()
        try:
            os.close(descriptor)
        except OSError:
            pass
    expected_members = set(RECOVERY_FILES) | {"manifest.json"}
    if set(members) != expected_members:
        raise RecoveryBootstrapError("recovery archive inventory is not closed")
    if modes.get("manifest.json") != DATA_MODE:
        raise RecoveryBootstrapError("recovery manifest mode is invalid")
    for name in RECOVERY_FILES:
        expected_mode = EXECUTABLE_MODE if name.endswith(".sh") else DATA_MODE
        if modes.get(name) != expected_mode:
            raise RecoveryBootstrapError("recovery member mode is invalid")
    try:
        manifest = json.loads(members["manifest.json"].decode("ascii"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, RecoveryBootstrapError) as exc:
        raise RecoveryBootstrapError("recovery manifest encoding is invalid") from exc
    manifest = _validate_manifest(
        manifest,
        expected_source_sha=expected_source_sha,
        expected_provenance=expected_provenance,
    )
    records = {str(record["path"]): record for record in manifest["files"]}
    for name, data in members.items():
        if name == "manifest.json":
            continue
        if _sha256(data) != records[name]["sha256"]:
            raise RecoveryBootstrapError("recovery member digest is invalid")
    return {
        "manifest": manifest,
        "bundle_sha256": digest.hexdigest(),
        "members": members,
    }


def _safe_host_directory(path: Path, *, mode: int | None = None) -> os.stat_result:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RecoveryBootstrapError("recovery host directory is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink < 2
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or resolved != path
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
    ):
        raise RecoveryBootstrapError("recovery host directory metadata is unsafe")
    return metadata


def _safe_generation_file(path: Path, *, mode: int, maximum: int = MAX_FILE_BYTES) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RecoveryBootstrapError("recovery generation file is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_size > maximum
    ):
        raise RecoveryBootstrapError("recovery generation file metadata is unsafe")
    return metadata


def _validate_generation_tree(
    generation: Path,
    *,
    bundle_sha: str,
    require_generation_name: bool = True,
    expected_members: dict[str, bytes] | None = None,
    expected_provenance: dict[str, object] | None = None,
    required_capability: str = CAPABILITY,
) -> None:
    if not HEX64_RE.fullmatch(bundle_sha) or (
        require_generation_name and generation.name != bundle_sha
    ):
        raise RecoveryBootstrapError("recovery generation identity is invalid")
    _safe_host_directory(generation, mode=0o555)
    members = list(generation.iterdir())
    if len(members) != len(RECOVERY_FILES) + 1:
        raise RecoveryBootstrapError("recovery generation inventory is invalid")
    for path in sorted(members, key=lambda item: item.name):
        if path.name not in set(RECOVERY_FILES) | {"manifest.json"}:
            raise RecoveryBootstrapError("recovery generation has an unexpected member")
        if path.name == "manifest.json":
            _safe_generation_file(path, mode=0o444, maximum=MAX_MANIFEST_BYTES)
        else:
            _safe_generation_file(path, mode=EXECUTABLE_MODE if path.name.endswith(".sh") else DATA_MODE)
    expected_manifest = generation / "manifest.json"
    manifest = _read_bounded_json(
        expected_manifest,
        maximum=MAX_MANIFEST_BYTES,
        label="recovery generation manifest",
        require_root=True,
    )
    manifest = _validate_manifest(
        manifest, expected_source_sha=None, expected_provenance=expected_provenance
    )
    capabilities = manifest.get("capabilities")
    if required_capability not in capabilities:
        raise RecoveryBootstrapError("recovery generation capability is unavailable")
    records = {str(record["path"]): record for record in manifest["files"]}
    for name, record in records.items():
        data = _read_stable_file(
            generation / name,
            maximum=MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_FILE_BYTES,
            label=f"recovery generation member {name}",
            require_root=True,
            mode=0o444 if name == "manifest.json" else (
                EXECUTABLE_MODE if name.endswith(".sh") else DATA_MODE
            ),
        )
        if _sha256(data) != record["sha256"]:
            raise RecoveryBootstrapError("recovery generation member digest is invalid")
    if expected_members is not None:
        if set(expected_members) != set(records) | {"manifest.json"}:
            raise RecoveryBootstrapError("recovery generation does not match the bundle")
        for name, expected in expected_members.items():
            actual = _read_stable_file(
                generation / name,
                maximum=MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_FILE_BYTES,
                label=f"recovery generation member {name}",
                require_root=True,
                mode=0o444 if name == "manifest.json" else (
                    EXECUTABLE_MODE if name.endswith(".sh") else DATA_MODE
                ),
            )
            if actual != expected:
                raise RecoveryBootstrapError("recovery generation does not match the bundle")


def install_bundle(
    bundle: Path,
    *,
    app_dir: Path,
    expected_bundle_sha: str | None = None,
    expected_source_sha: str | None = None,
    expected_provenance: dict[str, object] | None = None,
) -> Path:
    """Atomically install one verified bundle under its content digest."""

    if os.geteuid() != 0:
        raise RecoveryBootstrapError("recovery generation install requires root")
    verified = verify_bundle(
        bundle,
        expected_source_sha=expected_source_sha,
        expected_provenance=expected_provenance,
    )
    bundle_sha = str(verified["bundle_sha256"])
    if expected_bundle_sha is not None and bundle_sha != expected_bundle_sha:
        raise RecoveryBootstrapError("recovery archive digest does not match")
    if not HEX64_RE.fullmatch(bundle_sha):
        raise RecoveryBootstrapError("recovery archive digest is invalid")
    if not app_dir.is_absolute() or app_dir == Path("/"):
        raise RecoveryBootstrapError("recovery application path is invalid")
    _safe_host_directory(app_dir)
    shared = app_dir / "shared"
    _safe_host_directory(shared)
    recovery = shared / ".release-recovery"
    if not os.path.lexists(recovery):
        recovery.mkdir(mode=0o755)
        os.chown(recovery, 0, 0)
        os.chmod(recovery, 0o755)
    _safe_host_directory(recovery, mode=0o755)
    generations = recovery / "generations"
    if not os.path.lexists(generations):
        generations.mkdir(mode=0o755)
        os.chown(generations, 0, 0)
        os.chmod(generations, 0o755)
    _safe_host_directory(generations, mode=0o755)
    target = generations / bundle_sha
    if os.path.lexists(target):
        _validate_generation_tree(
            target,
            bundle_sha=bundle_sha,
            expected_members=verified["members"],
            expected_provenance=expected_provenance,
        )
        return target
    temporary = generations / f".{bundle_sha}.install-{os.getpid()}"
    if os.path.lexists(temporary):
        raise RecoveryBootstrapError("recovery generation staging path already exists")
    temporary.mkdir(mode=0o700)
    os.chown(temporary, 0, 0)
    try:
        members = verified["members"]
        for name in sorted(set(RECOVERY_FILES) | {"manifest.json"}):
            mode = DATA_MODE if name == "manifest.json" or not name.endswith(".sh") else EXECUTABLE_MODE
            destination = temporary / name
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            try:
                view = memoryview(members[name])
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise RecoveryBootstrapError("recovery generation member could not be written")
                    view = view[written:]
                os.fchmod(descriptor, mode)
                os.fchown(descriptor, 0, 0)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.chmod(temporary, 0o555)
        _validate_generation_tree(
            temporary,
            bundle_sha=bundle_sha,
            require_generation_name=False,
            expected_provenance=expected_provenance,
        )
        os.rename(temporary, target)
        directory_fd = os.open(generations, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if os.path.lexists(temporary) and not temporary.is_symlink():
            for item in sorted(temporary.iterdir(), key=lambda value: value.name):
                if item.is_file() and not item.is_symlink():
                    item.unlink()
            temporary.rmdir()
        raise
    return target


def _safe_receipt(path: Path) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > 64 * 1024
    ):
        raise RecoveryBootstrapError("release receipt metadata is unsafe")


def _receipt_json(path: Path) -> dict[str, object]:
    _safe_receipt(path)
    value = _read_bounded_json(
        path, maximum=64 * 1024, label="release receipt", require_root=True
    )
    if not isinstance(value, dict):
        raise RecoveryBootstrapError("release receipt is invalid")
    return value


def _release_pointer(app_dir: Path, name: str) -> Path:
    pointer = app_dir / name
    metadata = pointer.lstat()
    if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_nlink != 1:
        raise RecoveryBootstrapError("release pointer metadata is unsafe")
    target = pointer.resolve(strict=True)
    releases = app_dir / "releases"
    if target.parent != releases or not target.is_dir() or target.is_symlink():
        raise RecoveryBootstrapError("release pointer target is unsafe")
    return target


def _receipt_identity(value: object, *, required: bool) -> dict[str, int] | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"dev", "ino"}
        or type(value.get("dev")) is not int
        or type(value.get("ino")) is not int
        or value["dev"] < 0
        or value["ino"] <= 0
    ):
        raise RecoveryBootstrapError("release receipt identity is invalid")
    return {"dev": value["dev"], "ino": value["ino"]}


def _receipt_release_path(value: object, *, app_dir: Path, label: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RecoveryBootstrapError(f"release receipt {label} is invalid")
    path = Path(value)
    if path.parent != app_dir / "releases" or path.name in {"", ".", ".."}:
        raise RecoveryBootstrapError(f"release receipt {label} is invalid")
    return path


def _validate_initial_systemd_snapshot(receipt: dict[str, object]) -> None:
    """Validate the transaction-bound clean-install systemd baseline.

    The recovery generation must understand this field before it can consume
    a v2 operation receipt.  Missing fields remain accepted only for legacy
    receipts; a present field is never treated as advisory metadata.
    """

    snapshot = receipt.get("systemd_state_before")
    if snapshot is None:
        return
    if (
        receipt.get("operation") != "install"
        or receipt.get("current_before") is not None
        or receipt.get("previous_before") is not None
    ):
        raise RecoveryBootstrapError("release receipt initial systemd snapshot is unexpected")
    if not isinstance(snapshot, dict) or set(snapshot) != set(INITIAL_SYSTEMD_UNITS):
        raise RecoveryBootstrapError("release receipt initial systemd snapshot is incomplete")
    for unit in INITIAL_SYSTEMD_UNITS:
        state = snapshot.get(unit)
        if (
            not isinstance(state, dict)
            or set(state) != {"active", "enabled"}
            or state.get("active") != "inactive"
            or state.get("enabled") != "disabled"
        ):
            raise RecoveryBootstrapError(
                "release receipt initial systemd snapshot is invalid"
            )


def _validate_receipt_directory(path: Path, identity: dict[str, int], *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RecoveryBootstrapError(f"release receipt {label} is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink < 2
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or {"dev": metadata.st_dev, "ino": metadata.st_ino} != identity
    ):
        raise RecoveryBootstrapError(f"release receipt {label} identity changed")


def _validate_receipt_identity(receipt: dict[str, object], app_dir: Path) -> Path:
    expected = {
        "operation_id",
        "version", "operation", "phase", "app_dir", "current_before", "previous_before", "candidate_release",
        "shared_venv", "peer", "snapshot", "transition", "shared_before", "peer_before", "current_before_identity",
        "previous_before_identity", "candidate_identity", "remove_env_on_recovery", "service_state_before",
        "service_enabled_before", "quiesced_services", "timer_active_before",
        "timer_enabled_before", "systemd_state_before",
    }
    # Operation-less receipts are the narrowly supported pre-operation v2
    # bridge. They predate enabled-state capture and may only be consumed by
    # the cleanup-only path; a mixed legacy/v2 schema is not supported.
    legacy_expected = expected - {
        "operation_id", "service_enabled_before", "timer_enabled_before",
        "systemd_state_before",
    }
    v2_without_systemd = expected - {"systemd_state_before"}
    legacy = set(receipt) == legacy_expected
    if (
        (set(receipt) not in (expected, v2_without_systemd) and not legacy)
        or type(receipt.get("version")) is not int
        or receipt.get("version") != 2
        or receipt.get("operation") != "install"
    ):
        raise RecoveryBootstrapError("release receipt schema is invalid")
    if not legacy and (
        not isinstance(receipt.get("operation_id"), str)
        or OPERATION_ID_RE.fullmatch(receipt["operation_id"]) is None
    ):
        raise RecoveryBootstrapError("release receipt operation identity is invalid")
    _validate_initial_systemd_snapshot(receipt)
    phase = receipt.get("phase")
    if phase in MIGRATION_OUTCOME_UNCERTAIN_PHASES:
        raise RecoveryBootstrapError("migration outcome is uncertain")
    if phase != "recovery-restored":
        raise RecoveryBootstrapError("release receipt is not recovery-restored")
    if receipt.get("app_dir") != str(app_dir):
        raise RecoveryBootstrapError("release receipt application identity is invalid")
    if type(receipt.get("remove_env_on_recovery")) is not bool:
        raise RecoveryBootstrapError("release receipt recovery flag is invalid")
    service_state = receipt.get("service_state_before")
    if (
        not isinstance(service_state, dict)
        or set(service_state) != {"deadlock-api", "deadlock-worker", "deadlock-web"}
        or any(type(value) is not str or value not in {"active", "inactive"} for value in service_state.values())
    ):
        raise RecoveryBootstrapError("release receipt service state is invalid")
    if receipt.get("quiesced_services") != ["deadlock-api", "deadlock-worker", "deadlock-web"]:
        raise RecoveryBootstrapError("release receipt quiesced services are invalid")
    if type(receipt.get("timer_active_before")) is not bool:
        raise RecoveryBootstrapError("release receipt timer state is invalid")
    if not legacy:
        service_enabled = receipt.get("service_enabled_before")
        if (
            not isinstance(service_enabled, dict)
            or set(service_enabled) != {"deadlock-api", "deadlock-worker", "deadlock-web"}
            or any(
                type(value) is not str or value not in {"enabled", "disabled"}
                for value in service_enabled.values()
            )
            or receipt.get("timer_enabled_before") not in {"enabled", "disabled"}
        ):
            raise RecoveryBootstrapError("release receipt enabled state is invalid")
    current_before = _receipt_release_path(
        receipt.get("current_before"), app_dir=app_dir, label="current identity"
    )
    if current_before is None:
        raise RecoveryBootstrapError("release receipt current identity is invalid")
    previous_before = _receipt_release_path(
        receipt.get("previous_before"), app_dir=app_dir, label="previous identity"
    )
    current_identity = _receipt_identity(
        receipt.get("current_before_identity"), required=True
    )
    previous_identity = _receipt_identity(
        receipt.get("previous_before_identity"), required=previous_before is not None
    )
    candidate = _receipt_release_path(
        receipt.get("candidate_release"), app_dir=app_dir, label="candidate identity"
    )
    candidate_identity = _receipt_identity(
        receipt.get("candidate_identity"), required=True
    )
    if candidate is None or candidate in {current_before, previous_before}:
        raise RecoveryBootstrapError("release receipt candidate identity is invalid")
    shared_venv = Path(str(receipt.get("shared_venv")))
    snapshot = Path(str(receipt.get("snapshot")))
    peer = Path(str(receipt.get("peer")))
    if shared_venv != app_dir / "shared" / "venv" or snapshot != candidate / ".rollback" / "shared-venv-before-install":
        raise RecoveryBootstrapError("release receipt venv paths are invalid")
    if peer.parent != app_dir / "shared" or not peer.name.startswith(f".venv-install-{candidate.name}."):
        raise RecoveryBootstrapError("release receipt venv peer path is invalid")
    transition = receipt.get("transition")
    shared_before = receipt.get("shared_before")
    peer_before = receipt.get("peer_before")
    if transition == "exchange":
        _receipt_identity(shared_before, required=True)
        _receipt_identity(peer_before, required=True)
        if shared_before == peer_before:
            raise RecoveryBootstrapError("release receipt venv identities are ambiguous")
    elif transition == "create":
        if shared_before is not None or _receipt_identity(peer_before, required=True) is None:
            raise RecoveryBootstrapError("release receipt created venv identity is invalid")
    elif transition == "none":
        if peer_before is not None:
            raise RecoveryBootstrapError("release receipt no-op peer identity is invalid")
        if shared_before is not None:
            _receipt_identity(shared_before, required=True)
    else:
        raise RecoveryBootstrapError("release receipt venv transition is invalid")
    current = _release_pointer(app_dir, "current")
    if current != current_before:
        raise RecoveryBootstrapError("current release does not match receipt")
    _validate_receipt_directory(current, current_identity, label="current release")
    if previous_before is not None:
        if _release_pointer(app_dir, "previous") != previous_before:
            raise RecoveryBootstrapError("previous release does not match receipt")
        assert previous_identity is not None
        _validate_receipt_directory(previous_before, previous_identity, label="previous release")
    elif os.path.lexists(app_dir / "previous"):
        raise RecoveryBootstrapError("unexpected previous release pointer")
    if candidate_identity is None:
        raise RecoveryBootstrapError("release receipt candidate identity is invalid")
    if os.path.lexists(candidate):
        _validate_receipt_directory(candidate, candidate_identity, label="candidate release")
    elif receipt["phase"] != "recovery-restored":
        raise RecoveryBootstrapError("release receipt candidate release is unavailable")
    return current


def _validate_initial_receipt_identity(
    receipt: dict[str, object], app_dir: Path
) -> None:
    """Validate a clean first-install receipt before immutable cleanup."""

    if (
        receipt.get("version") != 2
        or receipt.get("operation") != "install"
        or receipt.get("current_before") is not None
        or receipt.get("previous_before") is not None
        or receipt.get("app_dir") != str(app_dir)
    ):
        raise RecoveryBootstrapError("initial release receipt topology is invalid")
    if (
        not isinstance(receipt.get("operation_id"), str)
        or OPERATION_ID_RE.fullmatch(receipt["operation_id"]) is None
    ):
        raise RecoveryBootstrapError("initial release receipt operation identity is invalid")
    phase = receipt.get("phase")
    if phase not in INITIAL_RECOVERY_PHASES:
        raise RecoveryBootstrapError("initial release receipt phase is invalid")
    _validate_initial_systemd_snapshot(receipt)
    if phase in INITIAL_SYSTEMD_PHASES and not isinstance(
        receipt.get("systemd_state_before"), dict
    ):
        raise RecoveryBootstrapError(
            "initial release receipt systemd snapshot is missing"
        )
    if type(receipt.get("remove_env_on_recovery")) is not bool:
        raise RecoveryBootstrapError("release receipt recovery flag is invalid")
    if receipt.get("candidate_release") is None:
        raise RecoveryBootstrapError("initial release receipt candidate is invalid")
    candidate = _receipt_release_path(
        receipt.get("candidate_release"), app_dir=app_dir, label="candidate identity"
    )
    assert candidate is not None
    candidate_identity = _receipt_identity(
        receipt.get("candidate_identity"), required=True
    )
    assert candidate_identity is not None
    if os.path.lexists(candidate):
        _validate_receipt_directory(candidate, candidate_identity, label="candidate release")
    elif phase != "recovery-restored":
        raise RecoveryBootstrapError("initial release receipt candidate is unavailable")

    shared_venv = Path(str(receipt.get("shared_venv")))
    snapshot = Path(str(receipt.get("snapshot")))
    peer = Path(str(receipt.get("peer")))
    if (
        shared_venv != app_dir / "shared" / "venv"
        or snapshot != candidate / ".rollback" / "shared-venv-before-install"
        or peer.parent != app_dir / "shared"
        or not peer.name.startswith(f".venv-install-{candidate.name}.")
    ):
        raise RecoveryBootstrapError("initial release receipt venv paths are invalid")
    transition = receipt.get("transition")
    shared_before = receipt.get("shared_before")
    peer_before = receipt.get("peer_before")
    if transition == "exchange":
        _receipt_identity(shared_before, required=True)
        _receipt_identity(peer_before, required=True)
        if shared_before == peer_before:
            raise RecoveryBootstrapError("initial venv identities are ambiguous")
    elif transition == "create":
        if shared_before is not None or _receipt_identity(peer_before, required=True) is None:
            raise RecoveryBootstrapError("initial created venv identity is invalid")
    elif transition == "none":
        if peer_before is not None:
            raise RecoveryBootstrapError("initial no-op peer identity is invalid")
        if shared_before is not None:
            _receipt_identity(shared_before, required=True)
    else:
        raise RecoveryBootstrapError("initial release receipt venv transition is invalid")

    current_pointer = app_dir / "current"
    previous_pointer = app_dir / "previous"
    if os.path.lexists(previous_pointer):
        raise RecoveryBootstrapError("unexpected initial previous release pointer")
    if os.path.lexists(current_pointer):
        current = _release_pointer(app_dir, "current")
        if current != candidate:
            raise RecoveryBootstrapError("initial current pointer does not match receipt")
    elif phase in {
        "current-switched",
        "previous-switched",
        "pointers-switched",
        "activation-pending",
        "services-restarted",
        "nginx-pending",
        "nginx-applied",
        "smoke-passed",
        "systemd-activation-pending",
        "systemd-activated",
        "activation-committed",
        "recovery-authorized",
    }:
        raise RecoveryBootstrapError("initial current pointer is missing")


def _abort_initial_retained_only(*, app_dir: Path, generation: Path) -> None:
    """Recover a clean install through the canonical transaction boundary."""

    shared = app_dir / "shared"
    state = shared / ".release-operation.json"
    transaction = generation / "platform_release_transaction.py"
    receipt = _receipt_json(state)
    _validate_initial_receipt_identity(receipt, app_dir)
    phase = str(receipt["phase"])
    systemd_authority = phase in INITIAL_SYSTEMD_PHASES
    if systemd_authority:
        _run_recovery_child(
            [
                "/usr/bin/python3",
                "-I",
                str(transaction),
                "validate-initial-systemd",
                "--state",
                str(state),
                "--systemctl",
                "/usr/bin/systemctl",
            ]
        )
        _run_recovery_child(
            [
                "/usr/bin/python3",
                "-I",
                str(transaction),
                "restore-initial-systemd",
                "--state",
                str(state),
                "--systemctl",
                "/usr/bin/systemctl",
            ]
        )
    if phase in MIGRATION_OUTCOME_UNCERTAIN_PHASES:
        # Authorization is intentionally after systemd restoration.  A crash
        # before this write leaves the original uncertain phase retryable.
        _run_recovery_child(
            [
                "/usr/bin/python3",
                "-I",
                str(transaction),
                "authorize-recovery",
                "--state",
                str(state),
                "--confirm",
                "MIGRATION_NOT_REVERSED",
            ]
        )
        phase = "recovery-authorized"
    if phase != "recovery-restored":
        _run_recovery_child(
            [
                "/usr/bin/python3",
                "-I",
                str(transaction),
                "recover",
                "--retain",
                "--state",
                str(state),
            ]
        )
    _run_recovery_child(
        [
            "/usr/bin/python3",
            "-I",
            str(transaction),
            "complete-recovery",
            "--retain-receipt",
            "--state",
            str(state),
        ]
    )
    if systemd_authority:
        _run_recovery_child(
            [
                "/usr/bin/python3",
                "-I",
                str(transaction),
                "verify-initial-systemd",
                "--state",
                str(state),
                "--systemctl",
                "/usr/bin/systemctl",
            ]
        )
    _run_recovery_child(
        [
            "/usr/bin/python3",
            "-I",
            str(transaction),
            "complete-recovery",
            "--state",
            str(state),
        ]
    )


def _terminate_recovery_child_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate a timed-out immutable helper and its descendants."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=RECOVERY_CHILD_TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()
    try:
        process.wait(timeout=RECOVERY_CHILD_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _run_recovery_child(command: list[str]) -> None:
    """Run one immutable recovery child with a bounded fail-closed deadline."""

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        raise RecoveryBootstrapError(
            "retained recovery child could not start; receipts remain for retry"
        ) from exc
    try:
        return_code = process.wait(timeout=RECOVERY_SUBPROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_recovery_child_group(process)
        raise RecoveryBootstrapError(
            "retained recovery child timed out; receipts remain for retry"
        ) from exc
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)


def abort_retained_only(*, app_dir: Path, generation: Path) -> None:
    """Restore runtime and complete only an already pointer-restored receipt."""

    if os.geteuid() != 0:
        raise RecoveryBootstrapError("retained recovery requires root")
    _validate_generation_tree(generation, bundle_sha=generation.name)
    shared = app_dir / "shared"
    state = shared / ".release-operation.json"
    systemd_state = shared / ".release-systemd-state.json"
    state_present = os.path.lexists(state)
    systemd_state_present = os.path.lexists(systemd_state)
    # A retry after the final transaction receipt was removed is a safe
    # idempotent no-op.  A half-pair is never treated as completed: it could
    # represent an interrupted cleanup or an unrelated host mutation.
    if not state_present:
        if systemd_state_present:
            raise RecoveryBootstrapError("retained recovery receipts are incomplete")
        return
    receipt = _receipt_json(state)
    if (
        receipt.get("operation") == "install"
        and receipt.get("version") == 2
        and receipt.get("current_before") is None
        and receipt.get("previous_before") is None
    ):
        # A clean first-install receipt is governed by its single transaction
        # snapshot. A second systemd receipt is an ambiguous/stale pair: do
        # not let the transaction cleanup proceed while that state could
        # still describe a different operation. Retain both for an explicit
        # operator recovery path and make a retry deterministic.
        if systemd_state_present:
            raise RecoveryBootstrapError(
                "initial release has an unexpected systemd receipt"
            )
        _abort_initial_retained_only(app_dir=app_dir, generation=generation)
        return
    release = _validate_receipt_identity(receipt, app_dir)
    transaction = generation / "platform_release_transaction.py"
    runtime = generation / "platform_release_restore_runtime.sh"
    systemd = generation / "platform_release_systemd_state.py"
    liveqa = generation / "platform_live_qa_runtime_install.py"
    if "operation_id" not in receipt:
        # The deployed v2 receipt predates operation correlation.  It is a
        # narrowly scoped, read-only compatibility bridge: with no systemd
        # receipt present, prove the peer is already absent and let only the
        # trusted transaction cleanup consume the inactive candidate.  Never
        # synthesize an identity or execute retained runtime helpers for this
        # legacy path.
        if systemd_state_present:
            raise RecoveryBootstrapError(
                "legacy release receipt cannot authorize systemd recovery"
            )
        peer_value = receipt.get("peer")
        if not isinstance(peer_value, str):
            raise RecoveryBootstrapError("release receipt peer identity is invalid")
        peer = Path(peer_value)
        if os.path.lexists(peer):
            raise RecoveryBootstrapError("legacy release receipt peer is present")
        _run_recovery_child([
            "/usr/bin/python3", "-I", str(transaction), "complete-recovery",
            "--state", str(state), "--retain-receipt",
        ])
        _run_recovery_child([
            "/usr/bin/python3", "-I", str(transaction), "complete-recovery",
            "--state", str(state),
        ])
        return
    if systemd_state_present:
        if (
            not isinstance(receipt.get("operation_id"), str)
            or OPERATION_ID_RE.fullmatch(receipt["operation_id"]) is None
        ):
            raise RecoveryBootstrapError(
                "legacy release receipt cannot authorize systemd recovery"
            )
        _safe_receipt(systemd_state)
        # Correlate both durable receipts before the runtime helper can touch
        # unit files, Nginx, or the live-QA runtime.  A stale systemd receipt
        # may be individually valid after a crash; it is not valid for a new
        # transaction merely because the app directory still matches.
        _run_recovery_child([
            "/usr/bin/python3", "-I", str(systemd), "validate",
            "--state", str(systemd_state),
            "--app-dir", str(app_dir),
            "--helper-release", str(release),
            "--transaction", str(state),
            "--systemctl", "/usr/bin/systemctl",
        ])
        command = [
            str(runtime),
            "--app-dir", str(app_dir),
            "--release", str(release),
            "--systemd-state", str(systemd_state),
            "--transaction", str(state),
            "--systemctl", "/usr/bin/systemctl",
            "--live-qa-runtime-installer", str(liveqa),
        ]
        _run_recovery_child(command)
        if _release_pointer(app_dir, "current") != release:
            raise RecoveryBootstrapError("current release changed during retained recovery")
        if receipt.get("previous_before") is not None and _release_pointer(app_dir, "previous") != Path(str(receipt["previous_before"])):
            raise RecoveryBootstrapError("previous release changed during retained recovery")
        _run_recovery_child([
            "/usr/bin/python3", "-I", str(systemd), "verify", "--state", str(systemd_state),
            "--app-dir", str(app_dir), "--helper-release", str(release),
            "--transaction", str(state),
            "--systemctl", "/usr/bin/systemctl",
        ])
        # Remove candidate/venv cleanup artifacts but retain the operation
        # receipt until the durable systemd receipt is also cleared.  A crash
        # after either side effect can therefore resume without guessing.
        _run_recovery_child([
            "/usr/bin/python3", "-I", str(transaction), "complete-recovery", "--state", str(state),
            "--retain-receipt",
        ])
        _run_recovery_child([
            "/usr/bin/python3", "-I", str(systemd), "clear", "--state", str(systemd_state),
            "--app-dir", str(app_dir), "--helper-release", str(release),
            "--transaction", str(state),
            "--systemctl", "/usr/bin/systemctl",
        ])
    else:
        # The first attempt may have retained the operation receipt after
        # completing its filesystem cleanup, then removed the systemd receipt
        # before a process failure.  Do not rerun runtime/systemd side effects;
        # prove the cleanup half is complete and finish the receipt deletion.
        candidate = _receipt_release_path(
            receipt.get("candidate_release"), app_dir=app_dir, label="candidate identity"
        )
        peer = Path(str(receipt.get("peer")))
        if os.path.lexists(candidate) or os.path.lexists(peer):
            raise RecoveryBootstrapError("systemd receipt is missing before transaction cleanup")

    # This is the only operation that removes the release receipt.  It is
    # deliberately after successful systemd clear, and is safe to retry after
    # a failure in either prior subprocess.
    _run_recovery_child([
        "/usr/bin/python3", "-I", str(transaction), "complete-recovery", "--state", str(state),
    ])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Immutable retained-release recovery bootstrap")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--source-root", type=Path, required=True)
    build.add_argument("--source-sha", required=True)
    build.add_argument("--provenance", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--bundle", type=Path, required=True)
    validate.add_argument("--source-sha")
    validate.add_argument("--provenance", type=Path)
    validate_generation = commands.add_parser("validate-generation")
    validate_generation.add_argument("--generation", type=Path, required=True)
    validate_generation.add_argument("--bundle-sha", required=True)
    validate_generation.add_argument("--provenance", type=Path)
    validate_generation.add_argument(
        "--capability",
        choices=(CAPABILITY, RECOVER_PENDING_CAPABILITY),
        default=CAPABILITY,
    )
    install = commands.add_parser("install")
    install.add_argument("--bundle", type=Path, required=True)
    install.add_argument("--app-dir", type=Path, required=True)
    install.add_argument("--expected-bundle-sha")
    install.add_argument("--source-sha")
    install.add_argument("--provenance", type=Path)
    install.add_argument(
        "--capability",
        choices=(CAPABILITY, RECOVER_PENDING_CAPABILITY),
        default=CAPABILITY,
    )
    abort = commands.add_parser("abort_retained_only")
    abort.add_argument("--app-dir", type=Path, required=True)
    abort.add_argument("--generation", type=Path)
    publish_validate = commands.add_parser("publish-validate")
    publish_validate.add_argument("--metadata", type=Path, required=True)
    publish_validate.add_argument("--provenance-output", type=Path, required=True)
    publish_validate.add_argument("--github-output", type=Path, required=True)
    publish_validate.add_argument("--repository", required=True)
    publish_validate.add_argument("--source-sha", required=True)
    publish_validate.add_argument("--security-run-id", required=True)
    publish_validate.add_argument("--security-run-attempt", required=True)
    publish_validate.add_argument("--security-workflow", required=True)
    publish_validate.add_argument("--security-workflow-path", required=True)
    publish_validate.add_argument("--security-job", required=True)
    publish_validate.add_argument("--recovery-run-id", required=True)
    publish_validate.add_argument("--recovery-run-attempt", required=True)
    publish_validate.add_argument("--recovery-workflow-sha", required=True)
    publish_validate.add_argument("--recovery-job", required=True)
    publish_validate.add_argument("--github-ref", required=True)
    publisher_select = commands.add_parser("publisher-select")
    publisher_select.add_argument("--metadata", type=Path, required=True)
    publisher_select.add_argument("--github-output", type=Path, required=True)
    publisher_select.add_argument("--repository", required=True)
    publisher_select.add_argument("--producer-run-id", required=True)
    publisher_select.add_argument("--producer-run-attempt", required=True)
    publisher_select.add_argument("--producer-workflow", required=True)
    publisher_select.add_argument("--producer-workflow-path", required=True)
    publisher_validate = commands.add_parser("publisher-validate")
    publisher_validate.add_argument("--metadata", type=Path, required=True)
    publisher_validate.add_argument("--provenance-output", type=Path, required=True)
    publisher_validate.add_argument("--github-output", type=Path, required=True)
    publisher_validate.add_argument("--repository", required=True)
    publisher_validate.add_argument("--producer-run-id", required=True)
    publisher_validate.add_argument("--producer-run-attempt", required=True)
    publisher_validate.add_argument("--producer-workflow", required=True)
    publisher_validate.add_argument("--producer-workflow-path", required=True)
    publisher_validate.add_argument("--security-workflow", required=True)
    publisher_validate.add_argument("--security-workflow-path", required=True)
    publisher_validate.add_argument("--security-job", required=True)
    publisher_validate.add_argument("--publisher-workflow-sha", required=True)
    publisher_validate.add_argument("--publisher-run-id", required=True)
    publisher_validate.add_argument("--publisher-run-attempt", required=True)
    publisher_validate.add_argument("--publisher-job-id", required=True)
    publisher_validate.add_argument("--github-ref", required=True)
    publish_artifact = commands.add_parser("publish-artifact-id")
    publish_artifact.add_argument("--metadata", type=Path, required=True)
    publish_artifact.add_argument("--artifact-id-output", type=Path, required=True)
    publish_artifact.add_argument("--artifact-name", required=True)
    publish_artifact.add_argument("--recovery-run-id", required=True)
    publish_artifact.add_argument("--recovery-run-attempt", required=True)
    publish_artifact.add_argument("--source-sha", required=True)
    publish_artifact.add_argument("--recovery-workflow-sha", required=True)
    publisher_artifact = commands.add_parser("publisher-artifact")
    publisher_artifact.add_argument("--metadata", type=Path, required=True)
    publisher_artifact.add_argument("--artifact-name", required=True)
    publisher_artifact.add_argument("--publisher-run-id", required=True)
    publisher_artifact.add_argument("--publisher-run-attempt", required=True)
    publisher_artifact.add_argument("--publisher-workflow-sha", required=True)
    publisher_artifact.add_argument("--github-output", type=Path, required=True)
    publish_bundle = commands.add_parser("publish-bundle")
    publish_bundle.add_argument("--archive", type=Path, required=True)
    publish_bundle.add_argument("--output", type=Path, required=True)
    publish_bundle.add_argument("--expected-name", required=True)
    publish_bundle.add_argument("--expected-sha", required=True)
    publish_bundle.add_argument("--artifact-sha256")
    publish_evidence = commands.add_parser("publish-evidence")
    publish_evidence.add_argument("--provenance", type=Path, required=True)
    publish_evidence.add_argument("--evidence", type=Path, required=True)
    publish_evidence.add_argument("--github-output", type=Path, required=True)
    publish_evidence.add_argument("--bundle-name", required=True)
    publish_evidence.add_argument("--bundle-sha", required=True)
    publish_evidence.add_argument("--bundle-artifact-sha256")
    publish_evidence.add_argument("--recovery-run-id", required=True)
    publish_evidence.add_argument("--recovery-run-attempt", required=True)
    publish_evidence.add_argument("--recovery-job-id", required=True)
    publish_evidence.add_argument("--publisher-workflow-sha")
    publish_evidence.add_argument("--publisher-run-id")
    publish_evidence.add_argument("--publisher-run-attempt")
    publish_evidence.add_argument("--publisher-job-id")
    publish_evidence.add_argument("--publisher-bundle-name")
    publish_evidence.add_argument("--publisher-bundle-artifact-id")
    publish_evidence.add_argument("--publisher-bundle-artifact-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        expected_provenance = None
        provenance_path = getattr(args, "provenance", None)
        if provenance_path is not None:
            value = _read_bounded_json(provenance_path, maximum=MAX_PROVENANCE_BYTES, label="recovery provenance")
            expected_provenance = _provenance_schema(value)
        if args.command == "build":
            provenance = _read_bounded_json(args.provenance, maximum=MAX_PROVENANCE_BYTES, label="recovery provenance")
            if not isinstance(provenance, dict):
                raise RecoveryBootstrapError("recovery provenance is invalid")
            result = build_bundle(args.source_root, source_sha=args.source_sha, provenance=provenance, output=args.output)
            print(json.dumps({"schema": SCHEMA, "capability": "recovery_bootstrap", "capabilities": [CAPABILITY, RECOVER_PENDING_CAPABILITY], "bundle_sha256": result["bundle_sha256"], "deployable": False}, sort_keys=True))
        elif args.command == "validate":
            result = verify_bundle(args.bundle, expected_source_sha=args.source_sha, expected_provenance=expected_provenance)
            print(json.dumps({"schema": SCHEMA, "capability": "recovery_bootstrap", "capabilities": [CAPABILITY, RECOVER_PENDING_CAPABILITY], "bundle_sha256": result["bundle_sha256"], "deployable": False}, sort_keys=True))
        elif args.command == "validate-generation":
            _validate_generation_tree(
                args.generation,
                bundle_sha=args.bundle_sha,
                expected_provenance=expected_provenance,
                required_capability=args.capability,
            )
            print(json.dumps({"schema": SCHEMA, "capability": args.capability, "generation": str(args.generation), "deployable": False}, sort_keys=True))
        elif args.command == "install":
            target = install_bundle(args.bundle, app_dir=args.app_dir, expected_bundle_sha=args.expected_bundle_sha, expected_source_sha=args.source_sha, expected_provenance=expected_provenance)
            _validate_generation_tree(
                target,
                bundle_sha=target.name,
                expected_provenance=expected_provenance,
                required_capability=args.capability,
            )
            print(json.dumps({"schema": SCHEMA, "capability": args.capability, "generation": str(target), "deployable": False}, sort_keys=True))
        elif args.command == "abort_retained_only":
            generation = args.generation or Path(__file__).resolve().parent
            abort_retained_only(app_dir=args.app_dir, generation=generation)
            print("RECOVERY_BOOTSTRAP schema=1 status=complete capability=abort_retained_only deployable=false")
        elif args.command == "publish-validate":
            result = validate_publish_metadata(
                args.metadata,
                repository=args.repository,
                source_sha=args.source_sha,
                security_run_id=args.security_run_id,
                security_run_attempt=args.security_run_attempt,
                security_workflow=args.security_workflow,
                security_workflow_path=args.security_workflow_path,
                security_job=args.security_job,
                recovery_run_id=args.recovery_run_id,
                recovery_run_attempt=args.recovery_run_attempt,
                recovery_workflow_sha=args.recovery_workflow_sha,
                recovery_job=args.recovery_job,
                github_ref=args.github_ref,
            )
            provenance = result["provenance"]
            provenance_path = args.provenance_output
            if provenance_path.exists() or provenance_path.is_symlink():
                raise RecoveryBootstrapError("recovery provenance output already exists")
            provenance_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            provenance_path.write_bytes(_canonical_json(provenance))
            provenance_path.chmod(0o600)
            with args.github_output.open("a", encoding="ascii") as stream:
                stream.write(f"provenance={provenance_path}\n")
                stream.write(f"artifact_name={provenance['artifact_name']}\n")
                stream.write(f"artifact_id={result['route_artifact_id']}\n")
                stream.write(f"artifact_sha256={provenance['artifact_sha256']}\n")
                stream.write(f"recovery_job_id={result['recovery_job_id']}\n")
        elif args.command == "publisher-select":
            result = select_publisher_bundle_metadata(
                args.metadata,
                repository=args.repository,
                producer_run_id=args.producer_run_id,
                producer_run_attempt=args.producer_run_attempt,
                producer_workflow=args.producer_workflow,
                producer_workflow_path=args.producer_workflow_path,
            )
            with args.github_output.open("a", encoding="ascii") as stream:
                for key in (
                    "producer_workflow_sha",
                    "producer_job_id",
                    "bundle_artifact_id",
                    "bundle_name",
                    "source_sha",
                    "security_run_id",
                    "security_run_attempt",
                    "bundle_artifact_sha256",
                ):
                    if key in result:
                        stream.write(f"{key}={result[key]}\n")
        elif args.command == "publisher-validate":
            result = validate_publisher_metadata(
                args.metadata,
                repository=args.repository,
                producer_run_id=args.producer_run_id,
                producer_run_attempt=args.producer_run_attempt,
                producer_workflow=args.producer_workflow,
                producer_workflow_path=args.producer_workflow_path,
                security_workflow=args.security_workflow,
                security_workflow_path=args.security_workflow_path,
                security_job=args.security_job,
                publisher_workflow_sha=args.publisher_workflow_sha,
                publisher_run_id=args.publisher_run_id,
                publisher_run_attempt=args.publisher_run_attempt,
                publisher_job_id=args.publisher_job_id,
                github_ref=args.github_ref,
            )
            provenance = result["provenance"]
            if not isinstance(provenance, dict):
                raise RecoveryBootstrapError("publisher provenance is invalid")
            if args.provenance_output.exists() or args.provenance_output.is_symlink():
                raise RecoveryBootstrapError("publisher provenance output already exists")
            args.provenance_output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            args.provenance_output.write_bytes(_canonical_json(provenance))
            args.provenance_output.chmod(0o600)
            with args.github_output.open("a", encoding="ascii") as stream:
                for key in (
                    "source_sha",
                    "security_run_id",
                    "security_run_attempt",
                    "security_job_id",
                    "producer_run_id",
                    "producer_run_attempt",
                    "producer_workflow_sha",
                    "producer_job_id",
                    "bundle_name",
                    "bundle_artifact_id",
                    "bundle_artifact_sha256",
                    "publisher_workflow_sha",
                    "publisher_run_id",
                    "publisher_run_attempt",
                    "publisher_job_id",
                ):
                    stream.write(f"{key}={result[key]}\n")
                stream.write(f"route_digest={provenance['artifact_sha256']}\n")
                stream.write(f"provenance={args.provenance_output}\n")
        elif args.command == "publish-artifact-id":
            artifact_id = validate_publish_artifact_metadata(
                args.metadata,
                expected_name=args.artifact_name,
                expected_run_id=args.recovery_run_id,
                expected_run_attempt=args.recovery_run_attempt,
                expected_source_sha=args.source_sha,
                expected_workflow_sha=args.recovery_workflow_sha,
            )
            if args.artifact_id_output.exists() or args.artifact_id_output.is_symlink():
                raise RecoveryBootstrapError("recovery artifact id output already exists")
            args.artifact_id_output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            args.artifact_id_output.write_text(f"{artifact_id}\n", encoding="ascii")
            args.artifact_id_output.chmod(0o600)
        elif args.command == "publisher-artifact":
            result = validate_publisher_artifact_metadata(
                args.metadata,
                expected_name=args.artifact_name,
                expected_run_id=args.publisher_run_id,
                expected_run_attempt=args.publisher_run_attempt,
                expected_workflow_sha=args.publisher_workflow_sha,
            )
            with args.github_output.open("a", encoding="ascii") as stream:
                for key in (
                    "publisher_bundle_name",
                    "publisher_bundle_artifact_id",
                    "publisher_bundle_artifact_sha256",
                ):
                    stream.write(f"{key}={result[key]}\n")
        elif args.command == "publish-bundle":
            extract_publish_bundle(
                args.archive,
                args.output,
                expected_name=args.expected_name,
                expected_sha=args.expected_sha,
                expected_archive_sha=args.artifact_sha256,
            )
        elif args.command == "publish-evidence":
            value = _read_bounded_json(
                args.provenance,
                maximum=MAX_PROVENANCE_BYTES,
                label="recovery provenance",
            )
            if not isinstance(value, dict):
                raise RecoveryBootstrapError("recovery provenance is invalid")
            payload = build_publish_evidence(
                value,
                bundle_name=args.bundle_name,
                bundle_sha=args.bundle_sha,
                bundle_artifact_sha256=args.bundle_artifact_sha256,
                recovery_run_id=args.recovery_run_id,
                recovery_run_attempt=args.recovery_run_attempt,
                recovery_job_id=args.recovery_job_id,
                publisher_workflow_sha=args.publisher_workflow_sha,
                publisher_run_id=args.publisher_run_id,
                publisher_run_attempt=args.publisher_run_attempt,
                publisher_job_id=args.publisher_job_id,
                publisher_bundle_name=args.publisher_bundle_name,
                publisher_bundle_artifact_id=args.publisher_bundle_artifact_id,
                publisher_bundle_artifact_sha256=args.publisher_bundle_artifact_sha256,
            )
            if args.evidence.exists() or args.evidence.is_symlink():
                raise RecoveryBootstrapError("recovery evidence output already exists")
            args.evidence.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            args.evidence.write_bytes(_canonical_json(payload))
            args.evidence.chmod(0o600)
            evidence_name = args.evidence.name
            with args.github_output.open("a", encoding="ascii") as stream:
                stream.write(f"evidence_name={evidence_name}\n")
                stream.write(f"evidence_sha={_sha256(args.evidence.read_bytes())}\n")
        else:  # pragma: no cover - argparse enforces commands
            raise RecoveryBootstrapError("unknown recovery command")
        return 0
    except (RecoveryBootstrapError, OSError, subprocess.CalledProcessError) as exc:
        del exc
        print("RECOVERY_BOOTSTRAP schema=1 status=failed capability=abort_retained_only deployable=false", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
