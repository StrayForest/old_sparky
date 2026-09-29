"""Pure provenance/status validation for the external-load workflow.

The production workflow receives GitHub artifact metadata and runner handoff
JSON from separate jobs.  This module keeps the exact identity predicates in
one stdlib-only implementation so workflow steps cannot accidentally drift or
turn a missing/failure-bearing record into a passing boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
from typing import Any, Mapping
import zipfile


SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}")
POSITIVE_ID_RE = re.compile(r"[1-9][0-9]{0,31}")
ATTEMPT_RE = re.compile(r"[1-9][0-9]{0,8}")
ARTIFACT_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
WORKFLOW_FILE_RE = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml")
# GitHub attributes artifacts produced by a called workflow to the caller's
# workflow run.  The public workflow is therefore the artifact identity; the
# immutable implementation is recorded separately in final provenance.
CALLER_WORKFLOW_FILE = ".github/workflows/platform-production-external-load.yml"
TRUSTED_WORKFLOW_FILE = ".github/workflows/platform-production-external-load-trusted.yml"
MAX_ARTIFACT_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_METADATA_BYTES = 1 * 1024 * 1024
MAX_ARTIFACT_MEMBERS = 32
MAX_ARTIFACT_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_MEMBER_BYTES = 32 * 1024 * 1024
PROFILE_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,94}[a-z0-9])?")
PROFILE_DIGEST_RE = ARTIFACT_DIGEST_RE
SECRET_FIELD_RE = re.compile(
    r"(?:session|csrf|token|cookie|secret|password|authorization|private[_-]?key)",
    re.IGNORECASE,
)
TOKEN_LIKE_VALUE_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{32,}$")
REPORT_SAFE_LONG_STRING_FIELDS = frozenset(
    {
        "source_git_sha",
        "trusted_runner_sha",
        "profile_digest",
        "artifact_digest",
        "fixture_marker",
        "diagnostic_id",
        "request_id",
        "cf_ray",
        "path",
        "route",
        "uri",
        "url",
        "environment",
        "origin_class",
        "scenario_kind",
        "phase",
        "method",
        "route_class",
        "error_class",
        "error_kind",
        "cf_error_type",
        "cf_error_origin",
        "profile_id",
    }
)

# ``platform_load.py`` is the sole trusted report producer.  Keep its
# top-level envelope closed here as well as in the later acceptance evaluator:
# an artifact can be replaced between jobs, and a provenance-only check must
# not accept an arbitrary extra field that could carry a token-shaped value.
# Nested measurement objects remain owned by the report/evidence schema and
# are projected by ``platform_evidence_sanitizer.py`` before publication.
REPORT_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "measurement_schema",
        "timing_schema",
        "source_git_sha",
        "external_run_id",
        "external_run_attempt",
        "trusted_runner_sha",
        "profile_id",
        "profile_version",
        "profile_digest",
        "environment",
        "load_contract",
        "runner",
        "authoritative",
        "dispatchable",
        "scope",
        "mode",
        "origin_class",
        "fixture_marker",
        "users",
        "tournaments",
        "started_at",
        "finished_at",
        "wall_seconds",
        "opening_spread_seconds",
        "scenario_kind",
        "client_transport",
        "duplicate_count",
        "manual_refresh_count",
        "concurrency",
        "concurrency_stages",
        "offered_logical_actions_per_second",
        "actual_arrival_logical_actions_per_second",
        "actual_arrival_requests_per_second",
        "offered_requests_per_second",
        "late_start_count",
        "dropped_work",
        "partial_work",
        "trace",
        "timeout_path_diagnostics",
        "phases",
        "overall",
        "raw_http",
        "logical",
        "acceptance",
        # Failure reports are not accepted as successful load evidence, but
        # keeping their fixed fields known makes rejection deterministic.
        "passed",
        "error_class",
    }
)


class ExternalLoadProvenanceError(ValueError):
    """Raised when a handoff is not an exact, failure-bearing record."""


def _read_json(path: Path) -> Any:
    return _read_regular_json(path)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExternalLoadProvenanceError("JSON handoff contains duplicate keys")
        result[key] = value
    return result


def _require_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or SOURCE_SHA_RE.fullmatch(value) is None:
        raise ExternalLoadProvenanceError(f"{field} is malformed")
    return value


def _require_positive_id(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExternalLoadProvenanceError(f"{field} is malformed")
    if POSITIVE_ID_RE.fullmatch(str(value)) is None:
        raise ExternalLoadProvenanceError(f"{field} is malformed")
    return value


def _require_id_text(value: Any, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ExternalLoadProvenanceError(f"{field} is malformed")
    return value


def _require_profile_id(value: Any, field: str = "profile ID") -> str:
    if not isinstance(value, str) or PROFILE_ID_RE.fullmatch(value) is None:
        raise ExternalLoadProvenanceError(f"{field} is malformed")
    return value


def _profile_execution_budget(profile: Mapping[str, Any]) -> int:
    """Derive a bounded fixture job budget from trusted profile data.

    The workflow timeout is a property of the reviewed T profile, not a
    caller-controlled dispatch input.  Keep a small setup allowance while
    bounding the result so a malformed profile cannot reserve an unbounded
    runner or silently use the old one-size-fits-all timeout.
    """

    execution = profile.get("execution")
    if not isinstance(execution, Mapping):
        raise ExternalLoadProvenanceError("trusted profile execution contract is missing")
    if execution.get("operator_confirmation") != "RUN-PRODUCTION-EXTERNAL-LOAD":
        raise ExternalLoadProvenanceError("profile is not approved for external dispatch")
    if execution.get("require_exact_observer_binding") is not True:
        raise ExternalLoadProvenanceError("profile does not require exact observer binding")
    if execution.get("non_dispatchable") is True or execution.get("external_runner_forbidden") is True:
        raise ExternalLoadProvenanceError("profile is not dispatchable")
    traffic = profile.get("traffic")
    if not isinstance(traffic, Mapping):
        raise ExternalLoadProvenanceError("trusted profile traffic contract is missing")

    def bounded_int(value: Any, *, minimum: int, maximum: int, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ExternalLoadProvenanceError(f"profile {field} is malformed")
        return value

    timeout_seconds = bounded_int(traffic.get("timeout_seconds"), minimum=1, maximum=3600, field="timeout")
    spread_seconds = bounded_int(traffic.get("spread_seconds"), minimum=0, maximum=3600, field="spread")
    retry = traffic.get("retry")
    if not isinstance(retry, Mapping):
        raise ExternalLoadProvenanceError("profile retry contract is missing")
    max_retries = bounded_int(retry.get("max_retries"), minimum=0, maximum=10, field="retry count")
    budget_seconds = 1800 + spread_seconds + (timeout_seconds * (max_retries + 1))
    return max(15, min(60, math.ceil(budget_seconds / 60)))


def _require_profile_version(value: Any, field: str = "profile version") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9999:
        raise ExternalLoadProvenanceError(f"{field} is malformed")
    return value


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExternalLoadProvenanceError("JSON handoff is not canonicalizable") from exc


def _payload_digest(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExternalLoadProvenanceError("profile data is not canonicalizable") from exc
    return hashlib.sha256(encoded).hexdigest()


def _assert_no_secret_fields(value: Any, *, path: str = "handoff") -> None:
    """Reject credential-shaped keys before a payload can cross a boundary."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or SECRET_FIELD_RE.search(key):
                raise ExternalLoadProvenanceError(
                    f"{path} contains a credential-shaped field"
                )
            _assert_no_secret_fields(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_secret_fields(child, path=f"{path}[{index}]")


def _assert_no_unknown_token_values(value: Any, *, path: str = "report") -> None:
    """Reject token-shaped strings under fields outside the report contract."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if (
                isinstance(child, str)
                and key not in REPORT_SAFE_LONG_STRING_FIELDS
                and TOKEN_LIKE_VALUE_RE.fullmatch(child) is not None
            ):
                raise ExternalLoadProvenanceError(
                    f"{path}.{key} contains an unknown token-shaped value"
                )
            _assert_no_unknown_token_values(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_unknown_token_values(child, path=f"{path}[{index}]")


def _assert_report_schema(payload: Mapping[str, Any]) -> None:
    unknown = sorted(set(payload) - REPORT_TOP_LEVEL_FIELDS)
    if unknown:
        raise ExternalLoadProvenanceError(
            "load report schema contains unknown top-level fields"
        )
    _assert_no_unknown_token_values(payload)


def _read_regular_json(path: Path) -> Any:
    """Read one regular, non-link JSON file without trusting a path twice."""

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, TypeError) as exc:
        raise ExternalLoadProvenanceError("JSON handoff cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ExternalLoadProvenanceError("JSON handoff is not a regular file")
        data = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 8 * 1024 * 1024:
                raise ExternalLoadProvenanceError("JSON handoff is too large")
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ExternalLoadProvenanceError("JSON handoff changed while being read")
    except OSError as exc:
        raise ExternalLoadProvenanceError("JSON handoff cannot be read safely") from exc
    finally:
        os.close(descriptor)
    try:
        return json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ExternalLoadProvenanceError("JSON handoff is not valid") from exc


def normalize_artifact_digest(value: Any) -> str:
    """Return a lowercase SHA-256 digest or fail closed."""

    if not isinstance(value, str):
        raise ExternalLoadProvenanceError("artifact digest is malformed")
    digest = value.removeprefix("sha256:")
    if ARTIFACT_DIGEST_RE.fullmatch(digest) is None:
        raise ExternalLoadProvenanceError("artifact digest is malformed")
    return digest


def validate_profile_data(
    candidate: Any,
    trusted: Any,
    *,
    profile_id: Any,
    source_sha: Any,
    trusted_runner_sha: Any,
    run_id: Any,
    run_attempt: Any,
) -> dict[str, Any]:
    """Bind candidate profile *data* to a trusted runner's approved digest.

    The candidate checkout is never imported or executed.  Its profile JSON
    must be byte-for-byte equivalent under canonical JSON to the profile from
    the trusted T checkout.  This keeps T authoritative for both the schema
    and the approved profile digest while still recording E in the handoff.
    """

    if not isinstance(candidate, Mapping) or not isinstance(trusted, Mapping):
        raise ExternalLoadProvenanceError("profile data is not an object")
    _assert_no_secret_fields(candidate, path="candidate profile")
    _assert_no_secret_fields(trusted, path="trusted profile")
    expected_id = _require_profile_id(profile_id)
    source = _require_sha(source_sha, "source SHA")
    trusted_sha = _require_sha(trusted_runner_sha, "trusted runner SHA")
    expected_run = _require_id_text(run_id, "run ID", POSITIVE_ID_RE)
    expected_attempt = _require_id_text(run_attempt, "run attempt", ATTEMPT_RE)
    candidate_id = _require_profile_id(candidate.get("profile_id"), "candidate profile ID")
    trusted_id = _require_profile_id(trusted.get("profile_id"), "trusted profile ID")
    if candidate_id != expected_id or trusted_id != expected_id or candidate_id != trusted_id:
        raise ExternalLoadProvenanceError("profile ID is not exact")
    candidate_version = _require_profile_version(candidate.get("profile_version"))
    trusted_version = _require_profile_version(trusted.get("profile_version"))
    if candidate_version != trusted_version:
        raise ExternalLoadProvenanceError("profile version is not approved by trusted runner")
    candidate_digest = _payload_digest(candidate)
    trusted_digest = _payload_digest(trusted)
    if candidate_digest != trusted_digest:
        raise ExternalLoadProvenanceError("candidate profile digest is not approved by trusted runner")
    execution_budget_minutes = _profile_execution_budget(trusted)
    fixture = trusted.get("fixture")
    if not isinstance(fixture, Mapping):
        raise ExternalLoadProvenanceError("trusted profile fixture is missing")
    fixture_keys = {"tournament_count", "users_per_tournament", "setup_concurrency", "max_total_users"}
    if set(fixture) != fixture_keys:
        raise ExternalLoadProvenanceError("trusted profile fixture schema is not closed")
    for key in fixture_keys:
        value = fixture[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000:
            raise ExternalLoadProvenanceError("trusted profile fixture value is malformed")
    return {
        "schema": 1,
        "profile_id": expected_id,
        "profile_version": trusted_version,
        "profile_digest": trusted_digest,
        "source_git_sha": source,
        "trusted_runner_sha": trusted_sha,
        "run_id": expected_run,
        "run_attempt": expected_attempt,
        "execution_budget_minutes": execution_budget_minutes,
        "fixture": {key: int(fixture[key]) for key in sorted(fixture_keys)},
    }


def validate_profile_contract(
    payload: Any,
    *,
    profile_id: Any,
    source_sha: Any,
    trusted_runner_sha: Any,
    run_id: Any,
    run_attempt: Any,
    trusted_profile: Any | None = None,
) -> dict[str, Any]:
    """Validate a non-secret profile contract between workflow jobs."""

    if not isinstance(payload, Mapping):
        raise ExternalLoadProvenanceError("profile contract is not an object")
    _assert_no_secret_fields(payload, path="profile contract")
    if trusted_profile is None:
        expected_id = _require_profile_id(profile_id)
        expected_source = _require_sha(source_sha, "source SHA")
        expected_trusted = _require_sha(trusted_runner_sha, "trusted runner SHA")
        expected_run = _require_id_text(run_id, "run ID", POSITIVE_ID_RE)
        expected_attempt = _require_id_text(run_attempt, "run attempt", ATTEMPT_RE)
        expected_keys = {
            "schema",
            "profile_id",
            "profile_version",
            "profile_digest",
            "source_git_sha",
            "trusted_runner_sha",
            "run_id",
            "run_attempt",
            "execution_budget_minutes",
            "fixture",
        }
        if set(payload) != expected_keys or payload.get("schema") != 1:
            raise ExternalLoadProvenanceError("profile contract schema is not closed")
        fixture = payload.get("fixture")
        if not isinstance(fixture, Mapping) or set(fixture) != {
            "max_total_users",
            "setup_concurrency",
            "tournament_count",
            "users_per_tournament",
        }:
            raise ExternalLoadProvenanceError("profile contract fixture schema is not closed")
        expected = {
            "schema": 1,
            "profile_id": expected_id,
            "profile_version": _require_profile_version(payload.get("profile_version")),
            "profile_digest": normalize_artifact_digest(payload.get("profile_digest")),
            "source_git_sha": expected_source,
            "trusted_runner_sha": expected_trusted,
            "run_id": expected_run,
            "run_attempt": expected_attempt,
            "execution_budget_minutes": payload.get("execution_budget_minutes"),
            "fixture": {},
        }
        budget = expected["execution_budget_minutes"]
        if isinstance(budget, bool) or not isinstance(budget, int) or not 15 <= budget <= 60:
            raise ExternalLoadProvenanceError("profile contract execution budget is malformed")
        for key in fixture:
            value = fixture[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000:
                raise ExternalLoadProvenanceError("profile contract fixture value is malformed")
            expected["fixture"][key] = value  # type: ignore[index]
        if dict(payload) != expected:
            raise ExternalLoadProvenanceError("profile contract identity is not exact")
        return expected
    # A contract is not itself a profile: it carries the profile's digest and
    # the closed fixture values plus the run binding.  Build the expected
    # contract from the pinned trusted profile, then compare the handoff byte
    # for byte.  Treating the contract as a profile here would reject every
    # valid cross-job handoff (and would make the trusted profile check
    # accidentally depend on candidate-controlled fields).
    expected = validate_profile_data(
        trusted_profile,
        trusted_profile,
        profile_id=profile_id,
        source_sha=source_sha,
        trusted_runner_sha=trusted_runner_sha,
        run_id=run_id,
        run_attempt=run_attempt,
    )
    contract_keys = set(expected)
    if set(payload) != contract_keys:
        raise ExternalLoadProvenanceError("profile contract schema is not closed")
    if dict(payload) != expected:
        raise ExternalLoadProvenanceError("profile contract identity is not exact")
    return expected


def validate_artifact_metadata(
    payload: Any,
    *,
    artifact_id: Any,
    artifact_name: Any,
    run_id: Any,
    run_attempt: Any,
    target_sha: Any,
    artifact_digest: Any,
    repository: Any,
    workflow_file: Any,
    run_metadata: Any | None = None,
) -> dict[str, Any]:
    """Validate one GitHub artifact API response and its expected digest.

    The artifact name, ID, run ID/attempt, source SHA, repository, workflow
    path, bounded size and unexpired state are mandatory and exact.
    """

    if not isinstance(payload, Mapping):
        raise ExternalLoadProvenanceError("artifact metadata is not an object")
    expected_id = _require_id_text(artifact_id, "artifact ID", POSITIVE_ID_RE)
    expected_run = _require_id_text(run_id, "run ID", POSITIVE_ID_RE)
    expected_attempt = _require_id_text(run_attempt, "run attempt", ATTEMPT_RE)
    expected_name = (
        artifact_name
        if isinstance(artifact_name, str) and artifact_name
        else None
    )
    if expected_name is None:
        raise ExternalLoadProvenanceError("artifact name is malformed")
    expected_sha = _require_sha(target_sha, "target SHA")
    expected_digest = normalize_artifact_digest(artifact_digest)
    if not isinstance(repository, str) or REPOSITORY_RE.fullmatch(repository) is None:
        raise ExternalLoadProvenanceError("artifact repository is malformed")
    if (
        not isinstance(workflow_file, str)
        or WORKFLOW_FILE_RE.fullmatch(workflow_file) is None
        or workflow_file != CALLER_WORKFLOW_FILE
    ):
        raise ExternalLoadProvenanceError("artifact workflow file is malformed")

    actual_id = _require_positive_id(payload.get("id"), "artifact metadata ID")
    if str(actual_id) != expected_id:
        raise ExternalLoadProvenanceError("artifact ID does not match handoff")
    if payload.get("name") != expected_name:
        raise ExternalLoadProvenanceError("artifact name does not match handoff")
    if payload.get("expired") is not False:
        raise ExternalLoadProvenanceError("artifact is expired or malformed")
    size_in_bytes = payload.get("size_in_bytes")
    if (
        isinstance(size_in_bytes, bool)
        or not isinstance(size_in_bytes, int)
        or not 1 <= size_in_bytes <= MAX_ARTIFACT_ARCHIVE_BYTES
    ):
        raise ExternalLoadProvenanceError("artifact size is missing or exceeds its bound")
    api_digest = payload.get("digest")
    if api_digest is not None and normalize_artifact_digest(api_digest) != expected_digest:
        raise ExternalLoadProvenanceError("artifact API digest does not match handoff")

    artifact_workflow_run = payload.get("workflow_run")
    if not isinstance(artifact_workflow_run, Mapping):
        raise ExternalLoadProvenanceError("artifact workflow identity is missing")
    # The artifact and workflow-run endpoints are separate API responses.  A
    # missing field may be supplied by the independently fetched run record,
    # but never by rewriting the artifact response in a shell step.  Compare
    # every overlapping field before using the run record as the authority for
    # the complete caller identity.
    workflow_run: Mapping[str, Any] = artifact_workflow_run
    if run_metadata is not None:
        if not isinstance(run_metadata, Mapping):
            raise ExternalLoadProvenanceError("workflow run metadata is not an object")
        for field in ("id", "head_sha", "run_attempt"):
            if field in artifact_workflow_run and artifact_workflow_run.get(field) != run_metadata.get(field):
                raise ExternalLoadProvenanceError("artifact and workflow-run identities differ")
        artifact_repository = artifact_workflow_run.get("head_repository")
        run_repository = run_metadata.get("head_repository")
        if artifact_repository is not None and artifact_repository != run_repository:
            raise ExternalLoadProvenanceError("artifact and workflow-run repositories differ")
        artifact_path = artifact_workflow_run.get("path") or artifact_workflow_run.get("workflow_file_name")
        run_path = run_metadata.get("path") or run_metadata.get("workflow_file_name")
        if artifact_path is not None and artifact_path != run_path:
            raise ExternalLoadProvenanceError("artifact and workflow-run workflow paths differ")
        workflow_run = run_metadata
    workflow_id = _require_positive_id(workflow_run.get("id"), "workflow run ID")
    if str(workflow_id) != expected_run:
        raise ExternalLoadProvenanceError("artifact workflow run does not match handoff")
    if workflow_run.get("head_sha") != expected_sha:
        raise ExternalLoadProvenanceError("artifact workflow SHA does not match handoff")
    actual_attempt = _require_positive_id(
        workflow_run.get("run_attempt"),
        "workflow run attempt",
    )
    if str(actual_attempt) != expected_attempt:
        raise ExternalLoadProvenanceError(
            "artifact workflow attempt does not match handoff"
        )
    head_repository = workflow_run.get("head_repository")
    if (
        not isinstance(head_repository, Mapping)
        or head_repository.get("full_name") != repository
    ):
        raise ExternalLoadProvenanceError("artifact repository does not match handoff")
    actual_workflow_file = workflow_run.get("path") or workflow_run.get("workflow_file_name")
    if actual_workflow_file != workflow_file:
        raise ExternalLoadProvenanceError("artifact workflow file does not match handoff")
    return {
        "artifact_id": actual_id,
        "artifact_name": expected_name,
        "run_id": expected_run,
        "run_attempt": expected_attempt,
        "target_sha": expected_sha,
        "artifact_digest": expected_digest,
        "size_in_bytes": size_in_bytes,
        "repository": repository,
        "workflow_file": workflow_file,
    }


def validate_artifact_archive(
    metadata: Any,
    archive_path: Path,
    **expected: Any,
) -> dict[str, Any]:
    """Validate API metadata and the exact downloaded archive bytes."""

    accepted = validate_artifact_metadata(metadata, **expected)
    try:
        descriptor = os.open(archive_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ExternalLoadProvenanceError("artifact archive cannot be opened safely") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        import stat

        if not stat.S_ISREG(before.st_mode):
            raise ExternalLoadProvenanceError("artifact archive is not a regular file")
        if before.st_size != accepted["size_in_bytes"]:
            raise ExternalLoadProvenanceError("artifact archive size does not match API metadata")
        if before.st_size > MAX_ARTIFACT_ARCHIVE_BYTES:
            raise ExternalLoadProvenanceError("artifact archive exceeds its size bound")
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ExternalLoadProvenanceError("artifact archive changed while being read")
    except OSError as exc:
        raise ExternalLoadProvenanceError("artifact archive cannot be read safely") from exc
    finally:
        os.close(descriptor)
    if digest.hexdigest() != accepted["artifact_digest"]:
        raise ExternalLoadProvenanceError("artifact archive bytes do not match API digest")
    _validate_artifact_zip(archive_path, artifact_name=accepted["artifact_name"])
    return accepted


def _artifact_member_allowlist(artifact_name: str) -> frozenset[str]:
    if re.fullmatch(r"platform-production-external-load-input-[1-9][0-9]{0,31}-[1-9][0-9]{0,8}", artifact_name):
        return frozenset(
            {
                "platform-production-external-load-input.json",
                "platform-production-external-load-profile-contract.json",
            }
        )
    if re.fullmatch(r"platform-production-external-load-client-[1-9][0-9]{0,31}-[1-9][0-9]{0,8}", artifact_name):
        return frozenset(
            {
                "external-load.json",
                "load-status.json",
                "timeout-diagnostic-ids.json",
            }
        )
    if re.fullmatch(r"platform-production-external-load-origin-[1-9][0-9]{0,31}-[1-9][0-9]{0,8}", artifact_name):
        return frozenset(
            {
                "matrix-summary.json",
                "canonical.log",
                "server-observability.json",
                "timeout-diagnostics.json",
                "cleanup-summary.json",
                "cleanup-canonical.log",
            }
        )
    raise ExternalLoadProvenanceError("artifact name has no trusted member allowlist")


def _validate_artifact_zip(archive_path: Path, *, artifact_name: str) -> None:
    allowed = _artifact_member_allowlist(artifact_name)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            if not 1 <= len(infos) <= MAX_ARTIFACT_MEMBERS:
                raise ExternalLoadProvenanceError("artifact archive member count is outside its bound")
            seen: set[str] = set()
            expanded = 0
            for info in infos:
                name = info.filename
                path = PurePosixPath(name)
                if (
                    not name
                    or name.endswith("/")
                    or name in seen
                    or path.is_absolute()
                    or "\\" in name
                    or ".." in path.parts
                    or path.name != name
                    or name not in allowed
                ):
                    raise ExternalLoadProvenanceError("artifact archive contains an unsafe member")
                mode = (info.external_attr >> 16) & 0o170000
                if stat.S_ISLNK(mode) or stat.S_ISDIR(mode):
                    raise ExternalLoadProvenanceError("artifact archive contains a link or directory")
                if info.file_size > MAX_ARTIFACT_MEMBER_BYTES:
                    raise ExternalLoadProvenanceError("artifact archive member exceeds its bound")
                expanded += info.file_size
                if expanded > MAX_ARTIFACT_EXPANDED_BYTES:
                    raise ExternalLoadProvenanceError("artifact archive expanded size exceeds its bound")
                if info.file_size and info.compress_size == 0:
                    raise ExternalLoadProvenanceError("artifact archive compression metadata is invalid")
                seen.add(name)
            if seen != allowed:
                raise ExternalLoadProvenanceError("artifact archive member allowlist is incomplete")
    except ExternalLoadProvenanceError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise ExternalLoadProvenanceError("artifact archive is not a valid bounded ZIP") from exc


def validate_load_status(
    payload: Any,
    *,
    target_sha: Any,
    run_id: Any,
    run_attempt: Any,
    trusted_runner_sha: Any | None = None,
    profile_id: Any | None = None,
    profile_version: Any | None = None,
    profile_digest: Any | None = None,
) -> dict[str, Any]:
    """Validate the success-bearing load-client status handoff."""

    if not isinstance(payload, Mapping):
        raise ExternalLoadProvenanceError("load status is not an object")
    expected_keys = {
        "schema",
        "status",
        "report_ready",
        "target_sha",
        "run_id",
        "run_attempt",
    }
    full_binding = any(
        value is not None
        for value in (trusted_runner_sha, profile_id, profile_version, profile_digest)
    )
    if full_binding:
        if any(value is None for value in (trusted_runner_sha, profile_id, profile_version, profile_digest)):
            raise ExternalLoadProvenanceError("load status binding is incomplete")
        expected_keys |= {
            "trusted_runner_sha",
            "profile_id",
            "profile_version",
            "profile_digest",
        }
    if set(payload) != expected_keys:
        raise ExternalLoadProvenanceError("load status schema is not closed")
    expected_sha = _require_sha(target_sha, "target SHA")
    expected_run = _require_id_text(run_id, "run ID", POSITIVE_ID_RE)
    expected_attempt = _require_id_text(run_attempt, "run attempt", ATTEMPT_RE)
    if payload.get("schema") != 1:
        raise ExternalLoadProvenanceError("load status schema version is invalid")
    if isinstance(payload.get("status"), bool) or payload.get("status") != 0:
        raise ExternalLoadProvenanceError("load status is not successful")
    if payload.get("report_ready") is not True:
        raise ExternalLoadProvenanceError("load report is not ready")
    if payload.get("target_sha") != expected_sha:
        raise ExternalLoadProvenanceError("load status SHA does not match handoff")
    if payload.get("run_id") != expected_run:
        raise ExternalLoadProvenanceError("load status run ID does not match handoff")
    if payload.get("run_attempt") != expected_attempt:
        raise ExternalLoadProvenanceError(
            "load status run attempt does not match handoff"
        )
    result = {
        "schema": 1,
        "status": 0,
        "report_ready": True,
        "target_sha": expected_sha,
        "run_id": expected_run,
        "run_attempt": expected_attempt,
    }
    if full_binding:
        result.update(
            {
                "trusted_runner_sha": _require_sha(trusted_runner_sha, "trusted runner SHA"),
                "profile_id": _require_profile_id(profile_id),
                "profile_version": _require_profile_version(profile_version),
                "profile_digest": normalize_artifact_digest(profile_digest),
            }
        )
        if payload.get("trusted_runner_sha") != result["trusted_runner_sha"]:
            raise ExternalLoadProvenanceError("load status trusted runner is not exact")
        if payload.get("profile_id") != result["profile_id"]:
            raise ExternalLoadProvenanceError("load status profile ID is not exact")
        if payload.get("profile_version") != result["profile_version"]:
            raise ExternalLoadProvenanceError("load status profile version is not exact")
        if payload.get("profile_digest") != result["profile_digest"]:
            raise ExternalLoadProvenanceError("load status profile digest is not exact")
    return result


def validate_report_provenance(
    payload: Any,
    *,
    target_sha: Any,
    run_id: Any,
    run_attempt: Any | None = None,
    trusted_runner_sha: Any | None = None,
    profile_id: Any | None = None,
    profile_version: Any | None = None,
    profile_digest: Any | None = None,
) -> dict[str, Any]:
    """Validate the candidate report's source/run identity before evaluation."""

    if not isinstance(payload, Mapping):
        raise ExternalLoadProvenanceError("load report is not an object")
    _assert_report_schema(payload)
    expected_sha = _require_sha(target_sha, "target SHA")
    expected_run = _require_id_text(run_id, "run ID", POSITIVE_ID_RE)
    full_binding = any(
        value is not None
        for value in (run_attempt, trusted_runner_sha, profile_id, profile_version, profile_digest)
    )
    if full_binding and any(
        value is None
        for value in (run_attempt, trusted_runner_sha, profile_id, profile_version, profile_digest)
    ):
        raise ExternalLoadProvenanceError("report provenance binding is incomplete")
    if payload.get("source_git_sha") != expected_sha:
        raise ExternalLoadProvenanceError("load report source SHA does not match")
    if payload.get("external_run_id") != expected_run:
        raise ExternalLoadProvenanceError("load report run ID does not match")
    if payload.get("authoritative") is not True or payload.get("dispatchable") is not True:
        raise ExternalLoadProvenanceError("load report is not authoritative")
    result = {"source_git_sha": expected_sha, "external_run_id": expected_run}
    if full_binding:
        expected_attempt = _require_id_text(run_attempt, "run attempt", ATTEMPT_RE)
        result.update(
            {
                "external_run_attempt": expected_attempt,
                "trusted_runner_sha": _require_sha(trusted_runner_sha, "trusted runner SHA"),
                "profile_id": _require_profile_id(profile_id),
                "profile_version": _require_profile_version(profile_version),
                "profile_digest": normalize_artifact_digest(profile_digest),
            }
        )
        for key, value in result.items():
            if payload.get(key) != value:
                raise ExternalLoadProvenanceError(f"load report {key} is not exact")
    _assert_no_secret_fields(payload, path="load report")
    return result


def attach_report_provenance(
    payload: Any,
    *,
    target_sha: Any,
    run_id: Any,
    run_attempt: Any,
    trusted_runner_sha: Any,
    profile_id: Any,
    profile_version: Any,
    profile_digest: Any,
) -> dict[str, Any]:
    """Add the closed trusted-runner binding to a trusted load report."""

    if not isinstance(payload, Mapping):
        raise ExternalLoadProvenanceError("load report is not an object")
    _assert_no_secret_fields(payload, path="load report")
    if payload.get("source_git_sha") != target_sha or payload.get("external_run_id") != run_id:
        raise ExternalLoadProvenanceError("load report source/run identity is not exact")
    result = dict(payload)
    binding = validate_report_provenance(
        {
            **result,
            "external_run_attempt": run_attempt,
            "trusted_runner_sha": trusted_runner_sha,
            "profile_id": profile_id,
            "profile_version": profile_version,
            "profile_digest": profile_digest,
        },
        target_sha=target_sha,
        run_id=run_id,
        run_attempt=run_attempt,
        trusted_runner_sha=trusted_runner_sha,
        profile_id=profile_id,
        profile_version=profile_version,
        profile_digest=profile_digest,
    )
    result.update(binding)
    return result


def build_load_status(
    *,
    status: Any,
    report_ready: Any,
    target_sha: Any,
    run_id: Any,
    run_attempt: Any,
    trusted_runner_sha: Any,
    profile_id: Any,
    profile_version: Any,
    profile_digest: Any,
) -> dict[str, Any]:
    if isinstance(status, bool) or not isinstance(status, int) or not 0 <= status <= 255:
        raise ExternalLoadProvenanceError("load status is malformed")
    if report_ready is not True and status == 0:
        raise ExternalLoadProvenanceError("successful load status requires a report")
    return {
        "schema": 1,
        "status": status,
        "report_ready": report_ready is True,
        "target_sha": _require_sha(target_sha, "target SHA"),
        "run_id": _require_id_text(run_id, "run ID", POSITIVE_ID_RE),
        "run_attempt": _require_id_text(run_attempt, "run attempt", ATTEMPT_RE),
        "trusted_runner_sha": _require_sha(trusted_runner_sha, "trusted runner SHA"),
        "profile_id": _require_profile_id(profile_id),
        "profile_version": _require_profile_version(profile_version),
        "profile_digest": normalize_artifact_digest(profile_digest),
    }


FINAL_PROVENANCE_FIELDS = frozenset(
    {
        "schema",
        "source_git_sha",
        "trusted_workflow_file",
        "trusted_runner_sha",
        "profile_id",
        "profile_version",
        "profile_digest",
        "run_id",
        "run_attempt",
        "repository",
        "workflow_file",
        "artifacts",
    }
)
FINAL_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_id",
        "artifact_name",
        "artifact_digest",
        "size_in_bytes",
        "repository",
        "workflow_file",
    }
)


def build_final_provenance(
    artifacts: Any,
    *,
    source_sha: Any,
    trusted_workflow_file: Any = TRUSTED_WORKFLOW_FILE,
    trusted_runner_sha: Any,
    profile_id: Any,
    profile_version: Any,
    profile_digest: Any,
    run_id: Any,
    run_attempt: Any,
    repository: Any,
    workflow_file: Any,
) -> dict[str, Any]:
    """Build the non-secret final provenance binding for published evidence.

    Artifact identity is recorded only after each API metadata/archive
    validator has accepted it.  The resulting closed document is safe to
    publish: it contains IDs, digests, sizes and workflow identities, never
    an artifact URL, token, cookie, raw response, or SSH material.
    """

    expected_source = _require_sha(source_sha, "source SHA")
    expected_trusted = _require_sha(trusted_runner_sha, "trusted runner SHA")
    expected_profile = _require_profile_id(profile_id)
    expected_version = _require_profile_version(profile_version)
    expected_digest = normalize_artifact_digest(profile_digest)
    expected_run = _require_id_text(run_id, "run ID", POSITIVE_ID_RE)
    expected_attempt = _require_id_text(run_attempt, "run attempt", ATTEMPT_RE)
    if trusted_workflow_file != TRUSTED_WORKFLOW_FILE:
        raise ExternalLoadProvenanceError("trusted workflow file is not the reviewed runner")
    if not isinstance(repository, str) or REPOSITORY_RE.fullmatch(repository) is None:
        raise ExternalLoadProvenanceError("provenance repository is malformed")
    if (
        not isinstance(workflow_file, str)
        or WORKFLOW_FILE_RE.fullmatch(workflow_file) is None
        or workflow_file != CALLER_WORKFLOW_FILE
    ):
        raise ExternalLoadProvenanceError("provenance workflow file is malformed")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 4:
        raise ExternalLoadProvenanceError("provenance artifact list is malformed")
    normalized_artifacts: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or set(artifact) != FINAL_ARTIFACT_FIELDS:
            raise ExternalLoadProvenanceError("provenance artifact record is not closed")
        artifact_id = _require_positive_id(artifact.get("artifact_id"), "provenance artifact ID")
        if artifact_id in seen_ids:
            raise ExternalLoadProvenanceError("provenance artifact IDs are duplicated")
        seen_ids.add(artifact_id)
        artifact_name = artifact.get("artifact_name")
        if not isinstance(artifact_name, str) or not re.fullmatch(
            r"platform-production-external-load-(?:input|client|origin)-[1-9][0-9]{0,31}-[1-9][0-9]{0,8}",
            artifact_name,
        ):
            raise ExternalLoadProvenanceError("provenance artifact name is malformed")
        artifact_repository = artifact.get("repository")
        artifact_workflow = artifact.get("workflow_file")
        if artifact_repository != repository or artifact_workflow != workflow_file:
            raise ExternalLoadProvenanceError("provenance artifact workflow identity is not exact")
        size = artifact.get("size_in_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_ARTIFACT_ARCHIVE_BYTES:
            raise ExternalLoadProvenanceError("provenance artifact size is malformed")
        normalized_artifacts.append(
            {
                "artifact_id": artifact_id,
                "artifact_name": artifact_name,
                "artifact_digest": normalize_artifact_digest(artifact.get("artifact_digest")),
                "size_in_bytes": size,
                "repository": repository,
                "workflow_file": workflow_file,
            }
        )
    return {
        "schema": 1,
        "source_git_sha": expected_source,
        "trusted_workflow_file": TRUSTED_WORKFLOW_FILE,
        "trusted_runner_sha": expected_trusted,
        "profile_id": expected_profile,
        "profile_version": expected_version,
        "profile_digest": expected_digest,
        "run_id": expected_run,
        "run_attempt": expected_attempt,
        "repository": repository,
        "workflow_file": workflow_file,
        "artifacts": normalized_artifacts,
    }


def validate_final_provenance(payload: Any, **expected: Any) -> dict[str, Any]:
    """Validate a final provenance document against its exact run binding."""

    if not isinstance(payload, Mapping) or set(payload) != FINAL_PROVENANCE_FIELDS:
        raise ExternalLoadProvenanceError("final provenance schema is not closed")
    normalized = build_final_provenance(payload.get("artifacts"), **expected)
    if dict(payload) != normalized:
        raise ExternalLoadProvenanceError("final provenance identity is not exact")
    return normalized


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.write(descriptor, _canonical_json(payload))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except (OSError, TypeError) as exc:
        raise ExternalLoadProvenanceError("JSON output cannot be written safely") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    artifact = subparsers.add_parser("artifact")
    artifact.add_argument("--metadata", type=Path, required=True)
    artifact.add_argument("--artifact-id", required=True)
    artifact.add_argument("--artifact-name", required=True)
    artifact.add_argument("--run-id", required=True)
    artifact.add_argument("--run-attempt", required=True)
    artifact.add_argument("--target-sha", required=True)
    artifact.add_argument("--digest", required=True)
    artifact.add_argument("--repository", required=True)
    artifact.add_argument("--workflow-file", required=True)
    artifact.add_argument("--run-metadata", type=Path)
    artifact.add_argument("--archive", type=Path, required=True)

    profile = subparsers.add_parser("profile")
    profile.add_argument("--candidate", type=Path, required=True)
    profile.add_argument("--trusted-profile", type=Path, required=True)
    profile.add_argument("--profile-id", required=True)
    profile.add_argument("--source-sha", required=True)
    profile.add_argument("--trusted-sha", required=True)
    profile.add_argument("--run-id", required=True)
    profile.add_argument("--run-attempt", required=True)
    profile.add_argument("--output", type=Path, required=True)

    contract = subparsers.add_parser("profile-contract")
    contract.add_argument("--path", type=Path, required=True)
    contract.add_argument("--profile-id", required=True)
    contract.add_argument("--source-sha", required=True)
    contract.add_argument("--trusted-sha", required=True)
    contract.add_argument("--run-id", required=True)
    contract.add_argument("--run-attempt", required=True)
    contract.add_argument("--trusted-profile", type=Path)

    status = subparsers.add_parser("load-status")
    status.add_argument("--path", type=Path, required=True)
    status.add_argument("--target-sha", required=True)
    status.add_argument("--run-id", required=True)
    status.add_argument("--run-attempt", required=True)
    status.add_argument("--trusted-sha")
    status.add_argument("--profile-id")
    status.add_argument("--profile-version", type=int)
    status.add_argument("--profile-digest")

    emit_status = subparsers.add_parser("emit-status")
    emit_status.add_argument("--output", type=Path, required=True)
    emit_status.add_argument("--status", type=int, required=True)
    emit_status.add_argument("--report-ready", type=int, required=True)
    emit_status.add_argument("--target-sha", required=True)
    emit_status.add_argument("--run-id", required=True)
    emit_status.add_argument("--run-attempt", required=True)
    emit_status.add_argument("--trusted-sha", required=True)
    emit_status.add_argument("--profile-id", required=True)
    emit_status.add_argument("--profile-version", type=int, required=True)
    emit_status.add_argument("--profile-digest", required=True)

    report = subparsers.add_parser("report")
    report.add_argument("--path", type=Path, required=True)
    report.add_argument("--target-sha", required=True)
    report.add_argument("--run-id", required=True)
    report.add_argument("--run-attempt")
    report.add_argument("--trusted-sha")
    report.add_argument("--profile-id")
    report.add_argument("--profile-version", type=int)
    report.add_argument("--profile-digest")

    attach = subparsers.add_parser("attach-report")
    attach.add_argument("--input", type=Path, required=True)
    attach.add_argument("--output", type=Path, required=True)
    attach.add_argument("--target-sha", required=True)
    attach.add_argument("--run-id", required=True)
    attach.add_argument("--run-attempt", required=True)
    attach.add_argument("--trusted-sha", required=True)
    attach.add_argument("--profile-id", required=True)
    attach.add_argument("--profile-version", type=int, required=True)
    attach.add_argument("--profile-digest", required=True)

    provenance = subparsers.add_parser("provenance")
    provenance.add_argument("--artifacts", type=Path, required=True)
    provenance.add_argument("--output", type=Path, required=True)
    provenance.add_argument("--target-sha", required=True)
    provenance.add_argument("--trusted-workflow-file", required=True)
    provenance.add_argument("--trusted-sha", required=True)
    provenance.add_argument("--profile-id", required=True)
    provenance.add_argument("--profile-version", type=int, required=True)
    provenance.add_argument("--profile-digest", required=True)
    provenance.add_argument("--run-id", required=True)
    provenance.add_argument("--run-attempt", required=True)
    provenance.add_argument("--repository", required=True)
    provenance.add_argument("--workflow-file", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "artifact":
            if (
                args.metadata.is_symlink()
                or not args.metadata.is_file()
                or args.metadata.stat().st_size > MAX_ARTIFACT_METADATA_BYTES
                or args.archive.is_symlink()
                or not args.archive.is_file()
                or args.archive.stat().st_size > MAX_ARTIFACT_ARCHIVE_BYTES
            ):
                raise ExternalLoadProvenanceError("artifact handoff file exceeds its bound")
            validate_artifact_archive(
                _read_json(args.metadata),
                args.archive,
                artifact_id=args.artifact_id,
                artifact_name=args.artifact_name,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                target_sha=args.target_sha,
                artifact_digest=args.digest,
                repository=args.repository,
                workflow_file=args.workflow_file,
                run_metadata=(
                    _read_json(args.run_metadata)
                    if args.run_metadata is not None
                    else None
                ),
            )
        elif args.command == "profile":
            candidate = validate_profile_data(
                _read_json(args.candidate),
                _read_json(args.trusted_profile),
                profile_id=args.profile_id,
                source_sha=args.source_sha,
                trusted_runner_sha=args.trusted_sha,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
            )
            _write_json(args.output, candidate)
        elif args.command == "profile-contract":
            validate_profile_contract(
                _read_json(args.path),
                profile_id=args.profile_id,
                source_sha=args.source_sha,
                trusted_runner_sha=args.trusted_sha,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                trusted_profile=(
                    _read_json(args.trusted_profile)
                    if args.trusted_profile is not None
                    else None
                ),
            )
        elif args.command == "load-status":
            validate_load_status(
                _read_json(args.path),
                target_sha=args.target_sha,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                trusted_runner_sha=args.trusted_sha,
                profile_id=args.profile_id,
                profile_version=args.profile_version,
                profile_digest=args.profile_digest,
            )
        elif args.command == "emit-status":
            _write_json(
                args.output,
                build_load_status(
                    status=args.status,
                    report_ready=args.report_ready == 1,
                    target_sha=args.target_sha,
                    run_id=args.run_id,
                    run_attempt=args.run_attempt,
                    trusted_runner_sha=args.trusted_sha,
                    profile_id=args.profile_id,
                    profile_version=args.profile_version,
                    profile_digest=args.profile_digest,
                ),
            )
        elif args.command == "attach-report":
            _write_json(
                args.output,
                attach_report_provenance(
                    _read_json(args.input),
                    target_sha=args.target_sha,
                    run_id=args.run_id,
                    run_attempt=args.run_attempt,
                    trusted_runner_sha=args.trusted_sha,
                    profile_id=args.profile_id,
                    profile_version=args.profile_version,
                    profile_digest=args.profile_digest,
                ),
            )
        elif args.command == "provenance":
            _write_json(
                args.output,
                build_final_provenance(
                    _read_json(args.artifacts),
                    source_sha=args.target_sha,
                    trusted_workflow_file=args.trusted_workflow_file,
                    trusted_runner_sha=args.trusted_sha,
                    profile_id=args.profile_id,
                    profile_version=args.profile_version,
                    profile_digest=args.profile_digest,
                    run_id=args.run_id,
                    run_attempt=args.run_attempt,
                    repository=args.repository,
                    workflow_file=args.workflow_file,
                ),
            )
        else:
            validate_report_provenance(
                _read_json(args.path),
                target_sha=args.target_sha,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                trusted_runner_sha=args.trusted_sha,
                profile_id=args.profile_id,
                profile_version=args.profile_version,
                profile_digest=args.profile_digest,
            )
    except ExternalLoadProvenanceError as exc:
        print(f"external-load provenance validation failed: {exc}")
        return 1
    print("external-load provenance accepted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
