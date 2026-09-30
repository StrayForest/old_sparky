#!/usr/bin/env python3
"""Canonical, closed release-authority receipt and artifact contracts.

This helper has no third-party dependencies and is used by the production
workflow and its downstream read-only consumers.  The receipt is data-only:
it never contains secrets, SSH material, or free-form logs.  A receipt is
accepted only when its key set, identities, status URL, job results and
artifact binding are exact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping
import zipfile


REPOSITORY_FULL_NAME = "StrayForest/old_sparky"
AUTO_WORKFLOW_NAME = "Platform production auto-deploy"
AUTO_WORKFLOW_PATH = ".github/workflows/platform-production-autodeploy.yml"
DEPLOY_WORKFLOW_NAME = "Platform production deploy"
DEPLOY_WORKFLOW_PATH = ".github/workflows/platform-production-deploy.yml"
DEPLOY_JOB_NAME = "Deploy production"
PREFLIGHT_JOB_NAME = "Production preflight"
AUTO_CALL_JOB_NAME = "Native production deployment"
AUTO_FINAL_JOB_NAME = "Auto-deploy result"
RELEASE_FINAL_JOB_NAME = "Release finalizer"
RECEIPT_KIND = "platform-production-release"
RECEIPT_SCHEMA = 1
RECEIPT_MEMBER = "platform-release-receipt.json"
RECEIPT_ARTIFACT_PREFIX = "platform-production-release-receipt-"
RECEIPT_CONTENT_ARTIFACT_PREFIX = "platform-production-release-receipt-content-"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/@+:-]{1,512}$")
SAFE_TEXT_RE = re.compile(r"^[A-Za-z0-9._/@+:-]{1,256}$")
STATUS_URL_RE = re.compile(
    r"^https://github\.com/StrayForest/old_sparky/actions/runs/"
    r"[1-9][0-9]{0,31}/attempts/[1-9][0-9]{0,31}$"
)


class ReceiptError(ValueError):
    """Raised when a release receipt or artifact envelope is not closed."""


def _fail(message: str = "release receipt is invalid") -> ReceiptError:
    return ReceiptError(message)


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise _fail() from exc


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the only byte representation accepted for a receipt."""

    return _canonical_bytes(payload)


def _string(value: object, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise _fail(f"{field} is malformed")
    return value


def _run_id(value: object, field: str) -> str:
    return _string(value, RUN_ID_RE, field)


def _sha(value: object, field: str) -> str:
    return _string(value, SHA_RE, field)


def _digest(value: object, field: str) -> str:
    return _string(value, DIGEST_RE, field)


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(f"{field} is malformed")
    return value


def _exact_keys(value: Mapping[str, Any], keys: set[str], field: str) -> None:
    if set(value) != keys:
        raise _fail(f"{field} key set is not closed")


def _identity(
    value: object,
    *,
    field: str,
    expected_event: str,
    expected_name: str,
    expected_path: str,
) -> Mapping[str, Any]:
    row = _mapping(value, field)
    _exact_keys(
        row,
        {
            "event",
            "workflow_name",
            "workflow_path",
            "workflow_ref",
            "workflow_sha",
            "repository",
            "run_id",
            "run_attempt",
        },
        field,
    )
    if row.get("event") != expected_event:
        raise _fail(f"{field} event is not canonical")
    if row.get("workflow_name") != expected_name or row.get("workflow_path") != expected_path:
        raise _fail(f"{field} workflow identity is not canonical")
    if row.get("repository") != REPOSITORY_FULL_NAME:
        raise _fail(f"{field} repository is not canonical")
    _string(row.get("workflow_ref"), REF_RE, f"{field} workflow ref")
    _sha(row.get("workflow_sha"), f"{field} workflow SHA")
    _run_id(row.get("run_id"), f"{field} run id")
    _run_id(row.get("run_attempt"), f"{field} run attempt")
    return row


def _job_result(value: object, *, field: str, expected_name: str) -> Mapping[str, Any]:
    row = _mapping(value, field)
    _exact_keys(row, {"name", "status", "conclusion"}, field)
    if row.get("name") != expected_name:
        raise _fail(f"{field} job name is not canonical")
    if row.get("status") not in {"completed", "in_progress", "queued"}:
        raise _fail(f"{field} job status is malformed")
    conclusion = row.get("conclusion")
    if conclusion is not None and conclusion not in {
        "success",
        "failure",
        "cancelled",
        "skipped",
        "neutral",
        "timed_out",
        "action_required",
        "startup_failure",
        "stale",
    }:
        raise _fail(f"{field} job conclusion is malformed")
    return row


def _artifact(value: object) -> Mapping[str, Any]:
    row = _mapping(value, "artifact")
    _exact_keys(
        row,
        {
            "id",
            "name",
            "size_bytes",
            "digest",
            "member",
            "content_sha256",
            "workflow_run_id",
            "workflow_run_attempt",
        },
        "artifact",
    )
    if not isinstance(row.get("id"), int) or isinstance(row.get("id"), bool) or row["id"] <= 0:
        raise _fail("artifact id is malformed")
    name = _string(row.get("name"), SAFE_TEXT_RE, "artifact name")
    if not name.startswith(RECEIPT_CONTENT_ARTIFACT_PREFIX):
        raise _fail("artifact name is not the receipt content artifact")
    if not isinstance(row.get("size_bytes"), int) or isinstance(row.get("size_bytes"), bool) or not 0 < row["size_bytes"] <= 8 * 1024 * 1024:
        raise _fail("artifact size is malformed")
    digest = row.get("digest")
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise _fail("artifact digest is malformed")
    if row.get("member") != RECEIPT_MEMBER:
        raise _fail("artifact member is not canonical")
    _digest(row.get("content_sha256"), "artifact content digest")
    _run_id(row.get("workflow_run_id"), "artifact workflow run id")
    _run_id(row.get("workflow_run_attempt"), "artifact workflow run attempt")
    return row


def _normalised_digest(payload: Mapping[str, Any]) -> str:
    copy_payload = json.loads(json.dumps(payload))
    artifact = _mapping(copy_payload.get("artifact"), "artifact")
    artifact["content_sha256"] = ""
    artifact["size_bytes"] = 0
    return hashlib.sha256(_canonical_bytes(copy_payload)).hexdigest()


def validate_receipt(
    payload: object,
    *,
    expected_target_sha: str | None = None,
    expected_mode: str | None = None,
    expected_status_url: str | None = None,
) -> Mapping[str, Any]:
    """Validate a closed receipt and return it without normalising values."""

    receipt = _mapping(payload, "receipt")
    _exact_keys(
        receipt,
        {
            "schema",
            "kind",
            "target_sha",
            "mode",
            "runtime_profile",
            "web_compression",
            "security",
            "classifier",
            "caller",
            "called",
            "jobs",
            "status",
            "status_url",
            "artifact",
        },
        "receipt",
    )
    if receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("kind") != RECEIPT_KIND:
        raise _fail("receipt schema is not canonical")
    target_sha = _sha(receipt.get("target_sha"), "target SHA")
    if expected_target_sha is not None and target_sha != expected_target_sha:
        raise _fail("receipt target SHA does not match the expected target")
    mode = receipt.get("mode")
    if mode != "deploy" or (expected_mode is not None and mode != expected_mode):
        raise _fail("receipt mode is not deploy")
    _string(receipt.get("runtime_profile"), SAFE_TEXT_RE, "runtime profile")
    if receipt.get("web_compression") not in {"enabled", "disabled"}:
        raise _fail("receipt compression mode is malformed")
    caller = _identity(
        receipt.get("caller"),
        field="caller",
        expected_event=(
            "workflow_run"
            if isinstance(receipt.get("caller"), Mapping)
            and receipt["caller"].get("event") == "workflow_run"
            else "workflow_dispatch"
        ),
        expected_name=(
            AUTO_WORKFLOW_NAME
            if isinstance(receipt.get("caller"), Mapping)
            and receipt["caller"].get("event") == "workflow_run"
            else DEPLOY_WORKFLOW_NAME
        ),
        expected_path=(
            AUTO_WORKFLOW_PATH
            if isinstance(receipt.get("caller"), Mapping)
            and receipt["caller"].get("event") == "workflow_run"
            else DEPLOY_WORKFLOW_PATH
        ),
    )
    expected_caller_ref = f"{REPOSITORY_FULL_NAME}/{caller['workflow_path']}@refs/heads/dev"
    if caller.get("workflow_ref") != expected_caller_ref:
        raise _fail("caller workflow ref is not the canonical dev ref")
    caller_event = caller.get("event")
    if caller_event == "workflow_run":
        called_event = "workflow_run"
        called_name = DEPLOY_WORKFLOW_NAME
        called_path = DEPLOY_WORKFLOW_PATH
    elif caller_event == "workflow_dispatch":
        called_event = "workflow_dispatch"
        called_name = DEPLOY_WORKFLOW_NAME
        called_path = DEPLOY_WORKFLOW_PATH
    else:
        raise _fail("receipt caller event is not canonical")
    called = _identity(
        receipt.get("called"),
        field="called",
        expected_event=called_event,
        expected_name=called_name,
        expected_path=called_path,
    )
    expected_called_ref = f"{REPOSITORY_FULL_NAME}/{DEPLOY_WORKFLOW_PATH}@refs/heads/dev"
    if called.get("workflow_ref") != expected_called_ref:
        raise _fail("called workflow ref is not the canonical dev ref")
    if caller_event == "workflow_run" and receipt.get("mode") != "deploy":
        raise _fail("auto receipt mode is not deploy")
    if caller_event == "workflow_dispatch" and receipt.get("mode") != "deploy":
        raise _fail("manual receipt mode is not deploy")
    _identity(
        receipt.get("security"),
        field="security",
        expected_event="push",
        expected_name="Platform security and build",
        expected_path=".github/workflows/platform-security.yml",
    )
    _identity(
        receipt.get("classifier"),
        field="classifier",
        expected_event="push",
        expected_name="Platform security and build",
        expected_path=".github/workflows/platform-security.yml",
    )
    if caller.get("run_id") != called.get("run_id") or caller.get("run_attempt") != called.get("run_attempt"):
        raise _fail("caller and called run identities are not top-level bound")
    jobs = _mapping(receipt.get("jobs"), "jobs")
    _exact_keys(jobs, {"deploy", "final"}, "jobs")
    deploy_job = _job_result(jobs.get("deploy"), field="deploy job", expected_name=DEPLOY_JOB_NAME)
    final_job = _job_result(jobs.get("final"), field="final job", expected_name=RELEASE_FINAL_JOB_NAME)
    if deploy_job.get("status") != "completed" or deploy_job.get("conclusion") != "success":
        raise _fail("receipt deploy job is not successful")
    if final_job.get("status") != "completed" or final_job.get("conclusion") != "success":
        raise _fail("receipt final barrier is not successful")
    status = _mapping(receipt.get("status"), "status")
    _exact_keys(status, {"context", "state", "description", "target_url"}, "status")
    if status.get("context") != "platform-production-deploy" or status.get("state") != "success" or status.get("description") != "Production deployment and live smoke passed":
        raise _fail("receipt status marker is not successful")
    status_url = _string(receipt.get("status_url"), STATUS_URL_RE, "status URL")
    if status.get("target_url") != status_url:
        raise _fail("receipt status URL is not self-consistent")
    if expected_status_url is not None and status_url != expected_status_url:
        raise _fail("receipt status URL does not match the expected top-level run")
    artifact = _artifact(receipt.get("artifact"))
    if artifact.get("workflow_run_id") != caller.get("run_id") or artifact.get("workflow_run_attempt") != caller.get("run_attempt"):
        raise _fail("receipt artifact is not bound to the caller run")
    # ``content_sha256`` binds the companion content artifact, not the outer
    # closed envelope.  The content member is checked independently after the
    # API metadata and ZIP digest have been validated.
    return receipt


def write_receipt(path: Path, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate and atomically write one mode-600 canonical receipt."""

    receipt = validate_receipt(payload)
    data = _canonical_bytes(receipt)
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink() or path.is_symlink():
        raise _fail("receipt output path is unsafe")
    temporary.write_bytes(data)
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)
    return receipt


def read_receipt(path: Path) -> Mapping[str, Any]:
    """Read one canonical mode-600 receipt from disk."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise _fail() from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_mode & 0o777 != 0o600:
        raise _fail("receipt file metadata is unsafe")
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _fail() from exc
    if not isinstance(payload, Mapping) or _canonical_bytes(payload) != path.read_bytes():
        raise _fail("receipt is not canonical JSON")
    return validate_receipt(payload)


def validate_artifact_metadata(
    payload: object,
    *,
    expected_id: int,
    expected_name: str,
    expected_run_id: str,
    expected_run_attempt: str,
    expected_digest: str,
) -> Mapping[str, Any]:
    """Validate the API metadata for the receipt content artifact."""

    metadata = _mapping(payload, "artifact metadata")
    workflow_run = _mapping(metadata.get("workflow_run"), "artifact workflow run")
    if (
        type(metadata.get("id")) is not int
        or metadata.get("id") != expected_id
        or metadata.get("name") != expected_name
        or metadata.get("expired") is not False
        or type(metadata.get("size_in_bytes")) is not int
        or metadata.get("size_in_bytes") <= 0
        or metadata.get("digest") != expected_digest
        or type(workflow_run.get("id")) is not int
        or str(workflow_run.get("id")) != expected_run_id
        or workflow_run.get("run_attempt") not in (None, int(expected_run_attempt))
        or workflow_run.get("head_sha") is not None and (not isinstance(workflow_run.get("head_sha"), str) or SHA_RE.fullmatch(workflow_run["head_sha"]) is None)
    ):
        raise _fail("receipt artifact metadata is not exact")
    if not isinstance(expected_digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest) is None:
        raise _fail("receipt artifact digest is malformed")
    return metadata


def validate_single_member_archive(
    archive_path: Path,
    *,
    expected_member: str = RECEIPT_MEMBER,
    expected_content_sha256: str,
) -> Mapping[str, Any]:
    """Validate an artifact ZIP has exactly one safe canonical receipt member."""

    if not isinstance(expected_content_sha256, str) or DIGEST_RE.fullmatch(expected_content_sha256) is None:
        raise _fail("expected receipt content digest is malformed")
    payload, data = inspect_single_member_archive(
        archive_path,
        expected_member=expected_member,
    )
    if hashlib.sha256(data).hexdigest() != expected_content_sha256:
        raise _fail("receipt artifact member digest does not match")
    return validate_receipt(payload)


def inspect_single_member_archive(
    archive_path: Path,
    *,
    expected_member: str = RECEIPT_MEMBER,
) -> tuple[Mapping[str, Any], bytes]:
    """Read one exact canonical JSON member before checking its outer digest.

    Consumers use this two-stage form when the expected digest is itself
    closed by the receipt envelope.  The ZIP shape and canonical payload are
    still checked before the caller accepts the receipt's declared digest.
    """

    try:
        with zipfile.ZipFile(archive_path, "r", allowZip64=False) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != expected_member:
                raise _fail("receipt artifact member set is not exact")
            info = members[0]
            mode = (info.external_attr >> 16) & 0o170000
            if info.is_dir() or mode not in {0, stat.S_IFREG} or info.file_size <= 0 or info.file_size > 8 * 1024 * 1024:
                raise _fail("receipt artifact member metadata is unsafe")
            data = archive.read(info)
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
        if isinstance(exc, ReceiptError):
            raise
        raise _fail() from exc
    try:
        payload = json.loads(data.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _fail() from exc
    if not isinstance(payload, Mapping) or _canonical_bytes(payload) != data:
        raise _fail("receipt artifact member is not canonical JSON")
    return validate_receipt(payload), data


def validate_closed_receipt_archive(
    archive_path: Path,
    metadata: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_run_attempt: str,
    expected_status_url: str,
    expected_mode: str,
) -> Mapping[str, Any]:
    """Validate the exact GitHub artifact envelope and closed receipt.

    The API artifact digest is checked against the downloaded ZIP bytes before
    the ZIP is opened.  The ZIP must contain one canonical receipt member;
    receipt identity then binds it to the source run attempt and marker URL.
    """

    metadata = _mapping(metadata, "receipt artifact metadata")
    digest = metadata.get("digest")
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise _fail("receipt artifact digest is malformed")
    archive_bytes = archive_path.read_bytes()
    if type(metadata.get("size_in_bytes")) is not int or metadata["size_in_bytes"] != len(archive_bytes):
        raise _fail("receipt artifact size does not match download")
    if hashlib.sha256(archive_bytes).hexdigest() != digest.removeprefix("sha256:"):
        raise _fail("receipt artifact ZIP digest does not match API metadata")
    receipt, _ = inspect_single_member_archive(archive_path)
    validate_receipt(receipt, expected_mode=expected_mode, expected_status_url=expected_status_url)
    if (
        receipt["caller"]["run_id"] != expected_run_id
        or receipt["caller"]["run_attempt"] != expected_run_attempt
        or receipt["called"]["run_id"] != expected_run_id
        or receipt["called"]["run_attempt"] != expected_run_attempt
    ):
        raise _fail("receipt is not bound to the exact source attempt")
    artifact = receipt["artifact"]
    if artifact["workflow_run_id"] != expected_run_id or artifact["workflow_run_attempt"] != expected_run_attempt:
        raise _fail("receipt content artifact is not bound to the exact source attempt")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a platform release receipt")
    sub = parser.add_subparsers(dest="command", required=True)
    read = sub.add_parser("validate")
    read.add_argument("path", type=Path)
    archive = sub.add_parser("validate-archive")
    archive.add_argument("path", type=Path)
    archive.add_argument("--expected-content-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "validate":
            read_receipt(args.path)
            print("release receipt accepted")
            return 0
        if args.command == "validate-archive":
            if args.expected_content_sha256:
                receipt = validate_single_member_archive(
                    args.path,
                    expected_content_sha256=args.expected_content_sha256,
                )
            else:
                receipt, _ = inspect_single_member_archive(args.path)
            print(json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
            return 0
        raise _fail()
    except (ReceiptError, OSError, UnicodeError, ValueError, TypeError, KeyError):
        print("release receipt validation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
