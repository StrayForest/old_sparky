#!/usr/bin/env python3
"""Validate and record a non-deployable host-tools PR candidate.

This module is the trusted, stdlib-only side of the ``workflow_run`` handoff.
The workflow checks out this file from the default branch and passes a PR
checkout to it only as data.  No function in this module imports, compiles or
executes anything below the candidate checkout; the candidate root is used
only for ``git`` metadata, the pin JSON and the fixed host-tools members read
by :mod:`platform_host_tools_bundle`.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import zipfile
from typing import Mapping, Sequence

try:
    from . import platform_host_tools_bundle as bundle
except ImportError:  # pragma: no cover - direct runner invocation
    import platform_host_tools_bundle as bundle  # type: ignore[no-redef]


SCHEMA = 1
REPOSITORY = "StrayForest/old_sparky"
DEFAULT_BRANCH = "dev"
SECURITY_WORKFLOW_ID = 339062797
SECURITY_WORKFLOW_NAME = "Platform security and build"
SECURITY_WORKFLOW_PATH = ".github/workflows/platform-security.yml"
SECURITY_EVENT = "pull_request"
SUMMARY_ARTIFACT_PREFIX = "platform-ci-summary-"
CANDIDATE_ARTIFACT_PREFIX = "platform-host-tools-candidate-"
EVIDENCE_ARTIFACT_PREFIX = "platform-host-tools-candidate-evidence-"
MAX_JSON_BYTES = 1024 * 1024
MAX_SUMMARY_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_SUMMARY_BYTES = 512 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_BYTES = 512 * 1024
# The inner bundle and the manifest's max_bundle_bytes contract share one
# owner.  The candidate validator must not widen that existing bound while
# checking the upload-artifact envelope.
MAX_BUNDLE_BYTES = bundle.MAX_BUNDLE_BYTES
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
ARTIFACT_NAME_RE = re.compile(
    r"^platform-host-tools-candidate-(?:evidence-)?"
    r"pr[1-9][0-9]{0,8}-c[0-9a-f]{40}-e[0-9a-f]{40}-"
    r"run[1-9][0-9]{0,31}-attempt[1-9][0-9]{0,31}$"
)

FULL_GATE_IDS: tuple[str, ...] = (
    "backend",
    "python-quality",
    "security",
    "migration",
    "docs",
    "web-quality",
    "web-hermetic",
    "verification-contract",
)

# Jobs are an allowlist, not a status context.  Names without an explicit
# ``name:`` in platform-security.yml are represented by their job id.
EXPECTED_JOB_NAMES = frozenset(
    {
        "CI route classifier",
        "status-start",
        "Backend DB-free contours",
        "Backend PostgreSQL and Redis integration",
        "Backend privileged ephemeral contour",
        "Backend aggregate",
        "Python quality",
        "Security gates",
        "web-quality",
        "Documentation consistency",
        "Migration scenarios",
        "Web hermetic",
        "Verification contract",
        "Conditional release runtime fixture",
        "Trusted dev immutable release runtime",
        "status-final",
    }
)
REQUIRED_SUCCESS_JOB_NAMES = frozenset(
    {
        "CI route classifier",
        "Backend DB-free contours",
        "Backend PostgreSQL and Redis integration",
        "Backend privileged ephemeral contour",
        "Backend aggregate",
        "Python quality",
        "Security gates",
        "web-quality",
        "Documentation consistency",
        "Migration scenarios",
        "Web hermetic",
        "Verification contract",
        "status-final",
    }
)
CONDITIONAL_JOB_NAMES = frozenset(
    {"status-start", "Conditional release runtime fixture", "Trusted dev immutable release runtime"}
)


class CandidateError(ValueError):
    """Bounded candidate provenance or evidence failure."""


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CandidateError("candidate JSON contains duplicate keys")
        result[key] = value
    return result


def _read_bytes(path: Path, *, maximum: int, description: str) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise CandidateError(f"{description} path is invalid")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CandidateError(f"{description} is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > maximum
    ):
        raise CandidateError(f"{description} metadata is unsafe")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size > maximum
        ):
            raise CandidateError(f"{description} changed while opening")
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(descriptor, maximum + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_nlink != 1
            or not stat.S_ISREG(after.st_mode)
            or after.st_size != opened.st_size
            or len(data) != after.st_size
            or len(data) > maximum
        ):
            raise CandidateError(f"{description} changed while reading")
        return bytes(data)
    except CandidateError:
        raise
    except OSError as exc:
        raise CandidateError(f"{description} cannot be read") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_json(path: Path, *, description: str, maximum: int = MAX_JSON_BYTES) -> object:
    raw = _read_bytes(path, maximum=maximum, description=description)
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except CandidateError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise CandidateError(f"{description} is invalid") from exc


def _object(payload: object, description: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise CandidateError(f"{description} is not an object")
    return payload


def _text(value: object, description: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise CandidateError(f"{description} is invalid")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise CandidateError(f"{description} is invalid")
    return value


def _id(value: object, description: str) -> str:
    if type(value) is int:
        value = str(value)
    return _text(value, description, ID_RE)


def _sha(value: object, description: str) -> str:
    return _text(value, description, SHA_RE)


def _bool(value: object, description: str) -> bool:
    if type(value) is not bool:
        raise CandidateError(f"{description} is invalid")
    return value


def _safe_branch(value: object, description: str = "head ref") -> str:
    branch = _text(value, description, BRANCH_RE)
    if (
        branch.startswith(("refs/", "refs-", "tags/", "pull/"))
        or branch.endswith(("/", ".", ".lock"))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
        or branch.lower() in {"merge", "synthetic-merge"}
    ):
        raise CandidateError(f"{description} is not a branch ref")
    return branch


def _repository(value: object, description: str = "repository") -> str:
    return _text(value, description, re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"))


def _canonical_sha256(value: object, description: str) -> str:
    digest = _text(value, description)
    if digest.startswith("sha256:"):
        if DIGEST_RE.fullmatch(digest) is None:
            raise CandidateError(f"{description} is invalid")
        return digest
    if SHA256_RE.fullmatch(digest) is None:
        raise CandidateError(f"{description} is invalid")
    return f"sha256:{digest}"


def _safe_root(path: Path, description: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise CandidateError(f"{description} root is invalid")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CandidateError(f"{description} root is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CandidateError(f"{description} root is unsafe")
    return path


def _git(root: Path, *args: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=check,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CandidateError("candidate git metadata is unavailable") from exc
    if not check:
        return str(result.returncode)
    return result.stdout.strip()


def _strict_ancestor(root: Path, ancestor: str, descendant: str, description: str) -> None:
    if ancestor == descendant or _git(root, "merge-base", "--is-ancestor", ancestor, descendant, check=False) != "0":
        raise CandidateError(f"{description} is not a strict ancestor")


@dataclass(frozen=True, slots=True)
class RunContext:
    repository: str
    workflow_id: int
    workflow_name: str
    workflow_path: str
    run_id: str
    run_attempt: str
    candidate_sha: str
    head_ref: str
    base_sha: str
    pull_request: str

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "repository": self.repository,
            "security_workflow": {
                "id": self.workflow_id,
                "name": self.workflow_name,
                "path": self.workflow_path,
            },
            "security_run": {
                "id": int(self.run_id),
                "attempt": int(self.run_attempt),
                "event": SECURITY_EVENT,
                "head_sha": self.candidate_sha,
                "head_ref": self.head_ref,
            },
            "pull_request": {
                "number": int(self.pull_request),
                "base_sha": self.base_sha,
            },
        }


def _workflow_run_fields(run: Mapping[str, object], *, expected: RunContext) -> None:
    repository = _object(run.get("repository"), "security run repository")
    if _repository(repository.get("full_name"), "security run repository") != REPOSITORY:
        raise CandidateError("security run repository is not canonical")
    if type(run.get("workflow_id")) is not int or run.get("workflow_id") != SECURITY_WORKFLOW_ID:
        raise CandidateError("security run workflow id is not canonical")
    if run.get("name") != SECURITY_WORKFLOW_NAME or run.get("path") != SECURITY_WORKFLOW_PATH:
        raise CandidateError("security run workflow identity is not canonical")
    if run.get("id") != int(expected.run_id) or run.get("run_attempt") != int(expected.run_attempt):
        raise CandidateError("security run identity changed")
    if run.get("event") != SECURITY_EVENT or run.get("status") != "completed" or run.get("conclusion") != "success":
        raise CandidateError("security run is not completed successfully")
    if _sha(run.get("head_sha"), "security run head SHA") != expected.candidate_sha:
        raise CandidateError("security run head SHA changed")
    if _safe_branch(run.get("head_branch")) != expected.head_ref:
        raise CandidateError("security run head ref changed")
    head_repository = _object(run.get("head_repository"), "security run head repository")
    if _repository(head_repository.get("full_name"), "security run head repository") != REPOSITORY:
        raise CandidateError("security run head repository is not canonical")
    if _workflow_run_pull_request_number(run) != expected.pull_request:
        raise CandidateError("security run pull request identity changed")


def _pr_repository(value: object, description: str) -> None:
    repository = _object(value, description)
    if _repository(repository.get("full_name"), description) != REPOSITORY:
        raise CandidateError(f"{description} is not canonical")
    owner = repository.get("owner")
    if not isinstance(owner, Mapping) or owner.get("login") != "StrayForest":
        raise CandidateError(f"{description} owner is not canonical")


def _validate_pr(pr: Mapping[str, object], *, expected: RunContext) -> tuple[str, str]:
    if _id(pr.get("number"), "pull request number") != expected.pull_request:
        raise CandidateError("pull request number changed")
    if pr.get("state") != "open" or _bool(pr.get("draft"), "pull request draft"):
        raise CandidateError("pull request is not open and non-draft")
    base = _object(pr.get("base"), "pull request base")
    head = _object(pr.get("head"), "pull request head")
    _pr_repository(base.get("repo"), "pull request base repository")
    _pr_repository(head.get("repo"), "pull request head repository")
    if base.get("ref") != DEFAULT_BRANCH:
        raise CandidateError("pull request base is not dev")
    base_sha = _sha(base.get("sha"), "pull request base SHA")
    head_ref = _safe_branch(head.get("ref"))
    head_sha = _sha(head.get("sha"), "pull request head SHA")
    if head_sha != expected.candidate_sha or head_ref != expected.head_ref:
        raise CandidateError("pull request head does not match security run")
    if base_sha == head_sha:
        raise CandidateError("pull request head is not distinct from base")
    merge_sha = pr.get("merge_commit_sha")
    if merge_sha is not None and isinstance(merge_sha, str) and merge_sha == head_sha:
        raise CandidateError("synthetic merge SHA was presented as the PR head")
    label = head.get("label")
    if label is not None and label != f"StrayForest:{head_ref}":
        raise CandidateError("pull request head label is not canonical")
    return base_sha, head_ref


def _workflow_run_pull_request_number(payload: Mapping[str, object]) -> str:
    """Read the one PR association carried by the canonical run payload."""

    rows = payload.get("pull_requests")
    if not isinstance(rows, list) or len(rows) != 1:
        raise CandidateError("security run must have exactly one pull request")
    row = _object(rows[0], "security run pull request")
    return _id(row.get("number"), "security run pull request number")


def inspect_event(path: Path) -> RunContext:
    payload = _object(_read_json(path, description="workflow_run event"), "workflow_run event")
    workflow_run = _object(payload.get("workflow_run"), "workflow_run event payload")
    repository_payload = _object(workflow_run.get("repository"), "workflow_run repository")
    if _repository(repository_payload.get("full_name"), "workflow_run repository") != REPOSITORY:
        raise CandidateError("workflow_run repository is not canonical")
    if type(workflow_run.get("workflow_id")) is not int or workflow_run.get("workflow_id") != SECURITY_WORKFLOW_ID:
        raise CandidateError("workflow_run workflow id is not canonical")
    if workflow_run.get("name") != SECURITY_WORKFLOW_NAME or workflow_run.get("path") != SECURITY_WORKFLOW_PATH:
        raise CandidateError("workflow_run workflow identity is not canonical")
    if workflow_run.get("event") != SECURITY_EVENT or workflow_run.get("status") != "completed" or workflow_run.get("conclusion") != "success":
        raise CandidateError("workflow_run is not the successful pull_request security run")
    run_id = _id(workflow_run.get("id"), "workflow_run id")
    run_attempt = _id(workflow_run.get("run_attempt"), "workflow_run attempt")
    candidate_sha = _sha(workflow_run.get("head_sha"), "workflow_run head SHA")
    head_ref = _safe_branch(workflow_run.get("head_branch"))
    head_repository = _object(workflow_run.get("head_repository"), "workflow_run head repository")
    if _repository(head_repository.get("full_name"), "workflow_run head repository") != REPOSITORY:
        raise CandidateError("workflow_run head repository is not canonical")
    pull_request = _workflow_run_pull_request_number(workflow_run)
    return RunContext(
        repository=REPOSITORY,
        workflow_id=SECURITY_WORKFLOW_ID,
        workflow_name=SECURITY_WORKFLOW_NAME,
        workflow_path=SECURITY_WORKFLOW_PATH,
        run_id=run_id,
        run_attempt=run_attempt,
        candidate_sha=candidate_sha,
        head_ref=head_ref,
        base_sha="",
        pull_request=pull_request,
    )


def validate_context(
    event_path: Path,
    run_path: Path,
    pr_path: Path,
    *,
    output: Path,
    github_output: Path | None = None,
) -> RunContext:
    initial = inspect_event(event_path)
    run = _object(_read_json(run_path, description="security run"), "security run")
    _workflow_run_fields(run, expected=initial)
    pr = _object(_read_json(pr_path, description="pull request"), "pull request")
    base_sha, head_ref = _validate_pr(pr, expected=initial)
    context = RunContext(
        repository=initial.repository,
        workflow_id=initial.workflow_id,
        workflow_name=initial.workflow_name,
        workflow_path=initial.workflow_path,
        run_id=initial.run_id,
        run_attempt=initial.run_attempt,
        candidate_sha=initial.candidate_sha,
        head_ref=head_ref,
        base_sha=base_sha,
        pull_request=initial.pull_request,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(context.as_payload(), sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
    if github_output is not None:
        values = {
            "candidate_sha": context.candidate_sha,
            "head_ref": context.head_ref,
            "base_sha": context.base_sha,
            "pull_request": context.pull_request,
            "security_run_id": context.run_id,
            "security_run_attempt": context.run_attempt,
        }
        with github_output.open("a", encoding="ascii") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")
    return context


def load_context(path: Path) -> RunContext:
    payload = _object(_read_json(path, description="candidate context"), "candidate context")
    if set(payload) != {"schema", "repository", "security_workflow", "security_run", "pull_request"}:
        raise CandidateError("candidate context schema is not closed")
    workflow = _object(payload.get("security_workflow"), "candidate context workflow")
    run = _object(payload.get("security_run"), "candidate context run")
    pr = _object(payload.get("pull_request"), "candidate context pull request")
    if payload.get("schema") != SCHEMA or payload.get("repository") != REPOSITORY:
        raise CandidateError("candidate context identity is invalid")
    if workflow.get("id") != SECURITY_WORKFLOW_ID or workflow.get("name") != SECURITY_WORKFLOW_NAME or workflow.get("path") != SECURITY_WORKFLOW_PATH:
        raise CandidateError("candidate context workflow identity is invalid")
    context = RunContext(
        repository=REPOSITORY,
        workflow_id=SECURITY_WORKFLOW_ID,
        workflow_name=SECURITY_WORKFLOW_NAME,
        workflow_path=SECURITY_WORKFLOW_PATH,
        run_id=_id(run.get("id"), "candidate context run id"),
        run_attempt=_id(run.get("attempt"), "candidate context attempt"),
        candidate_sha=_sha(run.get("head_sha"), "candidate context head SHA"),
        head_ref=_safe_branch(run.get("head_ref")),
        base_sha=_sha(pr.get("base_sha"), "candidate context base SHA"),
        pull_request=_id(pr.get("number"), "candidate context PR number"),
    )
    if run.get("event") != SECURITY_EVENT:
        raise CandidateError("candidate context event is invalid")
    return context


def _artifact_row(payload: object, *, name: str, run_id: str) -> Mapping[str, object]:
    root = _object(payload, "artifact list")
    rows = root.get("artifacts")
    if not isinstance(rows, list) or len(rows) > 100:
        raise CandidateError("artifact list is invalid")
    matches: list[Mapping[str, object]] = []
    for row in rows:
        item = _object(row, "artifact row")
        workflow_run = item.get("workflow_run")
        workflow_id = workflow_run.get("id") if isinstance(workflow_run, Mapping) else None
        if item.get("name") == name and type(workflow_id) is int and workflow_id == int(run_id):
            matches.append(item)
    if len(matches) != 1:
        raise CandidateError("artifact list does not contain one exact row")
    return matches[0]


def select_artifact_id(
    metadata_path: Path,
    *,
    name: str,
    run_id: str,
    run_attempt: str,
) -> str:
    expected_name = f"{SUMMARY_ARTIFACT_PREFIX}{_id(run_id, 'run id')}-{_id(run_attempt, 'run attempt')}"
    if name != expected_name:
        raise CandidateError("summary artifact name is invalid")
    row = _artifact_row(_read_json(metadata_path, description="artifact list"), name=name, run_id=run_id)
    artifact_id = _id(row.get("id"), "artifact id")
    if row.get("expired") is not False:
        raise CandidateError("artifact is expired")
    return artifact_id


def _verify_artifact_metadata(
    payload: Mapping[str, object],
    archive: Path,
    *,
    expected_id: str,
    expected_name: str,
    expected_run_id: str,
    expected_run_attempt: str,
    expected_head_sha: str,
    expected_head_ref: str,
) -> str:
    if _id(payload.get("id"), "artifact id") != expected_id or payload.get("name") != expected_name:
        raise CandidateError("artifact identity is invalid")
    if payload.get("expired") is not False:
        raise CandidateError("artifact is expired")
    size = payload.get("size_in_bytes")
    if type(size) is not int or size <= 0 or size > MAX_ARTIFACT_BYTES:
        raise CandidateError("artifact size is invalid")
    digest = _canonical_sha256(payload.get("digest"), "artifact digest")
    archive_data = _read_bytes(archive, maximum=MAX_ARTIFACT_BYTES, description="artifact archive")
    if len(archive_data) != size or hashlib.sha256(archive_data).hexdigest() != digest.removeprefix("sha256:"):
        raise CandidateError("artifact archive digest or size does not match metadata")
    workflow_run = _object(payload.get("workflow_run"), "artifact workflow binding")
    if type(workflow_run.get("id")) is not int or workflow_run.get("id") != int(expected_run_id):
        raise CandidateError("artifact workflow run identity is invalid")
    if (
        type(workflow_run.get("head_sha")) is not str
        or workflow_run.get("head_sha") != expected_head_sha
        or type(workflow_run.get("head_branch")) is not str
        or workflow_run.get("head_branch") != expected_head_ref
    ):
        raise CandidateError("artifact workflow head identity is invalid")
    if "run_attempt" in workflow_run and (
        type(workflow_run.get("run_attempt")) is not int
        or workflow_run.get("run_attempt") != int(expected_run_attempt)
    ):
        raise CandidateError("artifact workflow attempt identity is invalid")
    return digest


def _verify_closed_archive(
    archive_path: Path,
    *,
    expected_member: str,
    maximum_member_bytes: int,
    expected_member_digest: str | None = None,
) -> None:
    """Verify an upload-artifact ZIP has one bounded regular member."""

    archive_data = _read_bytes(
        archive_path,
        maximum=MAX_ARTIFACT_BYTES,
        description="artifact archive",
    )
    try:
        with zipfile.ZipFile(BytesIO(archive_data), "r", allowZip64=False) as opened:
            infos = opened.infolist()
            if len(infos) != 1:
                raise CandidateError("artifact archive member set is not closed")
            info = infos[0]
            name = info.filename
            mode = (info.external_attr >> 16) & 0o177777
            if (
                name != expected_member
                or not name
                or "/" in name
                or "\\" in name
                or name in {".", ".."}
                or info.is_dir()
                or stat.S_ISLNK(mode)
                or stat.S_ISDIR(mode)
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                or info.file_size > maximum_member_bytes
            ):
                raise CandidateError("artifact archive member is invalid")
            member = opened.read(info)
            if len(member) != info.file_size or len(member) > maximum_member_bytes:
                raise CandidateError("artifact archive member changed while reading")
    except CandidateError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise CandidateError("artifact archive is invalid") from exc
    if expected_member_digest is not None:
        if SHA256_RE.fullmatch(expected_member_digest) is None:
            raise CandidateError("artifact member digest is invalid")
        if hashlib.sha256(member).hexdigest() != expected_member_digest:
            raise CandidateError("artifact archive member digest is invalid")


def verify_summary_artifact(
    context_path: Path,
    metadata_path: Path,
    archive_path: Path,
    *,
    expected_artifact_id: str,
    output: Path | None = None,
    github_output: Path | None = None,
) -> dict[str, object]:
    context = load_context(context_path)
    metadata = _object(_read_json(metadata_path, description="summary artifact metadata"), "summary artifact metadata")
    artifact_id = _id(expected_artifact_id, "summary artifact id")
    digest = _verify_artifact_metadata(
        metadata,
        archive_path,
        expected_id=artifact_id,
        expected_name=f"{SUMMARY_ARTIFACT_PREFIX}{context.run_id}-{context.run_attempt}",
        expected_run_id=context.run_id,
        expected_run_attempt=context.run_attempt,
        expected_head_sha=context.candidate_sha,
        expected_head_ref=context.head_ref,
    )
    try:
        with zipfile.ZipFile(archive_path, "r", allowZip64=False) as opened:
            infos = opened.infolist()
            if len(infos) != 1:
                raise CandidateError("summary artifact member set is not closed")
            info = infos[0]
            if info.filename != "platform-security-summary.json" or info.is_dir() or "\\" in info.filename or info.file_size > MAX_SUMMARY_BYTES:
                raise CandidateError("summary artifact member is invalid")
            mode = (info.external_attr >> 16) & 0o177777
            if stat.S_ISLNK(mode) or stat.S_ISDIR(mode) or stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                raise CandidateError("summary artifact member type is invalid")
            summary = _object(json.loads(opened.read(info).decode("utf-8"), object_pairs_hook=_strict_object), "security summary")
    except CandidateError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise CandidateError("summary artifact is invalid") from exc
    _validate_summary(summary, context)
    result = {
        "schema": SCHEMA,
        "artifact_id": int(metadata["id"]),
        "artifact_name": metadata["name"],
        "artifact_size": metadata["size_in_bytes"],
        "artifact_digest": digest,
        "archive_sha256": hashlib.sha256(_read_bytes(archive_path, maximum=MAX_ARTIFACT_BYTES, description="summary archive")).hexdigest(),
        "summary": summary,
    }
    if output is not None:
        output.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
    if github_output is not None:
        with github_output.open("a", encoding="ascii") as stream:
            stream.write(f"summary_artifact_id={result['artifact_id']}\n")
            stream.write(f"summary_artifact_digest={result['artifact_digest']}\n")
            stream.write(f"summary_archive_sha256={result['archive_sha256']}\n")
    return result


def _validate_summary(summary: Mapping[str, object], context: RunContext) -> None:
    expected_keys = {
        "schema", "tested_sha", "event", "route_event", "class", "reason", "deployable", "fallback",
        "manifest_digest", "expected_gates", "gate_results", "conditional_gate_results", "runtime_sensitive",
        "requires_release_runtime", "requires_real_release_runtime", "missing_or_failed", "route_errors",
        "status_start_result", "passed",
    }
    if set(summary) != expected_keys:
        raise CandidateError("security final summary schema is not closed")
    if summary.get("schema") != 1 or summary.get("tested_sha") != context.candidate_sha:
        raise CandidateError("security final summary target is invalid")
    if summary.get("event") != SECURITY_EVENT or summary.get("route_event") != SECURITY_EVENT:
        raise CandidateError("security final summary event is invalid")
    if summary.get("class") != "full" or summary.get("deployable") is not False:
        raise CandidateError("security final summary is not a non-deployable full route")
    if not isinstance(summary.get("reason"), str) or not summary.get("reason"):
        raise CandidateError("security final summary reason is invalid")
    if type(summary.get("fallback")) is not bool or type(summary.get("runtime_sensitive")) is not bool:
        raise CandidateError("security final summary route flags are invalid")
    if not isinstance(summary.get("manifest_digest"), str) or SHA256_RE.fullmatch(summary["manifest_digest"]) is None:
        raise CandidateError("security final summary manifest digest is invalid")
    if summary.get("expected_gates") != list(FULL_GATE_IDS):
        raise CandidateError("security final summary gate set is invalid")
    gate_results = summary.get("gate_results")
    if not isinstance(gate_results, Mapping) or set(gate_results) != set(FULL_GATE_IDS):
        raise CandidateError("security final summary gate results are invalid")
    if any(gate_results[gate] != "success" for gate in FULL_GATE_IDS):
        raise CandidateError("security final summary contains an unsuccessful gate")
    conditional = summary.get("conditional_gate_results")
    if not isinstance(conditional, Mapping) or set(conditional) != {"release-runtime", "release-runtime-real"}:
        raise CandidateError("security final summary conditional results are invalid")
    if conditional.get("release-runtime") not in {"success", "skipped"} or conditional.get("release-runtime-real") != "skipped":
        raise CandidateError("security final summary conditional route is invalid")
    if summary.get("requires_real_release_runtime") is not False:
        raise CandidateError("pull request summary requires a real release runtime")
    if summary.get("requires_release_runtime") is not (conditional.get("release-runtime") == "success"):
        raise CandidateError("security final summary runtime requirement is inconsistent")
    if summary.get("missing_or_failed") != [] or summary.get("route_errors") != [] or summary.get("passed") is not True:
        raise CandidateError("security final summary did not pass closed")
    if summary.get("status_start_result") != "skipped":
        raise CandidateError("pull request status-start must be skipped")


def verify_jobs(jobs_path: Path, context: RunContext, summary: Mapping[str, object]) -> None:
    payload = _object(_read_json(jobs_path, description="security jobs"), "security jobs")
    rows = payload.get("jobs")
    if not isinstance(rows, list) or len(rows) != len(EXPECTED_JOB_NAMES):
        raise CandidateError("security job set is not bounded")
    by_name: dict[str, Mapping[str, object]] = {}
    job_ids: set[int] = set()
    for row in rows:
        job = _object(row, "security job")
        name = _text(job.get("name"), "security job name")
        if name in by_name or name not in EXPECTED_JOB_NAMES:
            raise CandidateError("security job name is not allowlisted")
        job_id = job.get("id")
        if type(job_id) is not int or job_id <= 0 or job_id in job_ids:
            raise CandidateError("security job id set is invalid")
        if job.get("status") != "completed" or job.get("conclusion") not in {"success", "skipped"}:
            raise CandidateError("security job is not completed with an allowed conclusion")
        by_name[name] = job
        job_ids.add(job_id)
    if payload.get("total_count") != len(rows):
        raise CandidateError("security job total count is invalid")
    if set(by_name) != EXPECTED_JOB_NAMES:
        raise CandidateError("security job allowlist is incomplete")
    for name in REQUIRED_SUCCESS_JOB_NAMES:
        if by_name[name].get("conclusion") != "success":
            raise CandidateError("required security job did not succeed")
    for name in CONDITIONAL_JOB_NAMES:
        if by_name[name].get("conclusion") not in {"success", "skipped"}:
            raise CandidateError("conditional security job conclusion is invalid")
    if summary.get("requires_release_runtime") is True and by_name["Conditional release runtime fixture"].get("conclusion") != "success":
        raise CandidateError("summary required the release runtime fixture")
    if summary.get("requires_release_runtime") is False and by_name["Conditional release runtime fixture"].get("conclusion") != "skipped":
        raise CandidateError("release runtime fixture was unexpectedly run")


def verify_security_run(
    context_path: Path,
    jobs_path: Path,
    summary_metadata_path: Path,
    summary_archive_path: Path,
    *,
    expected_summary_artifact_id: str,
    output: Path,
    github_output: Path | None = None,
) -> dict[str, object]:
    context = load_context(context_path)
    summary_result = verify_summary_artifact(
        context_path,
        summary_metadata_path,
        summary_archive_path,
        expected_artifact_id=expected_summary_artifact_id,
    )
    summary = _object(summary_result["summary"], "security summary")
    verify_jobs(jobs_path, context, summary)
    result = {
        "schema": SCHEMA,
        "context": context.as_payload(),
        "summary_artifact": {key: summary_result[key] for key in ("artifact_id", "artifact_name", "artifact_size", "artifact_digest", "archive_sha256")},
        "summary": summary,
    }
    output.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
    if github_output is not None:
        with github_output.open("a", encoding="ascii") as stream:
            stream.write(f"summary_artifact_id={summary_result['artifact_id']}\n")
            stream.write(f"summary_artifact_digest={summary_result['artifact_digest']}\n")
            stream.write(f"summary_archive_sha256={summary_result['archive_sha256']}\n")
    return result


def verify_ancestry(source_root: Path, *, base_sha: str, host_tools_sha: str, candidate_sha: str) -> None:
    root = _safe_root(source_root, "candidate")
    base = _sha(base_sha, "base SHA")
    host = _sha(host_tools_sha, "host-tools SHA")
    candidate = _sha(candidate_sha, "candidate SHA")
    if _git(root, "rev-parse", "--verify", "HEAD^{commit}") != candidate:
        raise CandidateError("candidate checkout is not the exact PR head")
    if _git(root, "cat-file", "-t", base) != "commit" or _git(root, "cat-file", "-t", host) != "commit":
        raise CandidateError("ancestry commit object is unavailable")
    # The current PR base must be present in E.  This allows a branch to
    # merge-sync current dev before its host-tools pin C, while still binding
    # C to commits introduced by the PR rather than an older generation that
    # current dev already contains.
    _strict_ancestor(root, base, candidate, "base SHA")
    _strict_ancestor(root, host, candidate, "host-tools SHA")
    host_in_base = _git(root, "merge-base", "--is-ancestor", host, base, check=False)
    if host_in_base == "0":
        raise CandidateError("host-tools SHA is already reachable from the current base")
    if host_in_base != "1":
        raise CandidateError("host-tools/base reachability could not be determined")


def _pin_payload(candidate_root: Path) -> Mapping[str, object]:
    path = candidate_root / "platform/contracts/host_tools_pin.json"
    payload = _object(_read_json(path, description="candidate host-tools pin", maximum=16 * 1024), "candidate host-tools pin")
    if set(payload) != {"schema", "repository", "host_tools_sha", "closure"} or payload.get("schema") != 1 or payload.get("repository") != REPOSITORY:
        raise CandidateError("candidate host-tools pin schema is invalid")
    return payload


def _pin_closure(candidate_root: Path, payload: Mapping[str, object]) -> list[dict[str, object]]:
    closure = payload.get("closure")
    if not isinstance(closure, list) or len(closure) != len(bundle.HOST_TOOL_FILES):
        raise CandidateError("candidate host-tools pin closure is invalid")
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in closure:
        item = _object(row, "candidate pin closure row")
        if set(item) != {"mode", "path", "sha256"}:
            raise CandidateError("candidate pin closure row is not closed")
        path = _text(item.get("path"), "candidate pin closure path")
        if path in seen or path != f"platform/tools/{path.removeprefix('platform/tools/')}" or path.removeprefix("platform/tools/") not in bundle.HOST_TOOL_FILES:
            raise CandidateError("candidate pin closure path is not fixed")
        mode = item.get("mode")
        digest = _text(item.get("sha256"), "candidate pin closure digest")
        if type(mode) is not int or mode not in {0o644, 0o755} or SHA256_RE.fullmatch(digest) is None:
            raise CandidateError("candidate pin closure row is invalid")
        seen.add(path)
        records.append({"mode": mode, "path": path, "sha256": digest})
    if tuple(row["path"] for row in records) != tuple(f"platform/tools/{name}" for name in bundle.HOST_TOOL_FILES):
        raise CandidateError("candidate pin closure ordering is not fixed")
    return records


def _source_closure(candidate_root: Path, records: Sequence[Mapping[str, object]]) -> None:
    for row in records:
        path = candidate_root / str(row["path"])
        data = _read_bytes(path, maximum=bundle.MAX_FILE_BYTES, description="candidate host-tools member")
        metadata = path.lstat()
        if stat.S_IMODE(metadata.st_mode) != row["mode"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise CandidateError("candidate closure changed during handoff")


def _artifact_name(*, prefix: str, pull_request: str, host_tools_sha: str, candidate_sha: str, run_id: str, run_attempt: str) -> str:
    if prefix not in {CANDIDATE_ARTIFACT_PREFIX, EVIDENCE_ARTIFACT_PREFIX}:
        raise CandidateError("artifact prefix is invalid")
    name = f"{prefix}pr{pull_request}-c{host_tools_sha}-e{candidate_sha}-run{run_id}-attempt{run_attempt}"
    if ARTIFACT_NAME_RE.fullmatch(name) is None:
        raise CandidateError("candidate artifact name is invalid")
    return name


def write_evidence(
    *,
    trusted_root: Path,
    candidate_root: Path,
    context_path: Path,
    security_evidence_path: Path,
    bundle_path: Path,
    artifact_metadata_path: Path,
    artifact_archive_path: Path,
    candidate_artifact_id: str,
    host_tools_sha: str,
    trusted_sha: str,
    packaging_run_id: str,
    packaging_run_attempt: str,
    output: Path,
) -> dict[str, object]:
    context = load_context(context_path)
    trusted = _safe_root(trusted_root, "trusted")
    candidate = _safe_root(candidate_root, "candidate")
    trusted_commit = _sha(trusted_sha, "trusted source SHA")
    if _git(trusted, "rev-parse", "--verify", "HEAD^{commit}") != trusted_commit:
        raise CandidateError("trusted checkout is not the requested default-branch commit")
    verify_ancestry(candidate, base_sha=context.base_sha, host_tools_sha=host_tools_sha, candidate_sha=context.candidate_sha)
    host = _sha(host_tools_sha, "host-tools SHA")
    pin = _pin_payload(candidate)
    if pin.get("host_tools_sha") != host:
        raise CandidateError("candidate pin does not resolve to the selected host-tools SHA")
    closure = _pin_closure(candidate, pin)
    _source_closure(candidate, closure)
    bundle_summary = bundle.verify_bundle(bundle_path, expected_source_sha=host)
    manifest = _object(bundle_summary.get("manifest"), "trusted bundle manifest")
    manifest_records = manifest.get("files")
    if not isinstance(manifest_records, list):
        raise CandidateError("trusted bundle manifest records are invalid")
    source_by_name = {str(row["path"]).removeprefix("platform/tools/"): row for row in closure}
    bundle_records: list[dict[str, object]] = []
    for row in manifest_records:
        record = _object(row, "trusted bundle record")
        path = _text(record.get("path"), "trusted bundle path")
        digest = _text(record.get("sha256"), "trusted bundle digest")
        mode = record.get("mode")
        if path == "capabilities.txt":
            if mode != bundle.DATA_MODE:
                raise CandidateError("trusted bundle capabilities mode is invalid")
        elif path not in source_by_name or digest != source_by_name[path]["sha256"] or mode != bundle.EXECUTABLE_MODE:
            raise CandidateError("trusted bundle inventory does not match the pin")
        bundle_records.append({"mode": mode, "path": path, "sha256": digest})
    if [row["path"] for row in bundle_records] != sorted(row["path"] for row in bundle_records):
        raise CandidateError("trusted bundle inventory ordering is invalid")
    artifact_name = _artifact_name(
        prefix=CANDIDATE_ARTIFACT_PREFIX,
        pull_request=context.pull_request,
        host_tools_sha=host,
        candidate_sha=context.candidate_sha,
        run_id=context.run_id,
        run_attempt=context.run_attempt,
    )
    metadata = _object(_read_json(artifact_metadata_path, description="candidate artifact metadata"), "candidate artifact metadata")
    candidate_artifact_id = _id(candidate_artifact_id, "candidate artifact id")
    outer_digest = _verify_artifact_metadata(
        metadata,
        artifact_archive_path,
        expected_id=candidate_artifact_id,
        expected_name=artifact_name,
        expected_run_id=packaging_run_id,
        expected_run_attempt=packaging_run_attempt,
        expected_head_sha=trusted_commit,
        expected_head_ref=DEFAULT_BRANCH,
    )
    _verify_closed_archive(
        artifact_archive_path,
        expected_member="platform-host-tools-bundle.zip",
        maximum_member_bytes=MAX_BUNDLE_BYTES,
        expected_member_digest=bundle_summary["bundle_sha256"],
    )
    security = _object(_read_json(security_evidence_path, description="security evidence"), "security evidence")
    if set(security) != {"schema", "context", "summary_artifact", "summary"}:
        raise CandidateError("security evidence schema is not closed")
    if security.get("schema") != SCHEMA or security.get("context") != context.as_payload():
        raise CandidateError("security evidence context changed")
    summary_artifact = _object(security.get("summary_artifact"), "summary artifact evidence")
    if set(summary_artifact) != {
        "artifact_id",
        "artifact_name",
        "artifact_size",
        "artifact_digest",
        "archive_sha256",
    }:
        raise CandidateError("summary artifact evidence schema is not closed")
    summary = _object(security.get("summary"), "security summary evidence")
    _validate_summary(summary, context)
    summary_artifact_id = summary_artifact.get("artifact_id")
    if type(summary_artifact_id) is not int or summary_artifact_id <= 0:
        raise CandidateError("summary artifact evidence id is invalid")
    if summary_artifact.get("artifact_name") != f"{SUMMARY_ARTIFACT_PREFIX}{context.run_id}-{context.run_attempt}":
        raise CandidateError("summary artifact evidence name is invalid")
    summary_size = summary_artifact.get("artifact_size")
    if type(summary_size) is not int or summary_size <= 0 or summary_size > MAX_ARTIFACT_BYTES:
        raise CandidateError("summary artifact evidence size is invalid")
    if _canonical_sha256(summary_artifact.get("artifact_digest"), "summary artifact evidence digest") != summary_artifact.get("artifact_digest"):
        raise CandidateError("summary artifact evidence digest is invalid")
    if not isinstance(summary_artifact.get("archive_sha256"), str) or SHA256_RE.fullmatch(summary_artifact["archive_sha256"]) is None:
        raise CandidateError("summary artifact evidence archive digest is invalid")
    inner_digest = f"sha256:{bundle_summary['bundle_sha256']}"
    evidence = {
        "schema": SCHEMA,
        "kind": "host_tools_candidate_evidence",
        "repository": REPOSITORY,
        "deployable": False,
        "trusted_source_sha": trusted_commit,
        "candidate_source_sha": context.candidate_sha,
        "host_tools_sha": host,
        "base_sha": context.base_sha,
        "pull_request": int(context.pull_request),
        "security_workflow": {
            "id": SECURITY_WORKFLOW_ID,
            "name": SECURITY_WORKFLOW_NAME,
            "path": SECURITY_WORKFLOW_PATH,
        },
        "security_run": {
            "id": int(context.run_id),
            "attempt": int(context.run_attempt),
            "event": SECURITY_EVENT,
            "head_sha": context.candidate_sha,
        },
        "security_summary": {
            "artifact_id": summary_artifact.get("artifact_id"),
            "artifact_name": summary_artifact.get("artifact_name"),
            "artifact_size": summary_artifact.get("artifact_size"),
            "artifact_digest": summary_artifact.get("artifact_digest"),
            "archive_sha256": summary_artifact.get("archive_sha256"),
        },
        "packaging_run": {
            "id": int(_id(packaging_run_id, "packaging run id")),
            "attempt": int(_id(packaging_run_attempt, "packaging run attempt")),
            "trusted_head_sha": trusted_commit,
        },
        "candidate_artifact": {
            "id": candidate_artifact_id,
            "name": artifact_name,
            "size": metadata.get("size_in_bytes"),
            "outer_digest": outer_digest,
            "inner_digest": inner_digest,
        },
        "host_tools": {
            "toolset_version": manifest.get("toolset_version"),
            "components": manifest.get("components"),
            "source_closure": closure,
            "bundle_inventory": bundle_records,
            "manifest_sha256": bundle_summary.get("manifest_sha256"),
        },
    }
    expected_keys = {
        "schema", "kind", "repository", "deployable", "trusted_source_sha", "candidate_source_sha", "host_tools_sha",
        "base_sha", "pull_request", "security_workflow", "security_run", "security_summary", "packaging_run",
        "candidate_artifact", "host_tools",
    }
    if set(evidence) != expected_keys:
        raise CandidateError("candidate evidence schema is not closed")
    encoded = (json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise CandidateError("candidate evidence exceeds its bound")
    output.write_bytes(encoded)
    os.chmod(output, 0o600)
    metadata = output.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size != len(encoded)
    ):
        raise CandidateError("candidate evidence output metadata is unsafe")
    return evidence


def verify_uploaded_evidence(
    metadata_path: Path,
    archive_path: Path,
    *,
    artifact_id: str,
    artifact_name: str,
    run_id: str,
    run_attempt: str,
    trusted_sha: str,
) -> str:
    """Verify the bounded evidence artifact envelope after upload."""

    metadata = _object(
        _read_json(metadata_path, description="evidence artifact metadata"),
        "evidence artifact metadata",
    )
    expected_id = _id(artifact_id, "evidence artifact id")
    if EVIDENCE_ARTIFACT_PREFIX not in artifact_name or ARTIFACT_NAME_RE.fullmatch(artifact_name) is None:
        raise CandidateError("evidence artifact name is invalid")
    digest = _verify_artifact_metadata(
        metadata,
        archive_path,
        expected_id=expected_id,
        expected_name=artifact_name,
        expected_run_id=run_id,
        expected_run_attempt=run_attempt,
        expected_head_sha=_sha(trusted_sha, "trusted source SHA"),
        expected_head_ref=DEFAULT_BRANCH,
    )
    _verify_closed_archive(
        archive_path,
        expected_member="platform-host-tools-candidate-evidence.json",
        maximum_member_bytes=MAX_EVIDENCE_BYTES,
    )
    return digest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect-event")
    inspect.add_argument("--event", required=True, type=Path)
    inspect.add_argument("--github-output", type=Path)
    context = sub.add_parser("validate-context")
    context.add_argument("--event", required=True, type=Path)
    context.add_argument("--run", required=True, type=Path)
    context.add_argument("--pr", required=True, type=Path)
    context.add_argument("--output", required=True, type=Path)
    context.add_argument("--github-output", type=Path)
    select = sub.add_parser("select-artifact-id")
    select.add_argument("--metadata", required=True, type=Path)
    select.add_argument("--name", required=True)
    select.add_argument("--run-id", required=True)
    select.add_argument("--run-attempt", required=True)
    summary = sub.add_parser("verify-summary")
    summary.add_argument("--context", required=True, type=Path)
    summary.add_argument("--metadata", required=True, type=Path)
    summary.add_argument("--archive", required=True, type=Path)
    summary.add_argument("--artifact-id", required=True)
    summary.add_argument("--output", required=True, type=Path)
    summary.add_argument("--github-output", type=Path)
    security = sub.add_parser("verify-security")
    security.add_argument("--context", required=True, type=Path)
    security.add_argument("--jobs", required=True, type=Path)
    security.add_argument("--summary-metadata", required=True, type=Path)
    security.add_argument("--summary-archive", required=True, type=Path)
    security.add_argument("--summary-artifact-id", required=True)
    security.add_argument("--output", required=True, type=Path)
    security.add_argument("--github-output", type=Path)
    ancestry = sub.add_parser("verify-ancestry")
    ancestry.add_argument("--source-root", required=True, type=Path)
    ancestry.add_argument("--base-sha", required=True)
    ancestry.add_argument("--host-tools-sha", required=True)
    ancestry.add_argument("--candidate-sha", required=True)
    evidence = sub.add_parser("write-evidence")
    evidence.add_argument("--trusted-root", required=True, type=Path)
    evidence.add_argument("--candidate-root", required=True, type=Path)
    evidence.add_argument("--context", required=True, type=Path)
    evidence.add_argument("--security-evidence", required=True, type=Path)
    evidence.add_argument("--bundle", required=True, type=Path)
    evidence.add_argument("--artifact-metadata", required=True, type=Path)
    evidence.add_argument("--artifact-archive", required=True, type=Path)
    evidence.add_argument("--candidate-artifact-id", required=True)
    evidence.add_argument("--host-tools-sha", required=True)
    evidence.add_argument("--trusted-sha", required=True)
    evidence.add_argument("--packaging-run-id", required=True)
    evidence.add_argument("--packaging-run-attempt", required=True)
    evidence.add_argument("--output", required=True, type=Path)
    uploaded = sub.add_parser("verify-evidence-upload")
    uploaded.add_argument("--metadata", required=True, type=Path)
    uploaded.add_argument("--archive", required=True, type=Path)
    uploaded.add_argument("--artifact-id", required=True)
    uploaded.add_argument("--artifact-name", required=True)
    uploaded.add_argument("--run-id", required=True)
    uploaded.add_argument("--run-attempt", required=True)
    uploaded.add_argument("--trusted-sha", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect-event":
            context = inspect_event(args.event)
            values = {
                "security_run_id": context.run_id,
                "security_run_attempt": context.run_attempt,
                "candidate_sha": context.candidate_sha,
                "head_ref": context.head_ref,
                "pull_request": context.pull_request,
            }
            if args.github_output is not None:
                with args.github_output.open("a", encoding="ascii") as stream:
                    for key, value in values.items():
                        stream.write(f"{key}={value}\n")
            else:
                print(json.dumps(context.as_payload(), sort_keys=True, separators=(",", ":")))
        elif args.command == "validate-context":
            validate_context(args.event, args.run, args.pr, output=args.output, github_output=args.github_output)
        elif args.command == "select-artifact-id":
            print(
                select_artifact_id(
                    args.metadata,
                    name=args.name,
                    run_id=args.run_id,
                    run_attempt=args.run_attempt,
                )
            )
        elif args.command == "verify-summary":
            verify_summary_artifact(
                args.context,
                args.metadata,
                args.archive,
                expected_artifact_id=args.artifact_id,
                output=args.output,
                github_output=args.github_output,
            )
        elif args.command == "verify-security":
            verify_security_run(
                args.context,
                args.jobs,
                args.summary_metadata,
                args.summary_archive,
                expected_summary_artifact_id=args.summary_artifact_id,
                output=args.output,
                github_output=args.github_output,
            )
        elif args.command == "verify-ancestry":
            verify_ancestry(args.source_root, base_sha=args.base_sha, host_tools_sha=args.host_tools_sha, candidate_sha=args.candidate_sha)
            print("HOST_TOOLS_CANDIDATE ancestry=verified")
        elif args.command == "write-evidence":
            write_evidence(
                trusted_root=args.trusted_root,
                candidate_root=args.candidate_root,
                context_path=args.context,
                security_evidence_path=args.security_evidence,
                bundle_path=args.bundle,
                artifact_metadata_path=args.artifact_metadata,
                artifact_archive_path=args.artifact_archive,
                candidate_artifact_id=args.candidate_artifact_id,
                host_tools_sha=args.host_tools_sha,
                trusted_sha=args.trusted_sha,
                packaging_run_id=args.packaging_run_id,
                packaging_run_attempt=args.packaging_run_attempt,
                output=args.output,
            )
            print("HOST_TOOLS_CANDIDATE evidence=written")
        elif args.command == "verify-evidence-upload":
            print(
                verify_uploaded_evidence(
                    args.metadata,
                    args.archive,
                    artifact_id=args.artifact_id,
                    artifact_name=args.artifact_name,
                    run_id=args.run_id,
                    run_attempt=args.run_attempt,
                    trusted_sha=args.trusted_sha,
                )
            )
        else:  # pragma: no cover - argparse enforces the subcommand set.
            raise CandidateError("candidate command is invalid")
        return 0
    except (CandidateError, OSError, ValueError, TypeError, zipfile.BadZipFile):
        print("host-tools candidate is invalid", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
