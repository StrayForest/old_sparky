#!/usr/bin/env python3
"""Fail-closed contracts for GitHub Actions workflow provenance.

The production workflows invoke this dependency-free module from the exact
tested source checkout.  It is the executable local contract for the
``workflow_run`` provenance boundary and is also suitable for runner-side
validators.

Status contexts are advisory signals only.  A deploy marker is accepted only
when the referenced run is the exact immutable attempt, belongs to the
canonical repository/workflow, has a successful ``Deploy production`` job, and
the latest context row was produced by the GitHub Actions bot at the exact
attempt URL.  A status row cannot promote a preflight or another repository.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlsplit


REPOSITORY_OWNER = "StrayForest"
REPOSITORY_NAME = "old_sparky"
REPOSITORY_FULL_NAME = f"{REPOSITORY_OWNER}/{REPOSITORY_NAME}"
SECURITY_WORKFLOW_PATH = ".github/workflows/platform-security.yml"
SECURITY_WORKFLOW_NAME = "Platform security and build"
DEPLOY_WORKFLOW_PATH = ".github/workflows/platform-production-deploy.yml"
DEPLOY_WORKFLOW_NAME = "Platform production deploy"
DEPLOY_JOB_NAME = "Deploy production"
PREFLIGHT_JOB_NAME = "Production preflight"
SECURITY_STATUS_CONTEXT = "platform-security-build"
DEPLOY_STATUS_CONTEXT = "platform-production-deploy"
DEPLOY_SUCCESS_DESCRIPTION = "Production deployment and live smoke passed"
SECURITY_SUCCESS_DESCRIPTION = "Platform security and build passed"
ACTIONS_BOT_LOGIN = "github-actions[bot]"
ACTIONS_BOT_TYPE = "Bot"
# The public GitHub Actions bot account is stable and lets consumers reject a
# user-created status with the same context/description.  The run/job checks
# remain authoritative because a different workflow can also use a token.
ACTIONS_BOT_ID = 41898282
GITHUB_SERVER_URL = "https://github.com"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
UTC_STATUS_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
STATUS_MAX_AGE = timedelta(days=30)
STATUS_MAX_FUTURE_SKEW = timedelta(0)


class ProvenanceError(ValueError):
    """Raised when an API payload is incomplete, mismatched, or spoofed."""


def _fail(message: str) -> ProvenanceError:
    return ProvenanceError(message)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _fail(f"{field} is not a positive integer")
    return value


def _run_id(value: object, field: str) -> int:
    """Require a positive GitHub run/attempt identifier in the API bound."""

    number = _positive_int(value, field)
    if len(str(number)) > 32:
        raise _fail(f"{field} is outside the bounded decimal grammar")
    return number


def parse_run_id(value: object, field: str) -> int:
    """Parse the canonical decimal form used by workflow inputs and URLs."""

    if not isinstance(value, str) or RUN_ID_RE.fullmatch(value) is None:
        raise _fail(f"{field} is not a canonical decimal identifier")
    return _run_id(int(value, 10), field)


def _as_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(f"{field} is missing")
    return value


def parse_status_timestamp(
    value: object,
    *,
    now: datetime | None = None,
    max_age: timedelta = STATUS_MAX_AGE,
    max_future_skew: timedelta = STATUS_MAX_FUTURE_SKEW,
) -> datetime:
    """Parse and bound a GitHub status timestamp.

    GitHub status timestamps are an authorization input, not display text.
    They therefore accept only second-precision UTC ``Z`` values.  A caller
    may provide a fixed ``now`` in tests or a workflow snapshot; the default
    is the current UTC clock.  Future and stale timestamps are rejected rather
    than being silently sorted around.
    """

    if (
        not isinstance(value, str)
        or UTC_STATUS_TIMESTAMP_RE.fullmatch(value) is None
        or not isinstance(max_age, timedelta)
        or not isinstance(max_future_skew, timedelta)
        or max_age <= timedelta(0)
        or max_future_skew < timedelta(0)
    ):
        raise _fail("status timestamp is malformed")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise _fail("status timestamp is not a valid UTC instant") from exc
    reference = now if now is not None else datetime.now(timezone.utc)
    if (
        not isinstance(reference, datetime)
        or reference.tzinfo is None
        or reference.utcoffset() is None
    ):
        raise _fail("status timestamp reference is not timezone-aware")
    reference = reference.astimezone(timezone.utc)
    if parsed > reference + max_future_skew:
        raise _fail("status timestamp is in the future")
    if parsed < reference - max_age:
        raise _fail("status timestamp is outside the accepted window")
    return parsed


def validate_repository_identity(run: Mapping[str, Any]) -> None:
    """Require the API run to identify the one trusted owner/repository."""

    repository = _as_mapping(run.get("repository"), "run repository")
    if "id" in repository:
        _positive_int(repository.get("id"), "repository id")
    if repository.get("full_name") != REPOSITORY_FULL_NAME:
        raise _fail("run repository full_name is not canonical")
    if repository.get("name") != REPOSITORY_NAME:
        raise _fail("run repository name is not canonical")
    owner = _as_mapping(repository.get("owner"), "run repository owner")
    if "id" in owner:
        _positive_int(owner.get("id"), "repository owner id")
    if owner.get("login") != REPOSITORY_OWNER:
        raise _fail("run repository owner is not canonical")


def canonical_run_url(
    run: Mapping[str, Any],
    *,
    expected_run_id: int,
    server_url: str = GITHUB_SERVER_URL,
) -> str:
    """Return the canonical base URL for an already validated run."""

    if not isinstance(server_url, str):
        raise _fail("GitHub server URL is malformed")
    server = server_url.rstrip("/")
    parsed_server = urlsplit(server)
    if (
        server != GITHUB_SERVER_URL
        or parsed_server.scheme != "https"
        or parsed_server.hostname != "github.com"
        or parsed_server.port is not None
        or parsed_server.username is not None
        or parsed_server.password is not None
        or parsed_server.path not in ("", "/")
        or parsed_server.query
        or parsed_server.fragment
    ):
        raise _fail("GitHub server URL is not canonical")
    expected = f"{server}/{REPOSITORY_FULL_NAME}/actions/runs/{expected_run_id}"
    if run.get("html_url") != expected:
        raise _fail("run URL is not canonical")
    return expected


def canonical_attempt_url(
    run: Mapping[str, Any],
    *,
    expected_run_id: int,
    expected_attempt: int,
    server_url: str = GITHUB_SERVER_URL,
) -> str:
    """Return the only target URL accepted for this exact run attempt."""

    base = canonical_run_url(
        run,
        expected_run_id=expected_run_id,
        server_url=server_url,
    )
    return f"{base}/attempts/{expected_attempt}"


def validate_workflow_run(
    workflow: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    expected_run_id: int,
    expected_attempt: int,
    expected_target_sha: str,
    expected_event: str,
    expected_branch: str,
    expected_path: str,
    expected_name: str,
    server_url: str = GITHUB_SERVER_URL,
) -> str:
    """Validate workflow metadata and return its exact attempt URL."""

    workflow_id = _positive_int(workflow.get("id"), "workflow id")
    if workflow.get("path") != expected_path:
        raise _fail("workflow path is not canonical")
    if workflow.get("name") != expected_name:
        raise _fail("workflow name is not canonical")
    validate_repository_identity(run)
    if _positive_int(run.get("workflow_id"), "run workflow id") != workflow_id:
        raise _fail("run belongs to a different workflow")
    if run.get("name") != expected_name:
        raise _fail("run name is not canonical")
    if _run_id(run.get("id"), "run id") != _run_id(
        expected_run_id, "run id"
    ):
        raise _fail("run id does not match the requested run")
    if _run_id(run.get("run_attempt"), "run attempt") != _run_id(
        expected_attempt, "run attempt"
    ):
        raise _fail("run attempt does not match the requested attempt")
    if (
        not isinstance(expected_target_sha, str)
        or SHA_RE.fullmatch(expected_target_sha) is None
    ):
        raise _fail("target SHA is not canonical")
    expected = {
        "event": expected_event,
        "head_branch": expected_branch,
        "head_sha": expected_target_sha,
        "status": "completed",
        "conclusion": "success",
    }
    for field, value in expected.items():
        if run.get(field) != value:
            raise _fail(f"run {field} is not the expected value")
    return canonical_attempt_url(
        run,
        expected_run_id=expected_run_id,
        expected_attempt=expected_attempt,
        server_url=server_url,
    )


def latest_context_status(
    statuses: Sequence[Mapping[str, Any]],
    *,
    context: str,
    now: datetime | None = None,
    max_age: timedelta = STATUS_MAX_AGE,
    max_future_skew: timedelta = STATUS_MAX_FUTURE_SKEW,
) -> Mapping[str, Any]:
    """Select one unambiguous latest status; timestamp errors fail closed."""

    _validate_status_rows(statuses)
    rows = [row for row in statuses if row.get("context") == context]
    if not rows:
        raise _fail(f"{context} status is missing")
    reference = now if now is not None else datetime.now(timezone.utc)
    timestamped: list[tuple[datetime, Mapping[str, Any]]] = [
        (
            _status_effective_timestamp(
                row,
                context=context,
                now=reference,
                max_age=max_age,
                max_future_skew=max_future_skew,
            ),
            row,
        )
        for row in rows
    ]
    latest_timestamp = max(timestamp for timestamp, _ in timestamped)
    latest = [row for timestamp, row in timestamped if timestamp == latest_timestamp]
    if len(latest) != 1:
        raise _fail(f"{context} status timestamp is ambiguous")
    return latest[0]


def _status_effective_timestamp(
    row: Mapping[str, Any],
    *,
    context: str,
    now: datetime | None,
    max_age: timedelta,
    max_future_skew: timedelta,
) -> datetime:
    """Validate every timestamp and return the row's effective update time."""

    if "updated_at" in row:
        effective_raw = row.get("updated_at")
        if not isinstance(effective_raw, str):
            raise _fail(f"{context} status timestamp is missing")
    elif "created_at" in row:
        effective_raw = row.get("created_at")
    else:
        raise _fail(f"{context} status timestamp is missing")
    effective = parse_status_timestamp(
        effective_raw,
        now=now,
        max_age=max_age,
        max_future_skew=max_future_skew,
    )
    # If GitHub supplies both values, validate both. Otherwise an attacker
    # could hide an invalid/future update behind an old create timestamp while
    # still influencing which row appears latest.
    if "created_at" in row:
        parse_status_timestamp(
            row.get("created_at"),
            now=now,
            max_age=max_age,
            max_future_skew=max_future_skew,
        )
    return effective


def validate_actions_bot_status(
    status: Mapping[str, Any],
    *,
    expected_context: str,
    expected_state: str,
    expected_target_url: str,
    expected_description: str | Sequence[str] | None = None,
) -> None:
    """Reject user-created or incorrectly targeted status rows."""

    status = _as_mapping(status, "status")
    _validate_status_rows([status])
    if status.get("context") != expected_context:
        raise _fail("status context is not canonical")
    if status.get("state") != expected_state:
        raise _fail("status state is not canonical")
    if status.get("target_url") != expected_target_url:
        raise _fail("status target URL is not the exact attempt")
    if expected_description is not None:
        accepted_descriptions = (
            frozenset(expected_description)
            if not isinstance(expected_description, str)
            else frozenset({expected_description})
        )
        if status.get("description") not in accepted_descriptions:
            raise _fail("status description is not canonical")
    creator = _as_mapping(status.get("creator"), "status creator")
    if (
        creator.get("login") != ACTIONS_BOT_LOGIN
        or creator.get("type") != ACTIONS_BOT_TYPE
        or creator.get("id") != ACTIONS_BOT_ID
    ):
        raise _fail("status creator is not the GitHub Actions bot")


def validate_status_collection(
    statuses: Sequence[Mapping[str, Any]],
    *,
    context: str,
    now: datetime | None = None,
    max_age: timedelta = STATUS_MAX_AGE,
    max_future_skew: timedelta = STATUS_MAX_FUTURE_SKEW,
) -> None:
    """Validate a complete status collection without selecting a marker.

    A successful preflight intentionally has no deployment marker.  It still
    must not be allowed to bypass the same malformed, future, stale, or
    ambiguous status input checks used by a successful deployment.  Callers
    use this helper when classifying the preflight no-op boundary.
    """

    _validate_status_rows(statuses)
    rows = [row for row in statuses if row.get("context") == context]
    if rows:
        # latest_context_status validates every matching row, not only the
        # selected row, and therefore rejects any invalid timestamp or tie.
        latest_context_status(
            statuses,
            context=context,
            now=now,
            max_age=max_age,
            max_future_skew=max_future_skew,
        )


def _validate_job_rows(jobs: Sequence[Mapping[str, Any]]) -> None:
    """Validate the shape and API identity types of a complete job page set."""

    if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)):
        raise _fail("job payload is malformed")
    if any(not isinstance(job, Mapping) for job in jobs):
        raise _fail("job row is malformed")
    seen_ids: set[int] = set()
    for job in jobs:
        # GitHub always supplies an id.  Missing ids are as unsafe as string
        # or boolean ids because they make a row impossible to bind to the
        # exact API object that was snapshotted.
        if "id" not in job:
            raise _fail("job id is missing")
        job_id = _positive_int(job.get("id"), "job id")
        if job_id in seen_ids:
            raise _fail("job row id is ambiguous")
        seen_ids.add(job_id)
        if not isinstance(job.get("name"), str):
            raise _fail("job name is malformed")


def _validate_status_rows(statuses: Sequence[Mapping[str, Any]]) -> None:
    """Validate the identity shape of every row in a complete status page set."""

    if not isinstance(statuses, Sequence) or isinstance(statuses, (str, bytes)):
        raise _fail("status payload is malformed")
    if any(not isinstance(row, Mapping) for row in statuses):
        raise _fail("status row is malformed")
    seen_ids: set[int] = set()
    for row in statuses:
        # When GitHub supplies an id, never coerce it and reject duplicate
        # rows; production snapshots retain that field.
        if "id" in row:
            status_id = _positive_int(row.get("id"), "status id")
            if status_id in seen_ids:
                raise _fail("status row id is ambiguous")
            seen_ids.add(status_id)


def deployment_snapshot_digest(
    workflow: Mapping[str, Any],
    run: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
) -> str:
    """Return a stable digest of every API field used by deployment auth."""

    _validate_job_rows(jobs)
    _validate_status_rows(statuses)
    try:
        encoded = json.dumps(
            {
                "workflow": workflow,
                "run": run,
                "jobs": list(jobs),
                "statuses": list(statuses),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise _fail("provenance snapshot is not serializable") from exc
    return hashlib.sha256(encoded).hexdigest()


def validate_security_marker(
    workflow: Mapping[str, Any],
    run: Mapping[str, Any],
    statuses: Sequence[Mapping[str, Any]],
    *,
    expected_run_id: int,
    expected_attempt: int,
    expected_target_sha: str,
    expected_run_url: str | None = None,
    server_url: str = GITHUB_SERVER_URL,
    now: datetime | None = None,
    max_age: timedelta = STATUS_MAX_AGE,
    max_future_skew: timedelta = STATUS_MAX_FUTURE_SKEW,
) -> str:
    """Validate the exact security run and its trusted status marker."""

    _validate_status_rows(statuses)

    attempt_url = validate_workflow_run(
        workflow,
        run,
        expected_run_id=expected_run_id,
        expected_attempt=expected_attempt,
        expected_target_sha=expected_target_sha,
        expected_event="push",
        expected_branch="dev",
        expected_path=SECURITY_WORKFLOW_PATH,
        expected_name=SECURITY_WORKFLOW_NAME,
        server_url=server_url,
    )
    if expected_run_url is not None and run.get("html_url") != expected_run_url:
        raise _fail("security workflow run URL does not match the event")
    marker = latest_context_status(
        statuses,
        context=SECURITY_STATUS_CONTEXT,
        now=now,
        max_age=max_age,
        max_future_skew=max_future_skew,
    )
    validate_actions_bot_status(
        marker,
        expected_context=SECURITY_STATUS_CONTEXT,
        expected_state="success",
        expected_target_url=attempt_url,
        expected_description=SECURITY_SUCCESS_DESCRIPTION,
    )
    return attempt_url


def validate_deployment_marker(
    workflow: Mapping[str, Any],
    run: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
    *,
    expected_run_id: int,
    expected_attempt: int,
    expected_target_sha: str,
    expected_run_url: str | None = None,
    server_url: str = GITHUB_SERVER_URL,
    now: datetime | None = None,
    max_age: timedelta = STATUS_MAX_AGE,
    max_future_skew: timedelta = STATUS_MAX_FUTURE_SKEW,
) -> str:
    """Validate a successful deploy run and its exact bot-produced marker."""

    _validate_job_rows(jobs)
    _validate_status_rows(statuses)

    attempt_url = validate_workflow_run(
        workflow,
        run,
        expected_run_id=expected_run_id,
        expected_attempt=expected_attempt,
        expected_target_sha=expected_target_sha,
        expected_event="workflow_dispatch",
        expected_branch="dev",
        expected_path=DEPLOY_WORKFLOW_PATH,
        expected_name=DEPLOY_WORKFLOW_NAME,
        server_url=server_url,
    )
    if expected_run_url is not None and run.get("html_url") != expected_run_url:
        raise _fail("deployment workflow run URL does not match the event")
    matching_jobs = [job for job in jobs if job.get("name") == DEPLOY_JOB_NAME]
    if len(matching_jobs) != 1:
        raise _fail("successful Deploy production job is missing or ambiguous")
    job = matching_jobs[0]
    if not isinstance(job, Mapping):
        raise _fail("Deploy production job metadata is malformed")
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        raise _fail("Deploy production job did not complete successfully")
    marker = latest_context_status(
        statuses,
        context=DEPLOY_STATUS_CONTEXT,
        now=now,
        max_age=max_age,
        max_future_skew=max_future_skew,
    )
    validate_actions_bot_status(
        marker,
        expected_context=DEPLOY_STATUS_CONTEXT,
        expected_state="success",
        expected_target_url=attempt_url,
        expected_description=DEPLOY_SUCCESS_DESCRIPTION,
    )
    return attempt_url


def validate_deployment_event(
    workflow: Mapping[str, Any],
    run: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
    *,
    expected_run_id: int,
    expected_attempt: int,
    expected_target_sha: str,
    expected_run_url: str | None = None,
    server_url: str = GITHUB_SERVER_URL,
    now: datetime | None = None,
    max_age: timedelta = STATUS_MAX_AGE,
    max_future_skew: timedelta = STATUS_MAX_FUTURE_SKEW,
) -> bool:
    """Validate a deployment workflow event and classify its side-effect state.

    ``workflow_run`` consumers need to distinguish an authenticated successful
    preflight (the intentional no-op) from a successful deployment.  The
    distinction is made only from the exact API run/job payload, never from a
    missing/invalid status response.  Any response ambiguity remains a hard
    validation failure.
    """

    _validate_job_rows(jobs)
    _validate_status_rows(statuses)
    attempt_url = validate_workflow_run(
        workflow,
        run,
        expected_run_id=expected_run_id,
        expected_attempt=expected_attempt,
        expected_target_sha=expected_target_sha,
        expected_event="workflow_dispatch",
        expected_branch="dev",
        expected_path=DEPLOY_WORKFLOW_PATH,
        expected_name=DEPLOY_WORKFLOW_NAME,
        server_url=server_url,
    )
    if expected_run_url is not None and run.get("html_url") != expected_run_url:
        raise _fail("deployment workflow run URL does not match the event")

    matching_deploy_jobs = [job for job in jobs if job.get("name") == DEPLOY_JOB_NAME]
    if len(matching_deploy_jobs) != 1:
        raise _fail("Deploy production job is missing or ambiguous")
    deploy_job = matching_deploy_jobs[0]
    deploy_state = (deploy_job.get("status"), deploy_job.get("conclusion"))
    if deploy_state == ("completed", "success"):
        marker = latest_context_status(
            statuses,
            context=DEPLOY_STATUS_CONTEXT,
            now=now,
            max_age=max_age,
            max_future_skew=max_future_skew,
        )
        validate_actions_bot_status(
            marker,
            expected_context=DEPLOY_STATUS_CONTEXT,
            expected_state="success",
            expected_target_url=attempt_url,
            expected_description=DEPLOY_SUCCESS_DESCRIPTION,
        )
        return True

    if deploy_state != ("completed", "skipped"):
        raise _fail("Deploy production job did not complete successfully or skip")
    matching_preflight_jobs = [
        job for job in jobs if job.get("name") == PREFLIGHT_JOB_NAME
    ]
    if len(matching_preflight_jobs) != 1:
        raise _fail("successful preflight job is missing or ambiguous")
    preflight_job = matching_preflight_jobs[0]
    if (
        preflight_job.get("status") != "completed"
        or preflight_job.get("conclusion") != "success"
    ):
        raise _fail("preflight job did not complete successfully")

    # A preflight has no success marker, but a status response still has to be
    # complete and well-formed.  This rejects a malformed/future/tied marker
    # rather than converting an API failure into the expected no-op.
    validate_status_collection(
        statuses,
        context=DEPLOY_STATUS_CONTEXT,
        now=now,
        max_age=max_age,
        max_future_skew=max_future_skew,
    )
    for row in statuses:
        if (
            row.get("context") == DEPLOY_STATUS_CONTEXT
            and row.get("target_url") == attempt_url
        ):
            raise _fail("preflight has a deployment marker")
    return False


class _ProvenanceArgumentParser(argparse.ArgumentParser):
    """Keep malformed runner arguments free of attacker-controlled echoes."""

    def error(self, _message: str) -> None:
        raise ProvenanceError("provenance arguments are malformed")


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _fail("provenance JSON payload is unreadable") from exc


def _payload_rows(payload: object, key: str, field: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        rows = payload.get(key)
        if "total_count" in payload:
            total_count = payload.get("total_count")
            if not isinstance(rows, list) or (
                isinstance(total_count, bool)
                or not isinstance(total_count, int)
                or total_count < 0
                or total_count != len(rows)
            ):
                raise _fail(f"{field} payload count is incomplete")
    else:
        rows = payload
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise _fail(f"{field} payload is malformed")
    return rows


def _status_fingerprint(
    statuses: Sequence[Mapping[str, Any]],
    *,
    context: str,
    now: datetime,
    max_age: timedelta = STATUS_MAX_AGE,
    max_future_skew: timedelta = STATUS_MAX_FUTURE_SKEW,
) -> dict[str, Any]:
    """Return only the validated marker fields used by a read-race check."""

    marker = latest_context_status(
        statuses,
        context=context,
        now=now,
        max_age=max_age,
        max_future_skew=max_future_skew,
    )
    timestamp = _status_effective_timestamp(
        marker,
        context=context,
        now=now,
        max_age=max_age,
        max_future_skew=max_future_skew,
    )
    return {
        "context": marker.get("context"),
        "state": marker.get("state"),
        "description": marker.get("description"),
        "target_url": marker.get("target_url"),
        "creator": marker.get("creator"),
        "timestamp": timestamp.isoformat(),
    }


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = _ProvenanceArgumentParser(
        description="Validate exact GitHub Actions workflow provenance"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("security", "deployment"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--workflow", type=Path, required=True)
        command_parser.add_argument("--run", type=Path, required=True)
        command_parser.add_argument("--status", type=Path, required=True)
        command_parser.add_argument("--jobs", type=Path)
        command_parser.add_argument("--expected-run-id", required=True)
        command_parser.add_argument("--expected-attempt", required=True)
        command_parser.add_argument("--expected-target-sha", required=True)
        command_parser.add_argument("--expected-run-url")
        if command == "deployment":
            command_parser.add_argument(
                "--allow-preflight-noop",
                action="store_true",
                help="accept an exact successful preflight as a non-mutating no-op",
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Validate JSON API snapshots for dependency-free workflow callers."""

    try:
        args = _build_cli_parser().parse_args(argv)
        workflow = _read_json(args.workflow)
        run = _read_json(args.run)
        status_payload = _read_json(args.status)
        if not isinstance(workflow, Mapping) or not isinstance(run, Mapping):
            raise _fail("workflow/run payload is malformed")
        expected_run_id = parse_run_id(args.expected_run_id, "expected run id")
        expected_attempt = parse_run_id(args.expected_attempt, "expected run attempt")
        now = datetime.now(timezone.utc)
        if args.command == "security":
            statuses = _payload_rows(
                status_payload,
                "statuses",
                "security status",
            )
            attempt_url = validate_security_marker(
                workflow,
                run,
                statuses,
                expected_run_id=expected_run_id,
                expected_attempt=expected_attempt,
                expected_target_sha=args.expected_target_sha,
                expected_run_url=args.expected_run_url,
                now=now,
            )
            context = SECURITY_STATUS_CONTEXT
        else:
            if args.jobs is None:
                raise _fail("deployment job payload is missing")
            jobs_payload = _read_json(args.jobs)
            jobs = _payload_rows(jobs_payload, "jobs", "deployment job")
            statuses = _payload_rows(
                status_payload,
                "statuses",
                "deployment status",
            )
            if args.allow_preflight_noop:
                deploy_ready = validate_deployment_event(
                    workflow,
                    run,
                    jobs,
                    statuses,
                    expected_run_id=expected_run_id,
                    expected_attempt=expected_attempt,
                    expected_target_sha=args.expected_target_sha,
                    expected_run_url=args.expected_run_url,
                    now=now,
                )
                attempt_url = canonical_attempt_url(
                    run,
                    expected_run_id=expected_run_id,
                    expected_attempt=expected_attempt,
                )
            else:
                deploy_ready = True
                attempt_url = validate_deployment_marker(
                    workflow,
                    run,
                    jobs,
                    statuses,
                    expected_run_id=expected_run_id,
                    expected_attempt=expected_attempt,
                    expected_target_sha=args.expected_target_sha,
                    expected_run_url=args.expected_run_url,
                    now=now,
                )
            context = DEPLOY_STATUS_CONTEXT
        snapshot_jobs = jobs if args.command == "deployment" else []
        marker = (
            _status_fingerprint(
                statuses,
                context=context,
                now=now,
            )
            if args.command == "security" or deploy_ready
            else None
        )
        print(
            json.dumps(
                {
                    "attempt_url": attempt_url,
                    "deploy_ready": deploy_ready if args.command == "deployment" else True,
                    "snapshot_digest": deployment_snapshot_digest(
                        workflow,
                        run,
                        snapshot_jobs,
                        statuses,
                    ),
                    "marker": marker,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (
        ProvenanceError,
        OSError,
        UnicodeError,
        TypeError,
        ValueError,
        AttributeError,
        KeyError,
        IndexError,
        RecursionError,
    ):
        # Do not echo JSON/API values or parser details into a workflow log.
        print("provenance validation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
