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
import time
from typing import Any
from urllib.parse import urlsplit

from tools.platform_ci_classifier import (
    FULL_GATE_IDS,
    RECOVERY_BOOTSTRAP_FILES,
    RECOVERY_BOOTSTRAP_REASON,
    ClassifierError,
    classify,
    validate_manifest,
)
from tools.platform_workflow_provenance import (
    AUTODEPLOY_WORKFLOW_NAME,
    AUTODEPLOY_WORKFLOW_PATH,
    DEPLOY_STATUS_CONTEXT,
    GITHUB_SERVER_URL,
    SHA_RE,
    SECURITY_SUCCESS_DESCRIPTION,
    SECURITY_WORKFLOW_NAME,
    SECURITY_WORKFLOW_PATH,
    ProvenanceError,
    canonical_attempt_url,
    latest_context_status,
    parse_run_id,
    validate_actions_bot_status,
    validate_deployment_marker,
    validate_repository_identity,
    validate_workflow_run,
    _validate_job_rows,
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
BASELINE_RUNTIME_STATUS_CONTEXT = "platform-baseline-runtime"
BASELINE_RUNTIME_GATE_NAMES = frozenset(
    {
        "Authenticate internal baseline runtime proof",
        "Backend DB-free contours",
        "Backend PostgreSQL and Redis integration",
        "Backend privileged ephemeral contour",
        "Backend aggregate",
        "Python quality",
        "Security gates",
        "web-quality",
        "Web hermetic",
        "Documentation consistency",
        "Migration scenarios",
        "Verification contract",
        "Conditional release runtime fixture",
        "Trusted dev immutable release runtime",
        "status-start",
        "status-final",
    }
)
BASELINE_RUNTIME_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "proof_mode",
        "target_sha",
        "source_security_run_id",
        "source_security_run_attempt",
        "autodeploy_run_id",
        "autodeploy_run_attempt",
        "production_deploy_run_id",
        "production_deploy_run_attempt",
        "proof_run_id",
        "proof_run_attempt",
        "required_gates",
    }
)
BASELINE_RUNTIME_REQUIRED_GATES = frozenset(
    {
        "backend",
        "python-quality",
        "security",
        "migration",
        "docs",
        "web-quality",
        "web-hermetic",
        "verification-contract",
        "release-runtime",
        "release-runtime-real",
    }
)
BASELINE_RUNTIME_TITLE_RE = re.compile(
    r"^platform-baseline-runtime-v1:(?P<target>[0-9a-f]{40}):"
    r"s(?P<source_id>[1-9][0-9]{0,31})\.(?P<source_attempt>[1-9][0-9]{0,31}):"
    r"a(?P<auto_id>[1-9][0-9]{0,31})\.(?P<auto_attempt>[1-9][0-9]{0,31}):"
    r"d(?P<parent_id>[1-9][0-9]{0,31})\.(?P<parent_attempt>[1-9][0-9]{0,31}):"
    r"r(?P<proof_id>[1-9][0-9]{0,31})\.(?P<proof_attempt>[1-9][0-9]{0,31})\Z"
)


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


def classify_cumulative_baseline(
    incremental_manifest: object,
    cumulative_changed_paths: Sequence[str],
    *,
    expected_target_sha: str,
) -> dict[str, object]:
    """Reclassify the complete active-baseline-to-target path set.

    The incremental artifact is accepted only as a valid, non-deployable
    recovery-bootstrap manifest. The cumulative file list comes from the
    bounded first-parent diff and is reclassified here with the canonical
    router, so an incremental ``runtime_sensitive=false`` result cannot hide
    older runtime-sensitive changes still present in the candidate range.

    The result is a closed wrapper containing the canonical cumulative
    manifest and an explicit ``no_op`` flag. A pure recovery-bootstrap range
    is a verified no-op; every other non-deployable or fallback result fails
    closed. Callers must not build or activate an application release when
    ``no_op`` is true.
    """

    try:
        validate_manifest(incremental_manifest, expected_target_sha=expected_target_sha)
    except ClassifierError as exc:
        raise ProvenanceError(f"incremental classifier manifest is invalid: {exc}") from exc
    if not isinstance(incremental_manifest, Mapping):
        raise ProvenanceError("incremental classifier manifest is missing")
    incremental_files = incremental_manifest.get("files")
    if (
        incremental_manifest.get("event") != "push"
        or incremental_manifest.get("class") != "full"
        or incremental_manifest.get("expected_gates") != list(FULL_GATE_IDS)
        or incremental_manifest.get("fallback") is not False
        or type(incremental_manifest.get("runtime_sensitive")) is not bool
        or incremental_manifest.get("deployable") is not False
        or incremental_manifest.get("reason") != RECOVERY_BOOTSTRAP_REASON
        or not isinstance(incremental_files, list)
        or not any(path in RECOVERY_BOOTSTRAP_FILES for path in incremental_files)
        or any(
            path not in RECOVERY_BOOTSTRAP_FILES and not path.startswith("platform/docs/")
            for path in incremental_files
        )
    ):
        raise ProvenanceError("incremental manifest is not the expected recovery-bootstrap route")
    if (
        not isinstance(cumulative_changed_paths, Sequence)
        or isinstance(cumulative_changed_paths, (str, bytes))
        or not cumulative_changed_paths
        or len(cumulative_changed_paths) > 10_000
        or any(not isinstance(path, str) for path in cumulative_changed_paths)
    ):
        raise ProvenanceError("cumulative changed-file list is missing or malformed")
    cumulative_set = set(cumulative_changed_paths)
    if not set(incremental_files).issubset(cumulative_set):
        raise ProvenanceError("cumulative changed-file list omits incremental target changes")
    if not isinstance(expected_target_sha, str) or SHA_RE.fullmatch(expected_target_sha) is None:
        raise ProvenanceError("target SHA is invalid")
    try:
        cumulative = classify(
            cumulative_changed_paths,
            event="push",
            branch="dev",
            target_sha=expected_target_sha,
            repository_ready=True,
        )
        validate_manifest(cumulative, expected_target_sha=expected_target_sha)
    except ClassifierError as exc:
        raise ProvenanceError(f"cumulative classifier route is unsafe: {exc}") from exc
    if cumulative.get("deployable") is True:
        if cumulative.get("fallback") is not False:
            raise ProvenanceError("cumulative deployable route unexpectedly used fallback")
        return {"manifest": cumulative, "no_op": False}
    if (
        cumulative.get("class") == "full"
        and cumulative.get("expected_gates") == list(FULL_GATE_IDS)
        and cumulative.get("fallback") is False
        and type(cumulative.get("runtime_sensitive")) is bool
        and cumulative.get("reason") == RECOVERY_BOOTSTRAP_REASON
        and cumulative.get("deployable") is False
    ):
        return {"manifest": cumulative, "no_op": True}
    raise ProvenanceError("cumulative classifier route is non-deployable and not a verified bootstrap no-op")


def _expected_run_identity(value: object, field: str) -> str:
    if isinstance(value, str):
        return str(parse_run_id(value, field))
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > MAX_IDENTITY_INTEGER
    ):
        raise ProvenanceError(f"{field} is invalid")
    return str(value)


def wait_for_autodeploy_completion(
    fetch_attempt: Any,
    *,
    expected_workflow_id: int,
    expected_run_id: int,
    expected_attempt: int,
    expected_target_sha: str,
    timeout_seconds: float = 120,
    poll_interval_seconds: float = 5,
) -> Mapping[str, Any]:
    """Wait boundedly for the exact auto-deploy attempt to finish successfully.

    The production caller dispatches while its parent is still running, so the
    corresponding ``workflow_run`` may briefly be queued or in progress. This
    helper only tolerates those pending states; completion requires the
    canonical API workflow path/name, exact target and attempt, repository,
    event, branch, success conclusion, and canonical run URL.
    """

    if not callable(fetch_attempt):
        raise ProvenanceError("auto-deploy attempt fetcher is invalid")
    workflow_id = _expected_run_identity(expected_workflow_id, "auto-deploy workflow id")
    run_id = _expected_run_identity(expected_run_id, "auto-deploy run id")
    attempt = _expected_run_identity(expected_attempt, "auto-deploy run attempt")
    if not isinstance(expected_target_sha, str) or SHA_RE.fullmatch(expected_target_sha) is None:
        raise ProvenanceError("auto-deploy target SHA is invalid")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
        or isinstance(poll_interval_seconds, bool)
        or not isinstance(poll_interval_seconds, (int, float))
        or poll_interval_seconds <= 0
        or timeout_seconds > 600
        or poll_interval_seconds > timeout_seconds
    ):
        raise ProvenanceError("auto-deploy completion wait bounds are invalid")

    deadline = time.monotonic() + timeout_seconds
    while True:
        run = fetch_attempt()
        if not isinstance(run, Mapping):
            raise ProvenanceError("auto-deploy attempt response is malformed")
        if (
            _expected_run_identity(run.get("id"), "auto-deploy run id") != run_id
            or _expected_run_identity(run.get("run_attempt"), "auto-deploy run attempt") != attempt
            or _expected_run_identity(run.get("workflow_id"), "auto-deploy workflow id") != workflow_id
            or run.get("path") != AUTODEPLOY_WORKFLOW_PATH
            or run.get("name") != AUTODEPLOY_WORKFLOW_NAME
            or run.get("event") != "workflow_run"
            or run.get("head_branch") != "dev"
            or run.get("head_sha") != expected_target_sha
        ):
            raise ProvenanceError("auto-deploy attempt identity is not canonical")
        validate_repository_identity(run)
        status = run.get("status")
        if status == "completed":
            if run.get("conclusion") != "success":
                raise ProvenanceError("auto-deploy attempt did not complete successfully")
            validate_workflow_run(
                {
                    "id": int(workflow_id),
                    "path": AUTODEPLOY_WORKFLOW_PATH,
                    "name": AUTODEPLOY_WORKFLOW_NAME,
                },
                run,
                expected_run_id=int(run_id),
                expected_attempt=int(attempt),
                expected_target_sha=expected_target_sha,
                expected_event="workflow_run",
                expected_branch="dev",
                expected_path=AUTODEPLOY_WORKFLOW_PATH,
                expected_name=AUTODEPLOY_WORKFLOW_NAME,
            )
            return run
        if status not in {"queued", "in_progress", "waiting", "pending", "requested"}:
            raise ProvenanceError("auto-deploy attempt state is invalid")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProvenanceError("timed out waiting for auto-deploy attempt completion")
        time.sleep(min(poll_interval_seconds, remaining))


def validate_baseline_runtime_proof(
    workflow: Mapping[str, Any],
    run: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
    receipt: object,
    *,
    expected_target_sha: str,
    source_security_run_id: int | str,
    source_security_attempt: int | str,
    autodeploy_run_id: int | str,
    autodeploy_attempt: int | str,
    production_deploy_run_id: int | str,
    production_deploy_attempt: int | str,
    jobs_complete: bool,
    statuses_complete: bool,
    now: datetime | None = None,
) -> dict[str, object]:
    """Authenticate the exact security run proving cumulative runtime gates."""

    if not isinstance(expected_target_sha, str) or SHA_RE.fullmatch(expected_target_sha) is None:
        raise ProvenanceError("runtime proof target SHA is invalid")
    if type(jobs_complete) is not bool or not jobs_complete:
        raise ProvenanceError("runtime proof job pagination is incomplete")
    if type(statuses_complete) is not bool or not statuses_complete:
        raise ProvenanceError("runtime proof status pagination is incomplete")
    if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)) or len(jobs) > MAX_JOB_ROWS:
        raise ProvenanceError("runtime proof job snapshot is missing or too large")
    if not isinstance(statuses, Sequence) or isinstance(statuses, (str, bytes)) or len(statuses) > MAX_STATUS_ROWS:
        raise ProvenanceError("runtime proof status snapshot is missing or too large")

    expected = {
        "source_security_run_id": _expected_run_identity(source_security_run_id, "source security run id"),
        "source_security_run_attempt": _expected_run_identity(source_security_attempt, "source security attempt"),
        "autodeploy_run_id": _expected_run_identity(autodeploy_run_id, "auto-deploy run id"),
        "autodeploy_run_attempt": _expected_run_identity(autodeploy_attempt, "auto-deploy attempt"),
        "production_deploy_run_id": _expected_run_identity(production_deploy_run_id, "production run id"),
        "production_deploy_run_attempt": _expected_run_identity(production_deploy_attempt, "production attempt"),
    }
    if not isinstance(receipt, Mapping) or set(receipt) != BASELINE_RUNTIME_RECEIPT_KEYS:
        raise ProvenanceError("baseline runtime receipt schema is invalid")
    if (
        type(receipt.get("schema")) is not int
        or receipt.get("schema") != 1
        or receipt.get("proof_mode") != "baseline-runtime"
        or receipt.get("target_sha") != expected_target_sha
    ):
        raise ProvenanceError("baseline runtime receipt identity is invalid")
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ProvenanceError(f"baseline runtime receipt {field} does not match its parent")
    proof_run_id = _expected_run_identity(run.get("id"), "runtime proof run id")
    proof_attempt = _expected_run_identity(run.get("run_attempt"), "runtime proof attempt")
    if receipt.get("proof_run_id") != proof_run_id or receipt.get("proof_run_attempt") != proof_attempt:
        raise ProvenanceError("baseline runtime receipt is not bound to this exact proof attempt")
    if receipt.get("required_gates") != [
        "backend",
        "python-quality",
        "security",
        "migration",
        "docs",
        "web-quality",
        "web-hermetic",
        "verification-contract",
        "release-runtime",
        "release-runtime-real",
    ]:
        raise ProvenanceError("baseline runtime receipt gate list is not canonical")

    expected_title = {
        "target": expected_target_sha,
        "source_id": expected["source_security_run_id"],
        "source_attempt": expected["source_security_run_attempt"],
        "auto_id": expected["autodeploy_run_id"],
        "auto_attempt": expected["autodeploy_run_attempt"],
        "parent_id": expected["production_deploy_run_id"],
        "parent_attempt": expected["production_deploy_run_attempt"],
        "proof_id": proof_run_id,
        "proof_attempt": proof_attempt,
    }
    expected_run_name = (
        f"platform-baseline-runtime-v1:{expected_title['target']}:"
        f"s{expected_title['source_id']}.{expected_title['source_attempt']}:"
        f"a{expected_title['auto_id']}.{expected_title['auto_attempt']}:"
        f"d{expected_title['parent_id']}.{expected_title['parent_attempt']}:"
        f"r{expected_title['proof_id']}.{expected_title['proof_attempt']}"
    )

    attempt_url = validate_workflow_run(
        workflow,
        run,
        expected_run_id=int(proof_run_id),
        expected_attempt=int(proof_attempt),
        expected_target_sha=expected_target_sha,
        expected_event="workflow_dispatch",
        expected_branch="dev",
        expected_path=SECURITY_WORKFLOW_PATH,
        expected_name=SECURITY_WORKFLOW_NAME,
        expected_run_name=expected_run_name,
    )
    if run.get("path") != SECURITY_WORKFLOW_PATH:
        raise ProvenanceError("runtime proof workflow definition is not from trusted dev")
    title = run.get("display_title")
    match = BASELINE_RUNTIME_TITLE_RE.fullmatch(title) if isinstance(title, str) else None
    if match is None or match.groupdict() != expected_title:
        raise ProvenanceError("runtime proof title is not bound to its exact source and parent runs")

    marker = latest_context_status(
        statuses,
        context=BASELINE_RUNTIME_STATUS_CONTEXT,
        now=now,
        max_age=None,
    )
    validate_actions_bot_status(
        marker,
        expected_context=BASELINE_RUNTIME_STATUS_CONTEXT,
        expected_state="success",
        expected_target_url=attempt_url,
        expected_description=SECURITY_SUCCESS_DESCRIPTION,
    )
    if marker.get("target_url") != canonical_attempt_url(
        run,
        expected_run_id=int(proof_run_id),
        expected_attempt=int(proof_attempt),
    ):
        raise ProvenanceError("runtime proof status target is not the exact attempt")

    _validate_job_rows(jobs)
    jobs_by_name: dict[str, list[Mapping[str, Any]]] = {}
    for job in jobs:
        jobs_by_name.setdefault(str(job.get("name")), []).append(job)
    if not BASELINE_RUNTIME_GATE_NAMES.issubset(jobs_by_name):
        raise ProvenanceError("runtime proof workflow is missing required gate jobs")
    if any(
        len(rows) != 1
        or rows[0].get("status") != "completed"
        or rows[0].get("conclusion") != "success"
        for rows in jobs_by_name.values()
    ):
        raise ProvenanceError("a required baseline runtime proof job did not succeed exactly once")
    return {
        "attempt_url": attempt_url,
        "target_sha": expected_target_sha,
        "receipt": dict(receipt),
    }


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
