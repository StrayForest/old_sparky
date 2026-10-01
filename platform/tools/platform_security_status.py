#!/usr/bin/env python3
"""Pure CI status evaluation and trusted status reconciliation.

The security workflow uses the evaluator without a status-writing token.  The
default-branch workflow uses the reconciler subcommand with a narrowly scoped
GitHub API client.  Both paths keep routing, terminal-state, pagination and
idempotency decisions in this module so workflow YAML contains no second copy
of those rules.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen


EXPECTED_GATES_BY_CLASS: Mapping[str, tuple[str, ...]] = {
    "docs-only": ("docs", "verification-contract"),
    "out-of-scope": ("verification-contract",),
    "full": (
        "backend",
        "python-quality",
        "security",
        "migration",
        "docs",
        "web-quality",
        "web-hermetic",
        "verification-contract",
    ),
}
CORE_GATE_IDS: tuple[str, ...] = (
    "backend",
    "python-quality",
    "security",
    "web-quality",
    "web-hermetic",
    "docs",
    "migration",
    "verification-contract",
)
PUBLISHED_EVENTS = frozenset({"push", "workflow_dispatch"})

REPOSITORY = "StrayForest/old_sparky"
SECURITY_WORKFLOW_NAME = "Platform security and build"
SECURITY_WORKFLOW_PATH = ".github/workflows/platform-security.yml"
DEFAULT_BRANCH = "dev"
STATUS_CONTEXT = "platform-security-build"
PASS_DESCRIPTION = "Platform security and build passed"
FAIL_DESCRIPTION = "Platform security or build failed"
STATUS_STATES = frozenset({"error", "failure", "pending", "success"})
TERMINAL_FAILURE_CONCLUSIONS = frozenset(
    {
        "cancelled",
        "failure",
        "skipped",
        "timed_out",
        "action_required",
        "neutral",
        "stale",
        "startup_failure",
    }
)
SUMMARY_SCHEMA = 1
PAGE_SIZE = 100
MAX_ID = 10**32
MAX_RUN_PAGES = 100
MAX_RUN_ROWS = MAX_RUN_PAGES * PAGE_SIZE
MAX_STATUS_PAGES = 100
MAX_STATUS_ROWS = MAX_STATUS_PAGES * PAGE_SIZE
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


class ReconcilerError(ValueError):
    """Raised when a workflow or API value cannot be trusted."""


def terminal_failure_required(status: object, conclusion: object) -> bool:
    """Return whether a source run needs a terminal failure reconciliation."""

    if status != "completed":
        return True
    if conclusion == "success":
        return False
    # Unknown conclusions fail closed just like every known terminal failure.
    if isinstance(conclusion, str) and conclusion in TERMINAL_FAILURE_CONCLUSIONS:
        return True
    return True


def _value(environment: Mapping[str, object], key: str, default: str = "") -> str:
    """Read one environment-like value without coercing malformed objects."""

    value = environment.get(key, default)
    return value if isinstance(value, str) else ""


def _json_expected_gates(raw: str) -> object:
    try:
        return json.loads(raw or "null")
    except (TypeError, json.JSONDecodeError):
        return None


def evaluate_status(environment: Mapping[str, object]) -> tuple[bool, dict[str, object]]:
    """Evaluate the security workflow route and gate results.

    The returned summary mirrors the former inline evaluator field-for-field.
    Every malformed or missing route/result value is a failure, while the
    evaluator itself remains total so the publisher can fail closed when the
    candidate evaluator cannot run.
    """

    if not isinstance(environment, Mapping):
        environment = {}
    route_class = _value(environment, "ROUTE_CLASS")
    route_event = _value(environment, "ROUTE_EVENT")
    event_name = _value(environment, "EVENT_NAME")
    raw_runtime_sensitive = _value(environment, "ROUTE_RUNTIME_SENSITIVE")
    route_errors: list[str] = []
    if raw_runtime_sensitive not in {"true", "false"}:
        route_errors.append("classifier runtime-sensitive output is missing or malformed")
    runtime_sensitive = raw_runtime_sensitive == "true"

    expected = _json_expected_gates(_value(environment, "EXPECTED_GATES"))
    if route_class not in EXPECTED_GATES_BY_CLASS:
        route_errors.append("classifier route class is missing or invalid")
    elif expected != list(EXPECTED_GATES_BY_CLASS[route_class]):
        route_errors.append("classifier expected gates do not match its class")
    if route_event != event_name:
        route_errors.append("classifier event does not match the workflow event")

    tested_sha = _value(environment, "TESTED_SHA")
    if len(tested_sha) != 40 or any(character not in "0123456789abcdef" for character in tested_sha):
        route_errors.append("tested SHA is missing")
    route_target_sha = _value(environment, "ROUTE_TARGET_SHA")
    if len(route_target_sha) != 40 or any(character not in "0123456789abcdef" for character in route_target_sha):
        route_errors.append("classifier target SHA is missing")
    elif route_target_sha != tested_sha:
        route_errors.append("classifier target SHA does not match the tested SHA")
    route_digest = _value(environment, "ROUTE_DIGEST")
    if len(route_digest) != 64 or any(character not in "0123456789abcdef" for character in route_digest):
        route_errors.append("classifier digest is missing")
    if not _value(environment, "ROUTE_REASON"):
        route_errors.append("classifier reason is missing")
    if _value(environment, "ROUTE_DEPLOYABLE") not in {"true", "false"}:
        route_errors.append("classifier deployable output is missing")
    raw_fallback = _value(environment, "ROUTE_FALLBACK")
    if raw_fallback not in {"true", "false"}:
        route_errors.append("classifier fallback output is missing")

    workflow_ref = _value(environment, "WORKFLOW_REF")
    published_event = event_name in PUBLISHED_EVENTS and workflow_ref == "refs/heads/dev"
    if published_event and _value(environment, "STATUS_START_RESULT") != "success":
        route_errors.append("status-start did not succeed before status publication")

    results = {
        gate: _value(environment, f"{gate.upper().replace('-', '_')}_RESULT", "missing")
        for gate in CORE_GATE_IDS
    }
    if isinstance(expected, list):
        try:
            missing_or_failed = [gate for gate in expected if results.get(gate) != "success"]
        except TypeError:
            missing_or_failed = ["classifier expected gates are malformed"]
    else:
        missing_or_failed = ["classifier expected gates are malformed"]
    release_runtime_result = _value(environment, "RELEASE_RUNTIME_RESULT", "missing")
    real_runtime_result = _value(environment, "RELEASE_RUNTIME_REAL_RESULT", "skipped")
    requires_release_runtime = runtime_sensitive or raw_fallback == "true"
    trusted_real_event = (
        (event_name == "push" and workflow_ref == "refs/heads/dev")
        or (event_name == "workflow_dispatch" and workflow_ref == "refs/heads/dev")
    )
    requires_real_release_runtime = requires_release_runtime and trusted_real_event
    if requires_release_runtime:
        if release_runtime_result != "success":
            missing_or_failed.append("release-runtime")
    elif release_runtime_result != "skipped":
        route_errors.append("release-runtime must be skipped for a non-sensitive non-fallback route")
    if requires_real_release_runtime:
        if real_runtime_result != "success":
            missing_or_failed.append("release-runtime-real")
    elif real_runtime_result not in {"", "skipped"}:
        route_errors.append("real release-runtime must be skipped outside trusted dev routes")

    passed = (
        _value(environment, "CLASSIFIER_RESULT") == "success"
        and not route_errors
        and not missing_or_failed
    )
    summary: dict[str, object] = {
        "schema": SUMMARY_SCHEMA,
        "tested_sha": tested_sha,
        "event": event_name,
        "route_event": route_event,
        "class": route_class,
        "reason": _value(environment, "ROUTE_REASON"),
        "deployable": _value(environment, "ROUTE_DEPLOYABLE") == "true",
        "fallback": raw_fallback == "true",
        "manifest_digest": route_digest,
        "expected_gates": expected,
        "gate_results": results,
        "conditional_gate_results": {
            "release-runtime": release_runtime_result,
            "release-runtime-real": real_runtime_result,
        },
        "runtime_sensitive": runtime_sensitive,
        "requires_release_runtime": requires_release_runtime,
        "requires_real_release_runtime": requires_real_release_runtime,
        "missing_or_failed": missing_or_failed,
        "route_errors": route_errors,
        "status_start_result": _value(environment, "STATUS_START_RESULT", "missing"),
        "passed": passed,
    }
    return passed, summary


def _positive_id(value: object, field: str) -> int:
    if type(value) is not int or not 0 < value < MAX_ID:
        raise ReconcilerError(f"{field} is malformed")
    return value


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise ReconcilerError(f"{field} is malformed")
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReconcilerError(f"{field} is malformed")
    return value


def _repository_name(run: Mapping[str, object], field: str, repository: str) -> None:
    repository_value = _mapping(run.get("repository"), f"{field} repository")
    if repository_value.get("full_name") != repository:
        raise ReconcilerError(f"{field} repository is not canonical")


def _canonical_target_url(
    run: Mapping[str, object],
    run_id: int,
    run_attempt: int,
    repository: str,
) -> str:
    expected = f"https://github.com/{repository}/actions/runs/{run_id}"
    raw = run.get("html_url")
    parsed = urlsplit(raw) if isinstance(raw, str) else None
    if (
        raw != expected
        or parsed is None
        or parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        raise ReconcilerError("workflow run URL is not canonical")
    return f"{expected}/attempts/{run_attempt}"


def _validate_run_row(
    row: Mapping[str, object],
    *,
    expected_sha: str,
    field: str,
    repository: str,
    workflow_name: str,
    workflow_path: str,
    default_branch: str,
) -> tuple[int, int] | None:
    run_id = _positive_id(row.get("id"), f"{field} id")
    run_attempt = _positive_id(row.get("run_attempt"), f"{field} attempt")
    if row.get("name") != workflow_name or row.get("path") != workflow_path:
        raise ReconcilerError(f"{field} is outside the reconciler scope")
    if _sha(row.get("head_sha"), f"{field} head SHA") != expected_sha:
        raise ReconcilerError(f"{field} is outside the reconciler scope")
    _repository_name(row, field, repository)
    _canonical_target_url(row, run_id, run_attempt, repository)
    source_event = row.get("event")
    if source_event in {"pull_request", "merge_group"}:
        return None
    if source_event not in PUBLISHED_EVENTS or row.get("head_branch") != default_branch:
        raise ReconcilerError(f"{field} is outside the reconciler scope")
    return run_id, run_attempt


def complete_workflow_run_keys(
    pages: Sequence[Mapping[str, object]],
    *,
    expected_sha: str,
    repository: str = REPOSITORY,
    workflow_name: str = SECURITY_WORKFLOW_NAME,
    workflow_path: str = SECURITY_WORKFLOW_PATH,
    default_branch: str = DEFAULT_BRANCH,
) -> tuple[tuple[int, int], ...]:
    """Validate complete bounded workflow-run pages and return run keys."""

    if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes, bytearray)):
        raise ReconcilerError("workflow run pagination response is malformed")
    if len(pages) > MAX_RUN_PAGES:
        raise ReconcilerError("workflow run pagination exceeded its bound")
    expected_sha = _sha(expected_sha, "expected workflow run SHA")
    expected_total: int | None = None
    keys: list[tuple[int, int]] = []
    seen_keys: set[tuple[int, int]] = set()
    row_count = 0
    for page in pages:
        page_mapping = _mapping(page, "workflow run pagination response")
        total_count = page_mapping.get("total_count")
        rows = page_mapping.get("workflow_runs")
        if type(total_count) is not int or not 0 <= total_count <= MAX_RUN_ROWS:
            raise ReconcilerError("workflow run pagination total_count is malformed")
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
            raise ReconcilerError("workflow run pagination page is malformed")
        if expected_total is None:
            expected_total = total_count
        elif expected_total != total_count:
            raise ReconcilerError("workflow run pagination total_count changed")
        for raw_row in rows:
            row = _mapping(raw_row, "workflow run row")
            run_id = _positive_id(row.get("id"), "workflow run id")
            run_attempt = _positive_id(row.get("run_attempt"), "workflow run attempt")
            run_key = (run_id, run_attempt)
            if run_key in seen_keys:
                raise ReconcilerError("workflow run pagination contains duplicate run keys")
            seen_keys.add(run_key)
            row_count += 1
            key = _validate_run_row(
                row,
                expected_sha=expected_sha,
                field="workflow run row",
                repository=repository,
                workflow_name=workflow_name,
                workflow_path=workflow_path,
                default_branch=default_branch,
            )
            if key is not None:
                keys.append(key)
        if row_count > total_count or row_count > MAX_RUN_ROWS:
            raise ReconcilerError("workflow run pagination returned excess rows")
    if expected_total is None or row_count != expected_total:
        raise ReconcilerError("workflow run pagination is incomplete")
    return tuple(keys)


def has_newer_workflow_run(
    source_run_id: int,
    source_attempt: int,
    run_keys: Sequence[tuple[int, int]],
) -> bool:
    """Return whether a validated workflow-run key supersedes the source."""

    source_key = (_positive_id(source_run_id, "source run identity"), _positive_id(source_attempt, "source run attempt"))
    if not isinstance(run_keys, Sequence) or isinstance(run_keys, (str, bytes, bytearray)):
        raise ReconcilerError("workflow run keys are malformed")
    for key in run_keys:
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or type(key[0]) is not int
            or type(key[1]) is not int
            or not 0 < key[0] < MAX_ID
            or not 0 < key[1] < MAX_ID
        ):
            raise ReconcilerError("workflow run key is malformed")
        if key > source_key:
            return True
    return False


def _complete_workflow_runs_from_api(
    client: "ReconcilerClient",
    *,
    expected_sha: str,
    repository: str,
    workflow_name: str,
    workflow_path: str,
    default_branch: str,
) -> tuple[tuple[int, int], ...]:
    pages: list[Mapping[str, object]] = []
    expected_total: int | None = None
    row_count = 0
    workflow_file = workflow_path.rsplit("/", 1)[-1]
    for page_number in range(1, MAX_RUN_PAGES + 1):
        payload = client.get_json(
            f"/repos/{quote(repository, safe='/')}/actions/workflows/{workflow_file}/runs",
            {"head_sha": expected_sha, "per_page": PAGE_SIZE, "page": page_number},
        )
        page = _mapping(payload, "workflow run pagination response")
        rows = page.get("workflow_runs")
        total_count = page.get("total_count")
        if type(total_count) is not int or not 0 <= total_count <= MAX_RUN_ROWS:
            raise ReconcilerError("workflow run pagination total_count is malformed")
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
            raise ReconcilerError("workflow run pagination page is malformed")
        if expected_total is None:
            expected_total = total_count
        elif expected_total != total_count:
            raise ReconcilerError("workflow run pagination total_count changed")
        pages.append(page)
        row_count += len(rows)
        if row_count > expected_total:
            raise ReconcilerError("workflow run pagination returned excess rows")
        if row_count == expected_total:
            return complete_workflow_run_keys(
                pages,
                expected_sha=expected_sha,
                repository=repository,
                workflow_name=workflow_name,
                workflow_path=workflow_path,
                default_branch=default_branch,
            )
        if not rows:
            raise ReconcilerError("workflow run pagination is incomplete")
    raise ReconcilerError("workflow run pagination exceeded its bound")


def _complete_status_rows_from_api(
    client: "ReconcilerClient",
    *,
    expected_sha: str,
    repository: str,
) -> tuple[Mapping[str, object], ...]:
    rows: list[Mapping[str, object]] = []
    seen_ids: set[int] = set()
    for page_number in range(1, MAX_STATUS_PAGES + 1):
        payload = client.get_json(
            f"/repos/{quote(repository, safe='/')}/commits/{expected_sha}/statuses",
            {"per_page": PAGE_SIZE, "page": page_number},
        )
        page = payload
        if not isinstance(page, list) or len(page) > PAGE_SIZE:
            raise ReconcilerError("commit status pagination response is malformed")
        for raw_row in page:
            row = _mapping(raw_row, "commit status row")
            row_id = _positive_id(row.get("id"), "commit status id")
            if row_id in seen_ids:
                raise ReconcilerError("commit status pagination contains duplicate IDs")
            seen_ids.add(row_id)
            if not isinstance(row.get("context"), str) or row.get("context") == "":
                raise ReconcilerError("commit status context is malformed")
            if row.get("state") not in STATUS_STATES:
                raise ReconcilerError("commit status state is malformed")
            target_url = row.get("target_url")
            if target_url is not None and not isinstance(target_url, str):
                raise ReconcilerError("commit status target URL is malformed")
            rows.append(row)
        if len(page) < PAGE_SIZE:
            return tuple(rows)
    raise ReconcilerError("commit status pagination exceeded its bound")


def _status_target_key(
    target_url: object,
    repository: str = REPOSITORY,
) -> tuple[int, int] | None:
    if not isinstance(target_url, str):
        return None
    pattern = re.compile(
        rf"https://github\.com/{re.escape(repository)}/actions/runs/"
        rf"([1-9][0-9]{{0,31}})/attempts/([1-9][0-9]{{0,31}})\Z"
    )
    match = pattern.fullmatch(target_url)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def status_reconciliation_needed(
    status_rows: Sequence[Mapping[str, object]],
    *,
    target_url: str,
    source_key: tuple[int, int],
    desired_state: str = "failure",
    context: str = STATUS_CONTEXT,
) -> tuple[bool, str]:
    """Decide whether a terminal status write is non-duplicate and safe.

    A status only owns this publication when its context, canonical attempt URL
    and terminal state all match the write being considered.  In particular,
    an older success (or any status aimed at another attempt) must not suppress
    a current failure, and an older failure must not suppress a current
    success.
    """

    if not isinstance(status_rows, Sequence) or isinstance(status_rows, (str, bytes, bytearray)):
        raise ReconcilerError("commit status rows are malformed")
    if (
        not isinstance(source_key, tuple)
        or len(source_key) != 2
        or type(source_key[0]) is not int
        or type(source_key[1]) is not int
    ):
        raise ReconcilerError("source status run key is malformed")
    source_id = _positive_id(source_key[0], "source run identity")
    source_attempt = _positive_id(source_key[1], "source run attempt")
    if desired_state not in {"success", "failure"}:
        raise ReconcilerError("desired status state is malformed")
    if (
        not isinstance(target_url, str)
        or _status_target_key(target_url) != (source_id, source_attempt)
    ):
        raise ReconcilerError("source status target URL is not canonical")
    seen_ids: set[int] = set()
    for raw_row in status_rows:
        row = _mapping(raw_row, "commit status row")
        row_id = _positive_id(row.get("id"), "commit status id")
        if row_id in seen_ids:
            raise ReconcilerError("commit status rows contain duplicate IDs")
        seen_ids.add(row_id)
        if not isinstance(row.get("context"), str) or not row.get("context"):
            raise ReconcilerError("commit status context is malformed")
        if row.get("context") != context:
            continue
        state = row.get("state")
        if state not in STATUS_STATES:
            raise ReconcilerError("commit status state is malformed")
        row_target = row.get("target_url")
        if row_target is not None and not isinstance(row_target, str):
            raise ReconcilerError("commit status target URL is malformed")
        if row_target == target_url and state == desired_state:
            return False, f"the terminal {desired_state} status already exists"
    return True, "no matching terminal status owns this attempt"


class ReconcilerClient(Protocol):
    """Minimal API surface injected into reconciliation decisions."""

    def get_json(self, path: str, query: Mapping[str, object] | None = None) -> object:
        ...

    def post_status(self, sha: str, payload: Mapping[str, object]) -> object:
        ...


@dataclass(frozen=True)
class ReconciliationResult:
    action: str
    reason: str
    payload: Mapping[str, object] | None = None


def _source_run_identity(
    source_run: Mapping[str, object],
    *,
    repository: str,
    workflow_name: str,
    workflow_path: str,
    default_branch: str,
) -> tuple[str, tuple[int, int], str]:
    """Validate one exact dev workflow run and derive its immutable identity."""

    key = _validate_run_row(
        source_run,
        expected_sha=_sha(source_run.get("head_sha"), "source head SHA"),
        field="source workflow run",
        repository=repository,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        default_branch=default_branch,
    )
    if key is None:
        raise ReconcilerError("source workflow event is outside the dev publisher scope")
    source_sha = _sha(source_run.get("head_sha"), "source head SHA")
    source_run_id, source_attempt = key
    target_url = _canonical_target_url(
        source_run,
        source_run_id,
        source_attempt,
        repository,
    )
    return source_sha, key, target_url


def _fetch_exact_source_run(
    source_run: Mapping[str, object],
    client: ReconcilerClient,
    *,
    repository: str,
    workflow_name: str,
    workflow_path: str,
    default_branch: str,
) -> tuple[Mapping[str, object], str, tuple[int, int], str]:
    """Read back the exact source run and reject any identity drift."""

    source_sha, source_key, source_target_url = _source_run_identity(
        source_run,
        repository=repository,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        default_branch=default_branch,
    )
    run_path = f"/repos/{quote(repository, safe='/')}/actions/runs/{source_key[0]}"
    fetched_run = _mapping(client.get_json(run_path), "API workflow run")
    for field in ("id", "run_attempt", "event", "head_branch", "head_sha", "name", "path"):
        if fetched_run.get(field) != source_run.get(field):
            raise ReconcilerError(f"exact run {field} changed")
    _repository_name(fetched_run, "API run", repository)
    fetched_target_url = _canonical_target_url(
        fetched_run,
        source_key[0],
        source_key[1],
        repository,
    )
    if fetched_target_url != source_target_url:
        raise ReconcilerError("exact run target URL changed")
    return fetched_run, source_sha, source_key, source_target_url


def _publish_status_for_source(
    source_run: Mapping[str, object],
    client: ReconcilerClient,
    desired_state: str,
    *,
    fetched_run: Mapping[str, object] | None = None,
    repository: str = REPOSITORY,
    workflow_name: str = SECURITY_WORKFLOW_NAME,
    workflow_path: str = SECURITY_WORKFLOW_PATH,
    default_branch: str = DEFAULT_BRANCH,
    context: str = STATUS_CONTEXT,
) -> ReconciliationResult:
    """Apply the shared ownership, newer-run and idempotency protocol."""

    if desired_state not in {"success", "failure"}:
        raise ReconcilerError("desired status state is malformed")
    source_sha, source_key, source_target_url = _source_run_identity(
        source_run,
        repository=repository,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        default_branch=default_branch,
    )
    if fetched_run is None:
        fetched_run, fetched_sha, fetched_key, fetched_target_url = _fetch_exact_source_run(
            source_run,
            client,
            repository=repository,
            workflow_name=workflow_name,
            workflow_path=workflow_path,
            default_branch=default_branch,
        )
        if (fetched_sha, fetched_key, fetched_target_url) != (
            source_sha,
            source_key,
            source_target_url,
        ):
            raise ReconcilerError("exact source run identity changed")
    else:
        fetched_mapping = _mapping(fetched_run, "API workflow run")
        for field in ("id", "run_attempt", "event", "head_branch", "head_sha", "name", "path"):
            if fetched_mapping.get(field) != source_run.get(field):
                raise ReconcilerError(f"exact run {field} changed")
        _repository_name(fetched_mapping, "API run", repository)
        if (
            _canonical_target_url(
                fetched_mapping,
                source_key[0],
                source_key[1],
                repository,
            )
            != source_target_url
        ):
            raise ReconcilerError("exact run target URL changed")
        fetched_run = fetched_mapping

    run_keys = _complete_workflow_runs_from_api(
        client,
        expected_sha=source_sha,
        repository=repository,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        default_branch=default_branch,
    )
    if source_key not in run_keys:
        raise ReconcilerError("exact source run is absent from the complete run list")
    if has_newer_workflow_run(source_key[0], source_key[1], run_keys):
        return ReconciliationResult("skip", "a newer run or attempt owns this SHA")

    status_rows = _complete_status_rows_from_api(
        client,
        expected_sha=source_sha,
        repository=repository,
    )
    needed, reason = status_reconciliation_needed(
        status_rows,
        target_url=source_target_url,
        source_key=source_key,
        desired_state=desired_state,
        context=context,
    )
    if not needed:
        return ReconciliationResult("skip", reason)

    # The status list and workflow-run list are separate APIs. Re-read the
    # bounded run list immediately before posting so a newer successful or
    # still-running run wins the publisher/reconciler race whenever GitHub has
    # made that run observable.
    latest_run_keys = _complete_workflow_runs_from_api(
        client,
        expected_sha=source_sha,
        repository=repository,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        default_branch=default_branch,
    )
    if source_key not in latest_run_keys:
        raise ReconcilerError("exact source run disappeared from the complete run list")
    if has_newer_workflow_run(source_key[0], source_key[1], latest_run_keys):
        return ReconciliationResult("skip", "a newer run or attempt won the publication race")

    latest_status_rows = _complete_status_rows_from_api(
        client,
        expected_sha=source_sha,
        repository=repository,
    )
    latest_needed, latest_reason = status_reconciliation_needed(
        latest_status_rows,
        target_url=source_target_url,
        source_key=source_key,
        desired_state=desired_state,
        context=context,
    )
    if not latest_needed:
        return ReconciliationResult("skip", latest_reason)

    description = PASS_DESCRIPTION if desired_state == "success" else FAIL_DESCRIPTION
    payload = {
        "state": desired_state,
        "context": context,
        "description": description,
        "target_url": source_target_url,
    }
    client.post_status(source_sha, payload)
    return ReconciliationResult("publish", "terminal status published", payload)


def publish_workflow_event(
    event: Mapping[str, object],
    source_run: Mapping[str, object],
    client: ReconcilerClient,
    desired_state: str,
    *,
    repository: str = REPOSITORY,
    workflow_name: str = SECURITY_WORKFLOW_NAME,
    workflow_path: str = SECURITY_WORKFLOW_PATH,
    default_branch: str = DEFAULT_BRANCH,
    context: str = STATUS_CONTEXT,
) -> ReconciliationResult:
    """Publish one dev run through the shared status ownership protocol."""

    event_mapping = _mapping(event, "publisher event")
    _repository_name(event_mapping, "publisher event", repository)
    if event_mapping.get("ref") != f"refs/heads/{default_branch}":
        raise ReconcilerError("publisher event is not the trusted default branch")
    source_sha = _sha(source_run.get("head_sha"), "source head SHA")
    if source_run.get("event") == "push" and event_mapping.get("after") != source_sha:
        raise ReconcilerError("publisher event SHA does not match the push payload")
    if source_run.get("event") not in PUBLISHED_EVENTS:
        raise ReconcilerError("publisher event is outside the dev status scope")
    fetched_run, _fetched_sha, _fetched_key, _fetched_target_url = _fetch_exact_source_run(
        source_run,
        client,
        repository=repository,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        default_branch=default_branch,
    )
    return _publish_status_for_source(
        source_run,
        client,
        desired_state,
        fetched_run=fetched_run,
        repository=repository,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        default_branch=default_branch,
        context=context,
    )


def reconcile_workflow_event(
    event: Mapping[str, object],
    client: ReconcilerClient,
    *,
    repository: str = REPOSITORY,
    workflow_name: str = SECURITY_WORKFLOW_NAME,
    workflow_path: str = SECURITY_WORKFLOW_PATH,
    default_branch: str = DEFAULT_BRANCH,
    context: str = STATUS_CONTEXT,
) -> ReconciliationResult:
    """Validate one completed workflow event and reconcile one terminal failure."""

    event_run = _mapping(event.get("workflow_run"), "workflow_run event")
    source_event = event_run.get("event")
    if source_event not in PUBLISHED_EVENTS:
        return ReconciliationResult("skip", "source event is not a dev push or dispatch")
    if event_run.get("head_branch") != default_branch:
        return ReconciliationResult("skip", "source branch is not the trusted default branch")
    fetched_run, _source_sha, _source_key, _source_target_url = _fetch_exact_source_run(
        event_run,
        client,
        repository=repository,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        default_branch=default_branch,
    )
    if not terminal_failure_required(fetched_run.get("status"), fetched_run.get("conclusion")):
        return ReconciliationResult("skip", "successful source run leaves success publication to status-publish")
    return _publish_status_for_source(
        event_run,
        client,
        "failure",
        fetched_run=fetched_run,
        repository=repository,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        default_branch=default_branch,
        context=context,
    )


class GitHubApiClient:
    """Small fail-closed GitHub API client used only by the trusted workflow."""

    def __init__(self, *, api_root: str, repository: str, token: str) -> None:
        if api_root != "https://api.github.com" or repository != REPOSITORY or not token:
            raise ReconcilerError("reconciler environment is not canonical")
        self.api_root = api_root
        self.repository = repository
        self.token = token

    def _request(
        self,
        method: str,
        path: str,
        query: Mapping[str, object] | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> object:
        query_string = urlencode(query or {})
        url = f"{self.api_root}{path}" + (f"?{query_string}" if query_string else "")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                raw = response.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise ReconcilerError("GitHub API response is oversized")
                if response.status not in {200, 201}:
                    raise ReconcilerError("GitHub API response status is unexpected")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ReconcilerError("GitHub API request failed") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReconcilerError("GitHub API response is not JSON") from exc

    def get_json(self, path: str, query: Mapping[str, object] | None = None) -> object:
        return self._request("GET", path, query=query)

    def post_status(self, sha: str, payload: Mapping[str, object]) -> object:
        return self._request(
            "POST",
            f"/repos/{quote(self.repository, safe='/')}/statuses/{sha}",
            payload=payload,
        )


def _write_summary(path: Path, summary: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_github_output(path: Path, passed: bool) -> None:
    state = "success" if passed else "failure"
    description = PASS_DESCRIPTION if passed else FAIL_DESCRIPTION
    with path.open("a", encoding="utf-8") as output:
        output.write(f"passed={'true' if passed else 'false'}\n")
        output.write(f"state={state}\n")
        output.write(f"description={description}\n")


def _evaluate_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary-path", required=True, type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    passed, summary = evaluate_status(os.environ)
    _write_summary(args.summary_path, summary)
    if args.github_output is not None:
        _write_github_output(args.github_output, passed)
    print("true" if passed else "false")
    return 0 if passed else 1


def _environment_positive_id(key: str) -> int:
    raw = os.environ.get(key, "")
    if re.fullmatch(r"[1-9][0-9]{0,31}", raw) is None:
        raise ReconcilerError(f"{key} is malformed")
    return _positive_id(int(raw), key)


def _publisher_source_run(event: Mapping[str, object]) -> Mapping[str, object]:
    """Build the exact current dev run identity from trusted GitHub context."""

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    ref = os.environ.get("GITHUB_REF", "")
    sha = _sha(os.environ.get("GITHUB_SHA", ""), "GITHUB_SHA")
    if repository != REPOSITORY or event_name not in PUBLISHED_EVENTS:
        raise ReconcilerError("publisher environment is outside the canonical scope")
    if ref != f"refs/heads/{DEFAULT_BRANCH}":
        raise ReconcilerError("publisher environment is not the trusted default branch")
    event_mapping = _mapping(event, "publisher event")
    _repository_name(event_mapping, "publisher event", REPOSITORY)
    if event_mapping.get("ref") != ref:
        raise ReconcilerError("publisher event ref does not match the trusted ref")
    if event_name == "push" and event_mapping.get("after") != sha:
        raise ReconcilerError("publisher push payload does not match GITHUB_SHA")
    run_id = _environment_positive_id("GITHUB_RUN_ID")
    run_attempt = _environment_positive_id("GITHUB_RUN_ATTEMPT")
    return {
        "id": run_id,
        "run_attempt": run_attempt,
        "head_sha": sha,
        "head_branch": DEFAULT_BRANCH,
        "event": event_name,
        "name": SECURITY_WORKFLOW_NAME,
        "path": SECURITY_WORKFLOW_PATH,
        "repository": {"full_name": REPOSITORY},
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
    }


def _publish_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Publish one dev workflow status")
    parser.add_argument("--event-file", required=True, type=Path)
    parser.add_argument("--state", required=True, choices=("success", "failure"))
    args = parser.parse_args(argv)
    try:
        event = json.loads(args.event_file.read_text(encoding="utf-8"))
        event_mapping = _mapping(event, "publisher event")
        source_run = _publisher_source_run(event_mapping)
        client = GitHubApiClient(
            api_root=os.environ.get("GITHUB_API_URL", ""),
            repository=os.environ.get("GITHUB_REPOSITORY", ""),
            token=os.environ.get("GH_TOKEN", ""),
        )
        result = publish_workflow_event(
            event_mapping,
            source_run,
            client,
            args.state,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ReconcilerError) as exc:
        print(f"publisher rejected input: {exc}", file=sys.stderr)
        return 1
    print(f"{result.action}: {result.reason}")
    return 0


def _reconcile_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Reconcile one completed security workflow run")
    parser.add_argument("--event-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        event = json.loads(args.event_file.read_text(encoding="utf-8"))
        event_mapping = _mapping(event, "workflow_run event")
        client = GitHubApiClient(
            api_root=os.environ.get("GITHUB_API_URL", ""),
            repository=os.environ.get("GITHUB_REPOSITORY", ""),
            token=os.environ.get("GH_TOKEN", ""),
        )
        result = reconcile_workflow_event(event_mapping, client)
    except (OSError, UnicodeError, json.JSONDecodeError, ReconcilerError) as exc:
        print(f"reconciler rejected input: {exc}", file=sys.stderr)
        return 1
    print(f"{result.action}: {result.reason}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "reconcile":
        return _reconcile_main(args[1:])
    if args and args[0] == "publish":
        return _publish_main(args[1:])
    return _evaluate_main(args)


if __name__ == "__main__":
    sys.exit(main())
