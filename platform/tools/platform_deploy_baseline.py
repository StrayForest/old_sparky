#!/usr/bin/env python3
"""Validate the active production baseline against exact GitHub proof.

This module is intentionally pure over bounded snapshots collected by the
workflow. It has no token or network access; callers must provide a complete
paginated status collection and first-parent history from the exact target
checkout.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import re
from typing import Any
from urllib.parse import urlsplit

from tools.platform_workflow_provenance import (
    DEPLOY_STATUS_CONTEXT,
    GITHUB_SERVER_URL,
    SHA_RE,
    ProvenanceError,
    latest_context_status,
    parse_run_id,
    validate_deployment_marker,
)


BASELINE_KEYS = frozenset(
    {
        "schema",
        "source_sha",
        "release_slug",
        "release_json_sha256",
        "current_link_dev",
        "current_link_ino",
        "release_dev",
        "release_ino",
        "pending_operation",
    }
)
RELEASE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
ATTEMPT_PATH_RE = re.compile(
    r"^/StrayForest/old_sparky/actions/runs/([1-9][0-9]{0,31})/attempts/([1-9][0-9]{0,31})$"
)
MAX_FIRST_PARENT_COMMITS = 8192
MAX_STATUS_ROWS = 10_000
MAX_JOB_ROWS = 10_000
MAX_IDENTITY_INTEGER = (1 << 63) - 1


def _positive_bounded_integer(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > MAX_IDENTITY_INTEGER
    ):
        raise ProvenanceError(f"baseline {field} is invalid")
    return value


def _nonnegative_bounded_integer(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_IDENTITY_INTEGER
    ):
        raise ProvenanceError(f"baseline {field} is invalid")
    return value


def _validate_baseline(baseline: object) -> Mapping[str, Any]:
    if not isinstance(baseline, Mapping) or set(baseline) != BASELINE_KEYS:
        raise ProvenanceError("active release baseline schema is invalid")
    if type(baseline.get("schema")) is not int or baseline["schema"] != 1:
        raise ProvenanceError("active release baseline schema is unsupported")
    source_sha = baseline.get("source_sha")
    if not isinstance(source_sha, str) or SHA_RE.fullmatch(source_sha) is None:
        raise ProvenanceError("active release source SHA is invalid")
    release_slug = baseline.get("release_slug")
    if (
        not isinstance(release_slug, str)
        or RELEASE_SLUG_RE.fullmatch(release_slug) is None
    ):
        raise ProvenanceError("active release slug is invalid")
    release_digest = baseline.get("release_json_sha256")
    if not isinstance(release_digest, str) or HEX_DIGEST_RE.fullmatch(release_digest) is None:
        raise ProvenanceError("active release receipt digest is invalid")
    _nonnegative_bounded_integer(baseline.get("current_link_dev"), "current link device")
    _positive_bounded_integer(baseline.get("current_link_ino"), "current link inode")
    _nonnegative_bounded_integer(baseline.get("release_dev"), "release device")
    _positive_bounded_integer(baseline.get("release_ino"), "release inode")
    if baseline.get("pending_operation") is not False:
        raise ProvenanceError("active release transaction is pending or unknown")
    return baseline


def _attempt_identity(target_url: object) -> tuple[int, int, str]:
    if not isinstance(target_url, str):
        raise ProvenanceError("deployment status attempt URL is malformed")
    parsed = urlsplit(target_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ProvenanceError("deployment status attempt URL is not canonical")
    match = ATTEMPT_PATH_RE.fullmatch(parsed.path)
    if match is None:
        raise ProvenanceError("deployment status attempt URL is not canonical")
    run_id = parse_run_id(match.group(1), "deployment run id")
    attempt = parse_run_id(match.group(2), "deployment run attempt")
    run_url = f"{GITHUB_SERVER_URL}/StrayForest/old_sparky/actions/runs/{run_id}"
    return run_id, attempt, run_url


def validate_active_baseline(
    baseline: object,
    workflow: Mapping[str, Any],
    run: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
    *,
    expected_target_sha: str,
    current_dev_sha: str,
    first_parent_shas: Sequence[str],
    statuses_complete: bool,
    jobs_complete: bool,
    now: datetime | None = None,
) -> dict[str, object]:
    """Authenticate the active release SHA and its ancestry to the target.

    ``baseline`` must be the exact tuple emitted by the immutable host reader.
    The API snapshots must come from complete bounded requests; in particular,
    ``statuses_complete`` and ``jobs_complete`` may be true only after their
    respective pagination reaches its end.
    The active SHA is eligible only when it is on the target's first-parent
    chain and its latest deployment status still points to the exact successful
    workflow attempt. There is no age-only expiry: live host identity is the
    freshness check, while status timestamps remain strict and non-future.
    """

    baseline_row = _validate_baseline(baseline)
    if not isinstance(expected_target_sha, str) or SHA_RE.fullmatch(expected_target_sha) is None:
        raise ProvenanceError("target SHA is invalid")
    if not isinstance(current_dev_sha, str) or SHA_RE.fullmatch(current_dev_sha) is None:
        raise ProvenanceError("current dev SHA is invalid")
    if current_dev_sha != expected_target_sha:
        raise ProvenanceError("target is not the current dev head")
    if type(statuses_complete) is not bool or not statuses_complete:
        raise ProvenanceError("deployment status pagination is incomplete")
    if type(jobs_complete) is not bool or not jobs_complete:
        raise ProvenanceError("deployment job pagination is incomplete")
    if (
        not isinstance(statuses, Sequence)
        or isinstance(statuses, (str, bytes))
        or len(statuses) > MAX_STATUS_ROWS
    ):
        raise ProvenanceError("deployment status snapshot is missing or too large")
    if (
        not isinstance(jobs, Sequence)
        or isinstance(jobs, (str, bytes))
        or len(jobs) > MAX_JOB_ROWS
    ):
        raise ProvenanceError("deployment job snapshot is missing or too large")
    if (
        not isinstance(first_parent_shas, Sequence)
        or isinstance(first_parent_shas, (str, bytes))
        or not first_parent_shas
        or len(first_parent_shas) > MAX_FIRST_PARENT_COMMITS
    ):
        raise ProvenanceError("first-parent history is missing or too large")
    if any(not isinstance(sha, str) or SHA_RE.fullmatch(sha) is None for sha in first_parent_shas):
        raise ProvenanceError("first-parent history contains an invalid SHA")
    if len(set(first_parent_shas)) != len(first_parent_shas):
        raise ProvenanceError("first-parent history is ambiguous")
    if first_parent_shas[0] != expected_target_sha:
        raise ProvenanceError("first-parent history does not start at the target")
    if baseline_row["source_sha"] not in first_parent_shas:
        raise ProvenanceError("active release is not a first-parent ancestor of the target")

    marker = latest_context_status(
        statuses,
        context=DEPLOY_STATUS_CONTEXT,
        now=now,
        max_age=None,
    )
    run_id, attempt, run_url = _attempt_identity(marker.get("target_url"))
    attempt_url = validate_deployment_marker(
        workflow,
        run,
        jobs,
        statuses,
        expected_run_id=run_id,
        expected_attempt=attempt,
        expected_target_sha=str(baseline_row["source_sha"]),
        expected_run_url=run_url,
        now=now,
        max_age=None,
    )
    return {
        "baseline": dict(baseline_row),
        "deployment_attempt_url": attempt_url,
        "target_sha": expected_target_sha,
    }
