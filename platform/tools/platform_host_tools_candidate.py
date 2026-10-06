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
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from types import ModuleType
import zipfile
from typing import Mapping, Sequence


def _load_trusted_bundle() -> ModuleType:
    """Load the sibling bundle helper from this trusted tools directory only.

    The workflow deliberately invokes this file with ``python -I -B``.  In
    isolated mode Python does not add the script directory to ``sys.path``,
    so a normal relative/fallback import would fail.  Resolve the sibling
    from ``__file__`` instead and load it under a private module name.  The
    candidate checkout is never added to the import path and cannot satisfy
    this dependency.
    """

    script = Path(__file__)
    if script.is_symlink() or script.name != "platform_host_tools_candidate.py":
        raise ImportError("trusted candidate validator path is unsafe")
    try:
        trusted_tools = script.resolve(strict=True).parent
        if (
            trusted_tools.name != "tools"
            or trusted_tools.parent.name != "platform"
            or trusted_tools.parent.is_symlink()
            or trusted_tools.parent.parent.is_symlink()
        ):
            raise ImportError("trusted candidate validator directory is unsafe")
        bundle_path = trusted_tools / "platform_host_tools_bundle.py"
        metadata = bundle_path.lstat()
    except OSError as exc:
        raise ImportError("trusted bundle helper is unavailable") from exc
    if (
        bundle_path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ImportError("trusted bundle helper metadata is unsafe")
    spec = importlib.util.spec_from_file_location(
        "_platform_host_tools_bundle_trusted", bundle_path
    )
    loader = spec.loader if spec is not None else None
    if spec is None or loader is None or spec.origin is None:
        raise ImportError("trusted bundle helper loader is unavailable")
    try:
        if Path(spec.origin).resolve(strict=True) != bundle_path.resolve(strict=True):
            raise ImportError("trusted bundle helper origin changed")
    except OSError as exc:
        raise ImportError("trusted bundle helper origin is unavailable") from exc
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


bundle = _load_trusted_bundle()


# The context/evidence schema deliberately changed when the PR source head
# was separated from the synthetic merge commit that GitHub tests.  Do not
# accept the old one-field context: it was the source of the P1 confusion.
SCHEMA = 2
REPOSITORY = "StrayForest/old_sparky"
DEFAULT_BRANCH = "dev"
SECURITY_WORKFLOW_ID = 339062797
SECURITY_WORKFLOW_NAME = "Platform security and build"
SECURITY_WORKFLOW_PATH = ".github/workflows/platform-security.yml"
SECURITY_EVENT = "pull_request"
SUMMARY_ARTIFACT_PREFIX = "platform-ci-summary-"
CANDIDATE_ARTIFACT_PREFIX = "platform-host-tools-candidate-"
EVIDENCE_ARTIFACT_PREFIX = "platform-host-tools-candidate-evidence-"
ROUTE_ARTIFACT_PREFIX = "platform-ci-route-"
ROUTE_MANIFEST_MEMBER = "classifier-manifest.json"
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
ROUTE_ARTIFACT_NAME_RE = re.compile(
    r"^platform-ci-route-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}$"
)
CHECK_RUN_URL_RE = re.compile(
    r"^https://api\.github\.com/repos/StrayForest/old_sparky/check-runs/[1-9][0-9]{0,31}$"
)
ROUTE_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "version",
        "target_sha",
        "event",
        "class",
        "expected_gates",
        "runtime_sensitive",
        "deployable",
        "fallback",
        "reason",
        "files",
        "digest",
    }
)
ROUTE_MANIFEST_DIGEST_KEYS = (
    "schema",
    "version",
    "target_sha",
    "event",
    "class",
    "expected_gates",
    "runtime_sensitive",
    "deployable",
    "fallback",
    "reason",
    "files",
)

# The current security summary has no split provenance fields.  If its
# producer grows those fields, only these two exact, closed spellings are
# accepted.  Every field is type-checked and compared to the immutable merge
# context; arbitrary extra keys never become an implicit extension.
SUMMARY_PROVENANCE_FIELD_SETS = (
    frozenset({"source_head_sha", "base_sha", "tested_tree_sha", "tested_parents"}),
    frozenset({"source_sha", "base_sha", "tree_sha", "parents"}),
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
        "Authenticate internal baseline runtime proof",
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
        "Dispatch exact baseline proof finalizer",
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
    {
        "status-start",
        "Authenticate internal baseline runtime proof",
        "Conditional release runtime fixture",
        "Trusted dev immutable release runtime",
        "Dispatch exact baseline proof finalizer",
    }
)
PR_ALWAYS_SKIPPED_JOB_NAMES = frozenset(
    {
        "Authenticate internal baseline runtime proof",
        "Dispatch exact baseline proof finalizer",
    }
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
class TriggerSnapshot:
    """The immutable identity delivered by the triggering workflow_run event."""

    repository: str
    workflow_id: int
    workflow_name: str
    workflow_path: str
    run_id: str
    run_attempt: str
    source_head_sha: str
    head_ref: str
    pull_request: str
    # GitHub includes these fields in the workflow_run pull_requests entry.
    # Keep them as part of the trigger snapshot when present and require the
    # exact-attempt API response to agree.  They are not inferred from a
    # later PR read.
    association_base_sha: str | None
    association_base_ref: str | None
    association_head_sha: str | None
    association_head_ref: str | None


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    base_repository: str
    base_ref: str
    base_sha: str
    head_repository: str
    head_ref: str
    head_sha: str
    merge_sha: str


@dataclass(frozen=True, slots=True)
class RunContext:
    repository: str
    workflow_id: int
    workflow_name: str
    workflow_path: str
    run_id: str
    run_attempt: str
    pull_request: str
    original_source_head_sha: str
    original_base_sha: str
    original_base_ref: str
    original_head_ref: str
    original_base_repository: str
    original_head_repository: str
    tested_merge_ref: str
    tested_merge_sha: str
    tested_tree_sha: str
    tested_parents: tuple[str, str]

    def as_payload(self) -> dict[str, object]:
        """Serialize the closed context without an ambiguous candidate SHA."""

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
                "status": "completed",
                "conclusion": "success",
                "head_sha": self.original_source_head_sha,
                "source_head_sha": self.original_source_head_sha,
                "head_ref": self.original_head_ref,
                "head_repository": self.original_head_repository,
            },
            "pull_request": {
                "number": int(self.pull_request),
                "base": {
                    "repository": self.original_base_repository,
                    "ref": self.original_base_ref,
                    "sha": self.original_base_sha,
                },
                "head": {
                    "repository": self.original_head_repository,
                    "ref": self.original_head_ref,
                    "sha": self.original_source_head_sha,
                },
            },
            "tested_merge": {
                "repository": self.repository,
                "ref": self.tested_merge_ref,
                "sha": self.tested_merge_sha,
                "tree_sha": self.tested_tree_sha,
                "parents": list(self.tested_parents),
            },
        }


def _association_repository(value: object, description: str) -> None:
    """Validate the partial repo object returned in workflow_run PR rows."""

    repository = _object(value, description)
    if "full_name" not in repository and "name" not in repository:
        raise CandidateError(f"{description} identity is missing")
    if "full_name" in repository and repository.get("full_name") != REPOSITORY:
        raise CandidateError(f"{description} is not canonical")
    if "name" in repository and repository.get("name") != "old_sparky":
        raise CandidateError(f"{description} name is not canonical")
    owner = repository.get("owner")
    if owner is not None:
        owner_mapping = _object(owner, f"{description} owner")
        if owner_mapping.get("login") != "StrayForest":
            raise CandidateError(f"{description} owner is not canonical")


def _workflow_run_pull_request_snapshot(
    payload: Mapping[str, object],
    *,
    require_fields: bool,
) -> tuple[str, dict[str, str | None]]:
    """Read one workflow_run PR row and its documented head/base snapshot."""

    rows = payload.get("pull_requests")
    if not isinstance(rows, list) or len(rows) != 1:
        raise CandidateError("security run must have exactly one pull request")
    row = _object(rows[0], "security run pull request")
    number = _id(row.get("number"), "security run pull request number")
    snapshot: dict[str, str | None] = {
        "base_sha": None,
        "base_ref": None,
        "head_sha": None,
        "head_ref": None,
    }
    for role in ("base", "head"):
        value = row.get(role)
        if value is None:
            if require_fields:
                raise CandidateError(f"security run pull request {role} snapshot is missing")
            continue
        section = _object(value, f"security run pull request {role}")
        if require_fields and ("ref" not in section or "sha" not in section or "repo" not in section):
            raise CandidateError(f"security run pull request {role} snapshot is incomplete")
        if "ref" in section:
            ref = _safe_branch(section.get("ref"), f"security run pull request {role} ref")
            snapshot[f"{role}_ref"] = ref
        if "sha" in section:
            snapshot[f"{role}_sha"] = _sha(section.get("sha"), f"security run pull request {role} SHA")
        if "repo" in section:
            _association_repository(section.get("repo"), f"security run pull request {role} repository")
    return number, snapshot


def _workflow_run_fields(
    run: Mapping[str, object],
    *,
    expected: TriggerSnapshot,
    require_association: bool = True,
) -> None:
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
    if _sha(run.get("head_sha"), "security run source head SHA") != expected.source_head_sha:
        raise CandidateError("security run source head SHA changed")
    if _safe_branch(run.get("head_branch")) != expected.head_ref:
        raise CandidateError("security run head ref changed")
    head_repository = _object(run.get("head_repository"), "security run head repository")
    if _repository(head_repository.get("full_name"), "security run head repository") != REPOSITORY:
        raise CandidateError("security run head repository is not canonical")
    number, association = _workflow_run_pull_request_snapshot(
        run,
        require_fields=require_association,
    )
    if number != expected.pull_request:
        raise CandidateError("security run pull request identity changed")
    for key, expected_value in (
        ("base_sha", expected.association_base_sha),
        ("base_ref", expected.association_base_ref),
        ("head_sha", expected.association_head_sha),
        ("head_ref", expected.association_head_ref),
    ):
        actual = association[key]
        if expected_value is not None and actual is not None and actual != expected_value:
            raise CandidateError("security run pull request snapshot changed")


def _pr_repository(value: object, description: str) -> None:
    repository = _object(value, description)
    if _repository(repository.get("full_name"), description) != REPOSITORY:
        raise CandidateError(f"{description} is not canonical")
    owner = repository.get("owner")
    if not isinstance(owner, Mapping) or owner.get("login") != "StrayForest":
        raise CandidateError(f"{description} owner is not canonical")


def _validate_pr(
    pr: Mapping[str, object],
    *,
    expected: TriggerSnapshot,
    expected_context: RunContext | None = None,
) -> PullRequestSnapshot:
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
    if head_sha != expected.source_head_sha or head_ref != expected.head_ref:
        raise CandidateError("pull request head does not match security run")
    if base_sha == head_sha:
        raise CandidateError("pull request head is not distinct from base")
    merge_sha = pr.get("merge_commit_sha")
    # A current PR response with null/missing merge_commit_sha is not a
    # usable tested context.  The merge ref and commit API below must prove
    # this exact non-null value, rather than silently falling back to head.
    merge_sha = _sha(merge_sha, "pull request merge commit SHA")
    if merge_sha == head_sha:
        raise CandidateError("synthetic merge SHA was presented as the PR head")
    label = head.get("label")
    if label is not None and label != f"StrayForest:{head_ref}":
        raise CandidateError("pull request head label is not canonical")
    association_checks = (
        (expected.association_base_sha, base_sha, "workflow_run base SHA"),
        (expected.association_base_ref, DEFAULT_BRANCH, "workflow_run base ref"),
        (expected.association_head_sha, head_sha, "workflow_run head SHA"),
        (expected.association_head_ref, head_ref, "workflow_run head ref"),
    )
    for association_value, current_value, description in association_checks:
        if association_value is not None and association_value != current_value:
            raise CandidateError(f"{description} does not match pull request")
    if expected_context is not None:
        if base_sha != expected_context.original_base_sha:
            raise CandidateError("pull request base SHA changed during handoff")
        if head_sha != expected_context.original_source_head_sha:
            raise CandidateError("pull request source head changed during handoff")
        if head_ref != expected_context.original_head_ref:
            raise CandidateError("pull request head ref changed during handoff")
        if merge_sha != expected_context.tested_merge_sha:
            raise CandidateError("pull request merge SHA changed during handoff")
    return PullRequestSnapshot(
        base_repository=_repository(base["repo"].get("full_name"), "pull request base repository"),
        base_ref=DEFAULT_BRANCH,
        base_sha=base_sha,
        head_repository=_repository(head["repo"].get("full_name"), "pull request head repository"),
        head_ref=head_ref,
        head_sha=head_sha,
        merge_sha=merge_sha,
    )


def _validate_workflow_run_pr_snapshot(
    run: Mapping[str, object],
    pr: PullRequestSnapshot,
) -> None:
    """Bind the exact-attempt API's embedded PR snapshot to the PR read."""

    _number, association = _workflow_run_pull_request_snapshot(
        run,
        require_fields=True,
    )
    expected = {
        "base_sha": pr.base_sha,
        "base_ref": pr.base_ref,
        "head_sha": pr.head_sha,
        "head_ref": pr.head_ref,
    }
    for key, value in expected.items():
        if association.get(key) != value:
            raise CandidateError("security run PR snapshot does not match current pull request")


def _validate_merge_ref(
    payload: object,
    *,
    pull_request: str,
    expected_merge_sha: str,
) -> str:
    """Validate matching-refs GET /git/matching-refs/pull/N/merge."""

    # GitHub's documented matching-refs endpoint returns a one-row array for
    # this synthetic ref.  Do not accept the single-ref endpoint's object
    # shape as an implicit alternate producer contract.
    if not isinstance(payload, list) or len(payload) != 1:
        raise CandidateError("pull request merge ref result is not singular")
    payload = _object(payload[0], "pull request merge ref row")
    expected_ref = f"refs/pull/{pull_request}/merge"
    if payload.get("ref") != expected_ref:
        raise CandidateError("pull request merge ref name is not canonical")
    obj = _object(payload.get("object"), "pull request merge ref object")
    if obj.get("type") != "commit":
        raise CandidateError("pull request merge ref is not a commit")
    merge_sha = _sha(obj.get("sha"), "pull request merge ref SHA")
    if merge_sha != expected_merge_sha:
        raise CandidateError("pull request merge ref SHA does not match PR")
    return merge_sha


def _validate_merge_commit(
    payload: Mapping[str, object],
    *,
    expected_merge_sha: str,
    expected_base_sha: str,
    expected_source_head_sha: str,
) -> tuple[str, tuple[str, str]]:
    """Validate GET /commits/<merge> tree and ordered two-parent ancestry."""

    if _sha(payload.get("sha"), "tested merge commit SHA") != expected_merge_sha:
        raise CandidateError("tested merge commit SHA does not match PR")
    commit = _object(payload.get("commit"), "tested merge commit details")
    tree = _object(commit.get("tree"), "tested merge commit tree")
    tree_sha = _sha(tree.get("sha"), "tested merge tree SHA")
    parents = payload.get("parents")
    if not isinstance(parents, list) or len(parents) != 2:
        raise CandidateError("tested merge commit must have exactly two parents")
    parent_shas = tuple(_sha(_object(parent, "tested merge parent").get("sha"), "tested merge parent SHA") for parent in parents)
    if parent_shas != (expected_base_sha, expected_source_head_sha):
        raise CandidateError("tested merge commit parents are not [base, head]")
    return tree_sha, (parent_shas[0], parent_shas[1])


def inspect_event(path: Path) -> TriggerSnapshot:
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
    source_head_sha = _sha(workflow_run.get("head_sha"), "workflow_run source head SHA")
    head_ref = _safe_branch(workflow_run.get("head_branch"))
    head_repository = _object(workflow_run.get("head_repository"), "workflow_run head repository")
    if _repository(head_repository.get("full_name"), "workflow_run head repository") != REPOSITORY:
        raise CandidateError("workflow_run head repository is not canonical")
    pull_request, association = _workflow_run_pull_request_snapshot(
        workflow_run,
        require_fields=True,
    )
    association_head_sha = _sha(association["head_sha"], "workflow_run pull request source head SHA")
    if association_head_sha != source_head_sha:
        raise CandidateError("workflow_run source head snapshot does not match run")
    if association["head_ref"] != head_ref:
        raise CandidateError("workflow_run pull request head snapshot does not match run")
    if association["base_ref"] != DEFAULT_BRANCH:
        raise CandidateError("workflow_run pull request base is not dev")
    if association["base_sha"] is None:
        raise CandidateError("workflow_run pull request base SHA is missing")
    return TriggerSnapshot(
        repository=REPOSITORY,
        workflow_id=SECURITY_WORKFLOW_ID,
        workflow_name=SECURITY_WORKFLOW_NAME,
        workflow_path=SECURITY_WORKFLOW_PATH,
        run_id=run_id,
        run_attempt=run_attempt,
        source_head_sha=source_head_sha,
        head_ref=head_ref,
        pull_request=pull_request,
        association_base_sha=association["base_sha"],
        association_base_ref=association["base_ref"],
        association_head_sha=association["head_sha"],
        association_head_ref=association["head_ref"],
    )


def validate_context(
    event_path: Path,
    run_path: Path,
    pr_path: Path,
    merge_ref_path: Path,
    commit_path: Path,
    *,
    output: Path,
    github_output: Path | None = None,
    latest_run_path: Path | None = None,
    expected_context_path: Path | None = None,
) -> RunContext:
    trigger = inspect_event(event_path)
    run = _object(_read_json(run_path, description="security run"), "security run")
    _workflow_run_fields(run, expected=trigger)
    if latest_run_path is not None:
        latest = _object(_read_json(latest_run_path, description="latest security run"), "latest security run")
        # The exact-attempt endpoint includes the embedded base/head snapshot;
        # the ordinary run endpoint may expose only the PR number.  Bind any
        # fields the latest response supplies, while the exact attempt and
        # current PR remain the authoritative complete snapshot.
        _workflow_run_fields(latest, expected=trigger, require_association=False)
    pr = _object(_read_json(pr_path, description="pull request"), "pull request")
    expected_context = load_context(expected_context_path) if expected_context_path is not None else None
    pr_snapshot = _validate_pr(pr, expected=trigger, expected_context=expected_context)
    _validate_workflow_run_pr_snapshot(run, pr_snapshot)
    merge_ref = _read_json(merge_ref_path, description="pull request merge ref")
    merge_ref_sha = _validate_merge_ref(merge_ref, pull_request=trigger.pull_request, expected_merge_sha=pr_snapshot.merge_sha)
    commit = _object(_read_json(commit_path, description="tested merge commit"), "tested merge commit")
    tested_tree_sha, tested_parents = _validate_merge_commit(
        commit,
        expected_merge_sha=pr_snapshot.merge_sha,
        expected_base_sha=pr_snapshot.base_sha,
        expected_source_head_sha=pr_snapshot.head_sha,
    )
    if merge_ref_sha != pr_snapshot.merge_sha:
        raise CandidateError("pull request merge ref does not match merge commit")
    context = RunContext(
        repository=trigger.repository,
        workflow_id=trigger.workflow_id,
        workflow_name=trigger.workflow_name,
        workflow_path=trigger.workflow_path,
        run_id=trigger.run_id,
        run_attempt=trigger.run_attempt,
        pull_request=trigger.pull_request,
        original_source_head_sha=pr_snapshot.head_sha,
        original_base_sha=pr_snapshot.base_sha,
        original_base_ref=pr_snapshot.base_ref,
        original_head_ref=pr_snapshot.head_ref,
        original_base_repository=pr_snapshot.base_repository,
        original_head_repository=pr_snapshot.head_repository,
        tested_merge_ref=f"refs/pull/{trigger.pull_request}/merge",
        tested_merge_sha=pr_snapshot.merge_sha,
        tested_tree_sha=tested_tree_sha,
        tested_parents=tested_parents,
    )
    if expected_context is not None and context.as_payload() != expected_context.as_payload():
        raise CandidateError("immutable candidate context changed during handoff")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(context.as_payload(), sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
    if github_output is not None:
        values = {
            "source_head_sha": context.original_source_head_sha,
            "tested_merge_sha": context.tested_merge_sha,
            "head_ref": context.original_head_ref,
            "base_sha": context.original_base_sha,
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
    if set(payload) != {"schema", "repository", "security_workflow", "security_run", "pull_request", "tested_merge"}:
        raise CandidateError("candidate context schema is not closed")
    workflow = _object(payload.get("security_workflow"), "candidate context workflow")
    run = _object(payload.get("security_run"), "candidate context run")
    pr = _object(payload.get("pull_request"), "candidate context pull request")
    if type(payload.get("schema")) is not int or payload.get("schema") != SCHEMA or payload.get("repository") != REPOSITORY:
        raise CandidateError("candidate context identity is invalid")
    if workflow.get("id") != SECURITY_WORKFLOW_ID or workflow.get("name") != SECURITY_WORKFLOW_NAME or workflow.get("path") != SECURITY_WORKFLOW_PATH:
        raise CandidateError("candidate context workflow identity is invalid")
    if set(run) != {
        "id", "attempt", "event", "status", "conclusion", "head_sha", "source_head_sha", "head_ref", "head_repository"
    }:
        raise CandidateError("candidate context run schema is not closed")
    if set(pr) != {"number", "base", "head"}:
        raise CandidateError("candidate context pull request schema is not closed")
    tested = _object(payload.get("tested_merge"), "candidate context tested merge")
    if set(tested) != {"repository", "ref", "sha", "tree_sha", "parents"}:
        raise CandidateError("candidate context tested merge schema is not closed")
    base = _object(pr.get("base"), "candidate context base")
    head = _object(pr.get("head"), "candidate context head")
    if set(base) != {"repository", "ref", "sha"} or set(head) != {"repository", "ref", "sha"}:
        raise CandidateError("candidate context PR side schema is not closed")
    original_base_repository = _repository(base.get("repository"), "candidate context base repository")
    original_head_repository = _repository(head.get("repository"), "candidate context head repository")
    original_base_ref = _safe_branch(base.get("ref"), "candidate context base ref")
    original_head_ref = _safe_branch(head.get("ref"), "candidate context head ref")
    original_base_sha = _sha(base.get("sha"), "candidate context base SHA")
    original_source_head_sha = _sha(head.get("sha"), "candidate context source head SHA")
    if original_base_ref != DEFAULT_BRANCH or original_base_repository != REPOSITORY or original_head_repository != REPOSITORY:
        raise CandidateError("candidate context PR repository/ref identity is invalid")
    parents = tested.get("parents")
    if not isinstance(parents, list) or len(parents) != 2:
        raise CandidateError("candidate context tested parents are invalid")
    tested_parents = (_sha(parents[0], "candidate context first parent"), _sha(parents[1], "candidate context second parent"))
    context = RunContext(
        repository=REPOSITORY,
        workflow_id=SECURITY_WORKFLOW_ID,
        workflow_name=SECURITY_WORKFLOW_NAME,
        workflow_path=SECURITY_WORKFLOW_PATH,
        run_id=_id(run.get("id"), "candidate context run id"),
        run_attempt=_id(run.get("attempt"), "candidate context attempt"),
        original_source_head_sha=_sha(run.get("source_head_sha"), "candidate context source head SHA"),
        original_head_ref=_safe_branch(run.get("head_ref")),
        original_base_sha=original_base_sha,
        original_base_ref=original_base_ref,
        original_base_repository=original_base_repository,
        original_head_repository=original_head_repository,
        pull_request=_id(pr.get("number"), "candidate context PR number"),
        tested_merge_ref=_text(tested.get("ref"), "candidate context tested merge ref"),
        tested_merge_sha=_sha(tested.get("sha"), "candidate context tested merge SHA"),
        tested_tree_sha=_sha(tested.get("tree_sha"), "candidate context tested tree SHA"),
        tested_parents=tested_parents,
    )
    if (
        run.get("event") != SECURITY_EVENT
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("head_sha") != original_source_head_sha
        or run.get("source_head_sha") != original_source_head_sha
        or run.get("head_ref") != original_head_ref
        or run.get("head_repository") != REPOSITORY
        or tested.get("repository") != REPOSITORY
        or tested.get("ref") != f"refs/pull/{context.pull_request}/merge"
        or tested_parents != (context.original_base_sha, context.original_source_head_sha)
    ):
        raise CandidateError("candidate context event is invalid")
    return context


def _artifact_row(
    payload: object,
    *,
    name: str,
    run_id: str,
    run_attempt: str | None = None,
    expected_head_sha: str | None = None,
    expected_head_ref: str | None = None,
) -> Mapping[str, object]:
    root = _object(payload, "artifact list")
    rows = root.get("artifacts")
    if not isinstance(rows, list) or len(rows) > 100:
        raise CandidateError("artifact list is invalid")
    matches: list[Mapping[str, object]] = []
    for row in rows:
        item = _object(row, "artifact row")
        workflow_run = item.get("workflow_run")
        workflow_id = workflow_run.get("id") if isinstance(workflow_run, Mapping) else None
        if item.get("name") != name or type(workflow_id) is not int or workflow_id != int(run_id):
            continue
        if isinstance(workflow_run, Mapping):
            if "run_attempt" in workflow_run and (
                type(workflow_run.get("run_attempt")) is not int
                or run_attempt is None
                or workflow_run.get("run_attempt") != int(run_attempt)
            ):
                continue
            if expected_head_sha is not None and "head_sha" in workflow_run:
                if workflow_run.get("head_sha") != expected_head_sha:
                    continue
            if expected_head_ref is not None and "head_branch" in workflow_run:
                if workflow_run.get("head_branch") != expected_head_ref:
                    continue
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
    validated_run_id = _id(run_id, "run id")
    validated_attempt = _id(run_attempt, "run attempt")
    expected_names = {
        f"{SUMMARY_ARTIFACT_PREFIX}{validated_run_id}-{validated_attempt}",
        f"{ROUTE_ARTIFACT_PREFIX}{validated_run_id}-{validated_attempt}",
    }
    if name not in expected_names:
        raise CandidateError("security artifact name is invalid")
    row = _artifact_row(
        _read_json(metadata_path, description="artifact list"),
        name=name,
        run_id=run_id,
        run_attempt=run_attempt,
    )
    artifact_id = _id(row.get("id"), "artifact id")
    if row.get("expired") is not False:
        raise CandidateError("artifact is expired")
    if "workflow_run" not in row or not isinstance(row.get("workflow_run"), Mapping):
        raise CandidateError("artifact workflow identity is missing")
    workflow_run = row["workflow_run"]
    if "run_attempt" in workflow_run and workflow_run.get("run_attempt") != int(run_attempt):
        raise CandidateError("artifact workflow attempt identity is invalid")
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
    maximum: int = MAX_ARTIFACT_BYTES,
) -> str:
    if _id(payload.get("id"), "artifact id") != expected_id or payload.get("name") != expected_name:
        raise CandidateError("artifact identity is invalid")
    if payload.get("expired") is not False:
        raise CandidateError("artifact is expired")
    size = payload.get("size_in_bytes")
    if type(maximum) is not int or maximum <= 0 or maximum > MAX_ARTIFACT_BYTES:
        raise CandidateError("artifact size bound is invalid")
    if type(size) is not int or size <= 0 or size > maximum:
        raise CandidateError("artifact size is invalid")
    digest = _canonical_sha256(payload.get("digest"), "artifact digest")
    archive_data = _read_bytes(archive, maximum=maximum, description="artifact archive")
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
    if "workflow_name" in workflow_run and workflow_run.get("workflow_name") != SECURITY_WORKFLOW_NAME:
        raise CandidateError("artifact workflow name identity is invalid")
    if "path" in workflow_run and workflow_run.get("path") != SECURITY_WORKFLOW_PATH:
        raise CandidateError("artifact workflow path identity is invalid")
    if "event" in workflow_run and workflow_run.get("event") != SECURITY_EVENT:
        raise CandidateError("artifact workflow event identity is invalid")
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
) -> bytes:
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
    return member


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
        expected_head_sha=context.original_source_head_sha,
        expected_head_ref=context.original_head_ref,
        maximum=MAX_SUMMARY_ARCHIVE_BYTES,
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
        "archive_sha256": hashlib.sha256(_read_bytes(archive_path, maximum=MAX_SUMMARY_ARCHIVE_BYTES, description="summary archive")).hexdigest(),
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


def _route_manifest_digest(manifest: Mapping[str, object]) -> str:
    """Hash the classifier's decision-bearing fields exactly as its producer."""

    try:
        encoded = json.dumps(
            {field: manifest[field] for field in ROUTE_MANIFEST_DIGEST_KEYS},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise CandidateError("route manifest digest fields are invalid") from exc
    return hashlib.sha256(encoded).hexdigest()


def _validate_route_manifest(
    manifest: Mapping[str, object],
    context: RunContext,
    *,
    expected_digest: str,
) -> None:
    """Validate classifier data as a second, merge-bound defense layer."""

    if set(manifest) != ROUTE_MANIFEST_KEYS:
        raise CandidateError("route manifest schema is not closed")
    if (
        type(manifest.get("schema")) is not int
        or type(manifest.get("version")) is not int
        or manifest.get("schema") != 1
        or manifest.get("version") != 1
    ):
        raise CandidateError("route manifest schema version is invalid")
    if manifest.get("target_sha") != context.tested_merge_sha:
        raise CandidateError("route manifest target is not the tested merge")
    if manifest.get("event") != SECURITY_EVENT or manifest.get("class") != "full":
        raise CandidateError("route manifest route identity is invalid")
    if manifest.get("expected_gates") != list(FULL_GATE_IDS):
        raise CandidateError("route manifest gate set is invalid")
    if type(manifest.get("runtime_sensitive")) is not bool:
        raise CandidateError("route manifest runtime_sensitive is invalid")
    if manifest.get("deployable") is not False or type(manifest.get("fallback")) is not bool:
        raise CandidateError("route manifest deployability is invalid")
    if not isinstance(manifest.get("reason"), str) or not manifest.get("reason"):
        raise CandidateError("route manifest reason is invalid")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or any(not isinstance(path, str) or not path for path in files):
        raise CandidateError("route manifest file list is invalid")
    digest = manifest.get("digest")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise CandidateError("route manifest digest is invalid")
    if digest != _route_manifest_digest(manifest) or digest != expected_digest:
        raise CandidateError("route manifest digest does not match security summary")


def verify_route_artifact(
    context_path: Path,
    metadata_path: Path,
    archive_path: Path,
    *,
    expected_artifact_id: str,
    expected_manifest_digest: str,
    output: Path | None = None,
    github_output: Path | None = None,
) -> dict[str, object]:
    """Validate the classifier artifact and bind its target to tested_merge_sha."""

    context = load_context(context_path)
    metadata = _object(_read_json(metadata_path, description="route artifact metadata"), "route artifact metadata")
    artifact_id = _id(expected_artifact_id, "route artifact id")
    artifact_name = f"{ROUTE_ARTIFACT_PREFIX}{context.run_id}-{context.run_attempt}"
    if ROUTE_ARTIFACT_NAME_RE.fullmatch(artifact_name) is None:
        raise CandidateError("route artifact name is invalid")
    digest = _verify_artifact_metadata(
        metadata,
        archive_path,
        expected_id=artifact_id,
        expected_name=artifact_name,
        expected_run_id=context.run_id,
        expected_run_attempt=context.run_attempt,
        expected_head_sha=context.original_source_head_sha,
        expected_head_ref=context.original_head_ref,
    )
    member = _verify_closed_archive(
        archive_path,
        expected_member=ROUTE_MANIFEST_MEMBER,
        maximum_member_bytes=MAX_SUMMARY_BYTES,
    )
    try:
        manifest = _object(json.loads(member.decode("utf-8"), object_pairs_hook=_strict_object), "route manifest")
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise CandidateError("route manifest is invalid") from exc
    # The summary is validated separately, then supplies the exact digest
    # expected here.  This function accepts it as a required argument rather
    # than trusting the route artifact's self-reported digest.
    _validate_route_manifest(
        manifest,
        context,
        expected_digest=expected_manifest_digest,
    )
    result = {
        "artifact_id": int(metadata["id"]),
        "artifact_name": metadata["name"],
        "artifact_size": metadata["size_in_bytes"],
        "artifact_digest": digest,
        "archive_sha256": hashlib.sha256(_read_bytes(archive_path, maximum=MAX_ARTIFACT_BYTES, description="route archive")).hexdigest(),
        "manifest": manifest,
    }
    if output is not None:
        output.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
    if github_output is not None:
        with github_output.open("a", encoding="ascii") as stream:
            stream.write(f"route_artifact_id={result['artifact_id']}\n")
            stream.write(f"route_artifact_digest={result['artifact_digest']}\n")
            stream.write(f"route_archive_sha256={result['archive_sha256']}\n")
    return result


def _validate_summary(summary: Mapping[str, object], context: RunContext) -> None:
    expected_keys = {
        "schema", "tested_sha", "event", "route_event", "class", "reason", "deployable", "fallback",
        "manifest_digest", "expected_gates", "gate_results", "conditional_gate_results", "runtime_sensitive",
        "requires_release_runtime", "requires_real_release_runtime", "missing_or_failed", "route_errors",
        "status_start_result", "passed",
    }
    summary_keys = set(summary)
    extra_keys = summary_keys - expected_keys
    if (
        not expected_keys.issubset(summary_keys)
        or (extra_keys and frozenset(extra_keys) not in SUMMARY_PROVENANCE_FIELD_SETS)
    ):
        raise CandidateError("security final summary schema is not closed")
    if (
        type(summary.get("schema")) is not int
        or summary.get("schema") != 1
        or summary.get("tested_sha") != context.tested_merge_sha
    ):
        raise CandidateError("security final summary target is invalid")
    if extra_keys:
        if frozenset(extra_keys) == SUMMARY_PROVENANCE_FIELD_SETS[0]:
            source_key, tree_key, parents_key = "source_head_sha", "tested_tree_sha", "tested_parents"
        else:
            source_key, tree_key, parents_key = "source_sha", "tree_sha", "parents"
        if summary.get(source_key) != context.original_source_head_sha or summary.get("base_sha") != context.original_base_sha:
            raise CandidateError("security final summary source/base identity is invalid")
        if summary.get(tree_key) != context.tested_tree_sha:
            raise CandidateError("security final summary tree identity is invalid")
        parents = summary.get(parents_key)
        if not isinstance(parents, list) or len(parents) != 2 or tuple(parents) != context.tested_parents:
            raise CandidateError("security final summary ordered parents are invalid")
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
        # The exact-attempt workflow-jobs response documents all five fields.
        # Require them here: accepting a row with an omitted identity would
        # turn the endpoint's attempt path into an unverifiable latest/all
        # jobs mix.  The optional check_run_url is validated when supplied by
        # GitHub but is not used as authority.
        for required_field in ("run_id", "run_attempt", "head_sha", "head_branch", "workflow_name"):
            if required_field not in job:
                raise CandidateError(f"security job {required_field} identity is missing")
        if type(job.get("run_id")) is not int or job.get("run_id") != int(context.run_id):
            raise CandidateError("security job run identity is invalid")
        if type(job.get("run_attempt")) is not int or job.get("run_attempt") != int(context.run_attempt):
            raise CandidateError("security job attempt identity is invalid")
        if _sha(job.get("head_sha"), "security job source head SHA") != context.original_source_head_sha:
            raise CandidateError("security job source head SHA identity is invalid")
        if job.get("head_branch") != context.original_head_ref:
            raise CandidateError("security job head ref identity is invalid")
        if job.get("workflow_name") != SECURITY_WORKFLOW_NAME:
            raise CandidateError("security job workflow name identity is invalid")
        if "check_run_url" in job:
            _text(job.get("check_run_url"), "security job check-run URL", CHECK_RUN_URL_RE)
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
    for name in PR_ALWAYS_SKIPPED_JOB_NAMES:
        if by_name[name].get("conclusion") != "skipped":
            raise CandidateError("PR baseline proof job was unexpectedly run")
    if summary.get("requires_release_runtime") is True and by_name["Conditional release runtime fixture"].get("conclusion") != "success":
        raise CandidateError("summary required the release runtime fixture")
    if summary.get("requires_release_runtime") is False and by_name["Conditional release runtime fixture"].get("conclusion") != "skipped":
        raise CandidateError("release runtime fixture was unexpectedly run")


def verify_security_run(
    context_path: Path,
    jobs_path: Path,
    summary_metadata_path: Path,
    summary_archive_path: Path,
    route_metadata_path: Path,
    route_archive_path: Path,
    *,
    expected_summary_artifact_id: str,
    expected_route_artifact_id: str,
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
    route_result = verify_route_artifact(
        context_path,
        route_metadata_path,
        route_archive_path,
        expected_artifact_id=expected_route_artifact_id,
        expected_manifest_digest=_text(summary.get("manifest_digest"), "security summary manifest digest", SHA256_RE),
    )
    result = {
        "schema": SCHEMA,
        "context": context.as_payload(),
        "summary_artifact": {key: summary_result[key] for key in ("artifact_id", "artifact_name", "artifact_size", "artifact_digest", "archive_sha256")},
        "route_artifact": {key: route_result[key] for key in ("artifact_id", "artifact_name", "artifact_size", "artifact_digest", "archive_sha256")},
        "route_manifest_digest": route_result["manifest"]["digest"],
        "summary": summary,
    }
    output.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
    if github_output is not None:
        with github_output.open("a", encoding="ascii") as stream:
            stream.write(f"summary_artifact_id={summary_result['artifact_id']}\n")
            stream.write(f"summary_artifact_digest={summary_result['artifact_digest']}\n")
            stream.write(f"summary_archive_sha256={summary_result['archive_sha256']}\n")
    return result


def verify_ancestry(
    source_root: Path,
    *,
    base_sha: str,
    host_tools_sha: str,
    source_head_sha: str,
) -> bool:
    """Validate ancestry and return whether this pin needs a candidate.

    A pin already reachable from the current PR base is a valid, existing
    generation and therefore a successful no-op.  A strict-ancestor pin that
    is reachable from the PR head but not the base is a novel generation and
    is eligible for packaging.  Every malformed, unrelated or otherwise
    ambiguous history remains a hard failure.
    """

    root = _safe_root(source_root, "candidate")
    base = _sha(base_sha, "base SHA")
    host = _sha(host_tools_sha, "host-tools SHA")
    candidate = _sha(source_head_sha, "source head SHA")
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
        return False
    if host_in_base != "1":
        raise CandidateError("host-tools/base reachability could not be determined")
    return True


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


def _artifact_name(*, prefix: str, pull_request: str, host_tools_sha: str, source_head_sha: str, run_id: str, run_attempt: str) -> str:
    if prefix not in {CANDIDATE_ARTIFACT_PREFIX, EVIDENCE_ARTIFACT_PREFIX}:
        raise CandidateError("artifact prefix is invalid")
    name = f"{prefix}pr{pull_request}-c{host_tools_sha}-e{source_head_sha}-run{run_id}-attempt{run_attempt}"
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
    if not verify_ancestry(
        candidate,
        base_sha=context.original_base_sha,
        host_tools_sha=host_tools_sha,
        source_head_sha=context.original_source_head_sha,
    ):
        raise CandidateError("host-tools SHA is already reachable from the current base")
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
        source_head_sha=context.original_source_head_sha,
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
    if set(security) != {"schema", "context", "summary_artifact", "route_artifact", "route_manifest_digest", "summary"}:
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
    route_artifact = _object(security.get("route_artifact"), "route artifact evidence")
    if set(route_artifact) != {
        "artifact_id", "artifact_name", "artifact_size", "artifact_digest", "archive_sha256"
    }:
        raise CandidateError("route artifact evidence schema is not closed")
    route_manifest_digest = security.get("route_manifest_digest")
    if not isinstance(route_manifest_digest, str) or SHA256_RE.fullmatch(route_manifest_digest) is None:
        raise CandidateError("route manifest digest evidence is invalid")
    if route_manifest_digest != summary.get("manifest_digest"):
        raise CandidateError("route manifest digest evidence does not match summary")
    route_artifact_id = route_artifact.get("artifact_id")
    if type(route_artifact_id) is not int or route_artifact_id <= 0:
        raise CandidateError("route artifact evidence id is invalid")
    if route_artifact.get("artifact_name") != f"{ROUTE_ARTIFACT_PREFIX}{context.run_id}-{context.run_attempt}":
        raise CandidateError("route artifact evidence name is invalid")
    route_size = route_artifact.get("artifact_size")
    if type(route_size) is not int or route_size <= 0 or route_size > MAX_ARTIFACT_BYTES:
        raise CandidateError("route artifact evidence size is invalid")
    if _canonical_sha256(route_artifact.get("artifact_digest"), "route artifact evidence digest") != route_artifact.get("artifact_digest"):
        raise CandidateError("route artifact evidence digest is invalid")
    if not isinstance(route_artifact.get("archive_sha256"), str) or SHA256_RE.fullmatch(route_artifact["archive_sha256"]) is None:
        raise CandidateError("route artifact evidence archive digest is invalid")
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
        "candidate_source_sha": context.original_source_head_sha,
        "tested_merge_sha": context.tested_merge_sha,
        "tested_merge_ref": context.tested_merge_ref,
        "tested_tree_sha": context.tested_tree_sha,
        "tested_parents": list(context.tested_parents),
        "host_tools_sha": host,
        "base_sha": context.original_base_sha,
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
            "source_head_sha": context.original_source_head_sha,
            "tested_merge_sha": context.tested_merge_sha,
            "tested_merge_ref": context.tested_merge_ref,
        },
        "security_summary": {
            "artifact_id": summary_artifact.get("artifact_id"),
            "artifact_name": summary_artifact.get("artifact_name"),
            "artifact_size": summary_artifact.get("artifact_size"),
            "artifact_digest": summary_artifact.get("artifact_digest"),
            "archive_sha256": summary_artifact.get("archive_sha256"),
        },
        "route_artifact": {
            "artifact_id": route_artifact.get("artifact_id"),
            "artifact_name": route_artifact.get("artifact_name"),
            "artifact_size": route_artifact.get("artifact_size"),
            "artifact_digest": route_artifact.get("artifact_digest"),
            "archive_sha256": route_artifact.get("archive_sha256"),
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
        "schema", "kind", "repository", "deployable", "trusted_source_sha", "candidate_source_sha", "tested_merge_sha",
        "tested_merge_ref", "tested_tree_sha", "tested_parents", "host_tools_sha", "base_sha", "pull_request", "security_workflow", "security_run", "security_summary", "route_artifact", "packaging_run",
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
    context.add_argument("--run-latest", required=True, type=Path)
    context.add_argument("--pr", required=True, type=Path)
    context.add_argument("--merge-ref", required=True, type=Path)
    context.add_argument("--commit", required=True, type=Path)
    context.add_argument("--expected-context", type=Path)
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
    security.add_argument("--route-metadata", required=True, type=Path)
    security.add_argument("--route-archive", required=True, type=Path)
    security.add_argument("--route-artifact-id", required=True)
    security.add_argument("--output", required=True, type=Path)
    security.add_argument("--github-output", type=Path)
    ancestry = sub.add_parser("verify-ancestry")
    ancestry.add_argument("--source-root", required=True, type=Path)
    ancestry.add_argument("--base-sha", required=True)
    ancestry.add_argument("--host-tools-sha", required=True)
    ancestry.add_argument("--source-head-sha", required=True)
    ancestry.add_argument("--github-output", type=Path)
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
                "source_head_sha": context.source_head_sha,
                "head_ref": context.head_ref,
                "pull_request": context.pull_request,
            }
            if args.github_output is not None:
                with args.github_output.open("a", encoding="ascii") as stream:
                    for key, value in values.items():
                        stream.write(f"{key}={value}\n")
            else:
                print(json.dumps(values, sort_keys=True, separators=(",", ":")))
        elif args.command == "validate-context":
            validate_context(
                args.event,
                args.run,
                args.pr,
                args.merge_ref,
                args.commit,
                output=args.output,
                github_output=args.github_output,
                latest_run_path=args.run_latest,
                expected_context_path=args.expected_context,
            )
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
                args.route_metadata,
                args.route_archive,
                expected_summary_artifact_id=args.summary_artifact_id,
                expected_route_artifact_id=args.route_artifact_id,
                output=args.output,
                github_output=args.github_output,
            )
        elif args.command == "verify-ancestry":
            eligible = verify_ancestry(
                args.source_root,
                base_sha=args.base_sha,
                host_tools_sha=args.host_tools_sha,
                source_head_sha=args.source_head_sha,
            )
            if args.github_output is not None:
                with args.github_output.open("a", encoding="ascii") as stream:
                    stream.write(f"eligible={'true' if eligible else 'false'}\n")
            print(
                "HOST_TOOLS_CANDIDATE "
                f"ancestry=verified eligible={'true' if eligible else 'false'}"
            )
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
