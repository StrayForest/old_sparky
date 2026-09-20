#!/usr/bin/env python3
"""Validate and serialize workflow values crossing the production SSH boundary.

Production workflow entry points accept values from ``workflow_dispatch``.
This module is intentionally dependency-free so the runner's system Python and
the production system Python can apply the same closed-world parser.  It never
prints a rejected value: callers receive only a stable error class.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Mapping


MAX_INPUT_BYTES = 64 * 1024
MAX_EMAIL_LENGTH = 254
MAX_EMAIL_LOCAL_LENGTH = 64
MAX_EMAIL_DOMAIN_LENGTH = 253
MAX_LIVE_MARKER_LENGTH = 63
EXPECTED_ORIGIN = "https://old-sparky.com"
EXTERNAL_CONFIRMATION = "RUN-PRODUCTION-EXTERNAL-LOAD"
TIMEOUT_DIAGNOSTICS_CONFIRMATION = "RUN-PRODUCTION-TIMEOUT-DIAGNOSTICS"
DELETE_CONFIRMATION = "DELETE-PRODUCTION-RETAINED-LOAD"
CONFIRMATION_PHRASES = frozenset(
    {
        EXTERNAL_CONFIRMATION,
        TIMEOUT_DIAGNOSTICS_CONFIRMATION,
        DELETE_CONFIRMATION,
        "RUN-LIVE-USER-QA",
        "RUN-PRODUCTION-PROFILE-REVIEW",
        "ABORT-PRODUCTION-RETAINED-LOAD",
        "ABORT-RETAINED-RELEASE-MIGRATION-NOT-REVERSED",
        "RECOVER-PENDING-RELEASE",
        "RECOVER-DEADLOCK-WEB",
        "APPLY-PRODUCTION-STORAGE-MAINTENANCE",
    }
)
DEPLOY_MODES = frozenset({"preflight", "deploy"})
DEPLOY_RUNTIME_PROFILES = frozenset(
    {
        "baseline",
        "ready-vote-static-4",
        "ready-vote-static-6",
        "ready-vote-static-8",
        "ready-vote-cprofile",
        "ready-vote-static-12",
        "ready-vote-static-16",
        "ready-vote-adaptive-v2",
        "api-3x16",
        "api-1x48",
        "read-mix-cprofile",
        "authenticated-read-admission-32",
        "authenticated-read-admission-24x8",
        "pool-pre-ping-off",
        "web-ssr-diagnostics",
        "web-ssr-native-transport",
        "web-ssr-workers-2",
        "uvicorn-classic",
        "uvicorn-optimized",
        "api-pool-12",
        "api-pool-16",
        "api-pool-20",
        "api-pool-24",
    }
)

# Keep the workflow identity intentionally narrower than the full RFC mailbox
# grammar.  In particular, shell punctuation, whitespace and option-like
# prefixes are not accepted at this trust boundary.
EMAIL_LOCAL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._%+\-]{0,62}[A-Za-z0-9])?$")
EMAIL_DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$")
LIVE_MARKER_RE = re.compile(r"^liveqa-[a-z0-9-]{6,56}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
POSITIVE_INTEGER_RE = re.compile(r"^[1-9][0-9]*$")
UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
RELEASE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
ARTIFACT_REMOTE_DIR_RE = re.compile(
    r"^/tmp/old-sparky-platform-artifact-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}$"
)


class WorkflowInputError(ValueError):
    """A deliberately detail-free input validation failure."""


def _invalid() -> WorkflowInputError:
    return WorkflowInputError("workflow input is invalid")


class _WorkflowInputArgumentParser(argparse.ArgumentParser):
    """Reject malformed CLI syntax without echoing attacker-controlled text."""

    def error(self, _message: str) -> None:
        raise _invalid()


def validate_control_email(value: object) -> str:
    """Return the canonical lower-case control email or raise safely.

    No whitespace trimming is performed: invisible leading/trailing characters
    must fail rather than changing the account identity that cleanup protects.
    """

    if not isinstance(value, str) or not value.isascii():
        raise _invalid()
    if not 3 <= len(value) <= MAX_EMAIL_LENGTH or value != value.strip():
        raise _invalid()
    if any(ord(character) < 0x21 or ord(character) == 0x7F for character in value):
        raise _invalid()
    if value.count("@") != 1:
        raise _invalid()
    local, domain = value.split("@", 1)
    if not 1 <= len(local) <= MAX_EMAIL_LOCAL_LENGTH:
        raise _invalid()
    if not 1 <= len(domain) <= MAX_EMAIL_DOMAIN_LENGTH or "." not in domain:
        raise _invalid()
    if EMAIL_LOCAL_RE.fullmatch(local) is None:
        raise _invalid()
    labels = domain.split(".")
    if any(EMAIL_DOMAIN_LABEL_RE.fullmatch(label) is None for label in labels):
        raise _invalid()
    canonical = value.lower()
    if len(canonical) > MAX_EMAIL_LENGTH:
        raise _invalid()
    return canonical


def validate_live_marker(value: object, *, allow_empty: bool = False) -> str:
    """Return a lower-case live marker, preserving the empty false-mode value."""

    if not isinstance(value, str) or not value.isascii():
        raise _invalid()
    if allow_empty and value == "":
        return ""
    if len(value) > MAX_LIVE_MARKER_LENGTH or value != value.strip():
        raise _invalid()
    if LIVE_MARKER_RE.fullmatch(value) is None:
        raise _invalid()
    return value


def validate_confirmation(value: object, expected: object) -> str:
    """Require one exact, bounded ASCII operator confirmation token.

    Workflows pass the expected token as a fixed literal.  Keeping the
    comparison here means dispatch inputs do not acquire a second, subtly
    different shell grammar in individual workflows.
    """

    if (
        not isinstance(value, str)
        or not isinstance(expected, str)
        or not value.isascii()
        or not expected.isascii()
        or expected not in CONFIRMATION_PHRASES
        or value != expected
        or value != value.strip()
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
    ):
        raise _invalid()
    return value


def validate_target_sha(value: object) -> str:
    """Require one lower-case, full-length Git commit SHA."""

    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise _invalid()
    return value


def validate_run_id(value: object) -> str:
    """Require one bounded decimal GitHub run identifier."""

    return _validate_run_id(value)


def validate_bounded_integer(value: object, *, minimum: int, maximum: int) -> str:
    """Require one decimal integer in the caller's closed range."""

    if (
        type(minimum) is not int
        or type(maximum) is not int
        or minimum < 0
        or maximum < minimum
    ):
        raise _invalid()
    if not isinstance(value, str) or POSITIVE_INTEGER_RE.fullmatch(value) is None:
        raise _invalid()
    try:
        number = int(value, 10)
    except ValueError as exc:  # pragma: no cover - regex already excludes this.
        raise _invalid() from exc
    if number < minimum or number > maximum:
        raise _invalid()
    return value


def validate_utc_timestamp(value: object) -> str:
    """Require an RFC3339 UTC timestamp with second precision.

    Calendar and ordering checks remain with the workflow's existing date
    arithmetic.  This validator owns only the shell-safe, fixed-width input
    grammar so control characters and command syntax cannot reach that code.
    """

    if not isinstance(value, str) or UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise _invalid()
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise _invalid() from exc
    return value


def _require_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise _invalid()
    return value


def _require_mapping(payload: object) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise _invalid()
    return payload


def _require_exact_keys(payload: Mapping[str, Any], keys: set[str]) -> None:
    if set(payload) != keys:
        raise _invalid()


def _validate_run_id(value: object) -> str:
    if not isinstance(value, str) or RUN_ID_RE.fullmatch(value) is None:
        raise _invalid()
    return value


def _validate_positive_integer(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or POSITIVE_INTEGER_RE.fullmatch(value) is None:
        raise _invalid()
    try:
        number = int(value, 10)
    except ValueError as exc:  # pragma: no cover - regex already excludes this.
        raise _invalid() from exc
    if number > maximum:
        raise _invalid()
    return value


def _validate_schema(value: object) -> None:
    # JSON handoffs emitted by this module use the string form so every
    # serialized field has one bounded representation.  Accept only that
    # representation or the integer form used by direct callers; booleans are
    # deliberately not interchangeable with schema version 1.
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, str))
        or (isinstance(value, int) and value != 1)
        or (isinstance(value, str) and value != "1")
    ):
        raise _invalid()


def validate_external_payload(payload: Mapping[str, Any]) -> dict[str, str]:
    """Validate the complete external-load dispatch object."""

    payload = _require_mapping(payload)
    keys = {
        "schema",
        "confirmation",
        "target_sha",
        "control_email",
        "setup_concurrency",
        "run_id",
        "profile",
        "tournament_count",
        "users_per_tournament",
        "timeout_diagnostics",
    }
    _require_exact_keys(payload, keys)
    _validate_schema(payload.get("schema"))
    confirmation = _require_string(payload, "confirmation")
    target_sha = _require_string(payload, "target_sha")
    control_email = validate_control_email(payload.get("control_email"))
    setup_concurrency = _validate_positive_integer(
        payload.get("setup_concurrency"), maximum=256
    )
    run_id = _validate_run_id(payload.get("run_id"))
    profile = _require_string(payload, "profile")
    if profile != "external-vote":
        raise _invalid()
    tournament_count = _validate_positive_integer(
        payload.get("tournament_count"), maximum=40
    )
    users_per_tournament = _validate_positive_integer(
        payload.get("users_per_tournament"), maximum=500
    )
    if int(users_per_tournament, 10) < 14:
        raise _invalid()
    timeout_diagnostics = _require_string(payload, "timeout_diagnostics")
    if timeout_diagnostics not in {"true", "false"}:
        raise _invalid()
    if timeout_diagnostics == "true":
        if confirmation != TIMEOUT_DIAGNOSTICS_CONFIRMATION:
            raise _invalid()
    elif confirmation != EXTERNAL_CONFIRMATION:
        raise _invalid()
    if SHA_RE.fullmatch(target_sha) is None:
        raise _invalid()
    return {
        "schema": "1",
        "confirmation": confirmation,
        "target_sha": target_sha,
        "control_email": control_email,
        "setup_concurrency": setup_concurrency,
        "run_id": run_id,
        "profile": profile,
        "tournament_count": tournament_count,
        "users_per_tournament": users_per_tournament,
        "timeout_diagnostics": timeout_diagnostics,
    }


def validate_live_payload(payload: Mapping[str, Any]) -> dict[str, str]:
    """Validate the complete live-launch dispatch object."""

    payload = _require_mapping(payload)
    keys = {"schema", "base_url", "provision", "marker", "target_sha"}
    _require_exact_keys(payload, keys)
    _validate_schema(payload.get("schema"))
    base_url = _require_string(payload, "base_url")
    if base_url != EXPECTED_ORIGIN:
        raise _invalid()
    provision = _require_string(payload, "provision")
    if provision not in {"true", "false"}:
        raise _invalid()
    marker = validate_live_marker(payload.get("marker"), allow_empty=provision == "false")
    if provision == "true" and not marker:
        raise _invalid()
    target_sha = _require_string(payload, "target_sha")
    if SHA_RE.fullmatch(target_sha) is None:
        raise _invalid()
    return {
        "schema": "1",
        "base_url": base_url,
        "provision": provision,
        "marker": marker,
        "target_sha": target_sha,
    }


def validate_cleanup_payload(payload: Mapping[str, Any]) -> dict[str, str]:
    """Validate values used by exact retained-load cleanup."""

    payload = _require_mapping(payload)
    keys = {
        "schema",
        "target_sha",
        "control_email",
        "load_run_id",
        "cleanup_run_id",
    }
    _require_exact_keys(payload, keys)
    _validate_schema(payload.get("schema"))
    target_sha = _require_string(payload, "target_sha")
    if SHA_RE.fullmatch(target_sha) is None:
        raise _invalid()
    return {
        "schema": "1",
        "target_sha": target_sha,
        "control_email": validate_control_email(payload.get("control_email")),
        "load_run_id": _validate_run_id(payload.get("load_run_id")),
        "cleanup_run_id": _validate_run_id(payload.get("cleanup_run_id")),
    }


def validate_deployment_payload(payload: Mapping[str, Any]) -> dict[str, str]:
    """Validate the complete production deployment handoff object."""

    payload = _require_mapping(payload)
    keys = {
        "schema",
        "mode",
        "runtime_profile",
        "release_slug",
        "target_sha",
        "artifact_remote_dir",
        "classifier_run_id",
        "classifier_run_attempt",
        "web_compression",
    }
    _require_exact_keys(payload, keys)
    _validate_schema(payload.get("schema"))
    mode = _require_string(payload, "mode")
    if mode not in DEPLOY_MODES:
        raise _invalid()
    runtime_profile = _require_string(payload, "runtime_profile")
    if runtime_profile not in DEPLOY_RUNTIME_PROFILES:
        raise _invalid()
    release_slug = _require_string(payload, "release_slug")
    if not release_slug.isascii() or RELEASE_SLUG_RE.fullmatch(release_slug) is None:
        raise _invalid()
    target_sha = _require_string(payload, "target_sha")
    if SHA_RE.fullmatch(target_sha) is None:
        raise _invalid()
    artifact_remote_dir = _require_string(payload, "artifact_remote_dir")
    if (
        not artifact_remote_dir.isascii()
        or ARTIFACT_REMOTE_DIR_RE.fullmatch(artifact_remote_dir) is None
    ):
        raise _invalid()
    classifier_run_id = _validate_run_id(payload.get("classifier_run_id"))
    classifier_run_attempt = _validate_run_id(payload.get("classifier_run_attempt"))
    web_compression = _require_string(payload, "web_compression")
    if web_compression not in {"enabled", "disabled"}:
        raise _invalid()
    return {
        "schema": "1",
        "mode": mode,
        "runtime_profile": runtime_profile,
        "release_slug": release_slug,
        "target_sha": target_sha,
        "artifact_remote_dir": artifact_remote_dir,
        "classifier_run_id": classifier_run_id,
        "classifier_run_attempt": classifier_run_attempt,
        "web_compression": web_compression,
    }


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise _invalid()
        payload[key] = value
    return payload


def _load_json_bytes(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_INPUT_BYTES:
        raise _invalid()
    try:
        decoded = raw.decode("utf-8")
        payload = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _invalid() from exc
    if not isinstance(payload, dict):
        raise _invalid()
    return payload


def _reject_symlink_components(path: Path) -> None:
    """Reject a path whose leaf or any existing ancestor is a symlink."""

    if not path.is_absolute():
        raise _invalid()
    for component in (path, *path.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _invalid() from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise _invalid()


def load_payload(path: Path, *, mode: str) -> dict[str, str]:
    try:
        _reject_symlink_components(path)
    except WorkflowInputError:
        raise
    if path.is_symlink() or not path.is_file():
        raise _invalid()
    try:
        raw = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise _invalid() from exc
    payload = _load_json_bytes(raw)
    if mode == "external":
        return validate_external_payload(payload)
    if mode == "live":
        return validate_live_payload(payload)
    if mode == "cleanup":
        return validate_cleanup_payload(payload)
    if mode == "deployment":
        return validate_deployment_payload(payload)
    raise _invalid()


def load_stdin_payload(*, mode: str) -> dict[str, str]:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise _invalid() from exc
    payload = _load_json_bytes(raw)
    if mode == "external":
        return validate_external_payload(payload)
    if mode == "live":
        return validate_live_payload(payload)
    if mode == "cleanup":
        return validate_cleanup_payload(payload)
    if mode == "deployment":
        return validate_deployment_payload(payload)
    raise _invalid()


def _write_private_json(path: Path, payload: Mapping[str, str]) -> None:
    """Atomically publish a mode-600 JSON handoff without following a symlink."""

    if not path.is_absolute():
        raise _invalid()
    _reject_symlink_components(path)
    parent = path.parent
    temporary: Path | None = None
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise _invalid() from exc
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise _invalid()
    try:
        destination_stat = path.lstat()
    except FileNotFoundError:
        destination_stat = None
    except OSError as exc:
        raise _invalid() from exc
    if destination_stat is not None and stat.S_ISLNK(destination_stat.st_mode):
        raise _invalid()
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=".workflow-input-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.chmod(handle.fileno(), 0o600)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise _invalid() from exc


def _external_from_args(args: argparse.Namespace) -> dict[str, str]:
    payload: dict[str, Any] = {
        "schema": 1,
        "confirmation": args.confirmation,
        "target_sha": args.target_sha,
        "control_email": args.control_email,
        "setup_concurrency": args.setup_concurrency,
        "run_id": args.run_id,
        "profile": args.profile,
        "tournament_count": args.tournament_count,
        "users_per_tournament": args.users_per_tournament,
        "timeout_diagnostics": args.timeout_diagnostics,
    }
    return validate_external_payload(payload)


def _live_from_args(args: argparse.Namespace) -> dict[str, str]:
    return validate_live_payload(
        {
            "schema": 1,
            "base_url": args.base_url,
            "provision": args.provision,
            "marker": args.marker,
            "target_sha": args.target_sha,
        }
    )


def _cleanup_from_args(args: argparse.Namespace) -> dict[str, str]:
    payload: dict[str, Any] = {
        "schema": 1,
        "target_sha": args.target_sha,
        "control_email": args.control_email,
        "load_run_id": args.load_run_id,
        "cleanup_run_id": args.cleanup_run_id,
    }
    return validate_cleanup_payload(payload)


def _deployment_from_args(args: argparse.Namespace) -> dict[str, str]:
    payload: dict[str, Any] = {
        "schema": 1,
        "mode": args.mode,
        "runtime_profile": args.runtime_profile,
        "release_slug": args.release_slug,
        "target_sha": args.target_sha,
        "artifact_remote_dir": args.artifact_remote_dir,
        "classifier_run_id": args.classifier_run_id,
        "classifier_run_attempt": args.classifier_run_attempt,
        "web_compression": args.web_compression,
    }
    return validate_deployment_payload(payload)


def _parser() -> argparse.ArgumentParser:
    parser = _WorkflowInputArgumentParser(
        description="Validate production workflow handoff data"
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_WorkflowInputArgumentParser,
    )

    email = subparsers.add_parser("email")
    email.add_argument("--value", required=True)

    marker = subparsers.add_parser("marker")
    marker.add_argument("--value", required=True)

    confirmation = subparsers.add_parser("confirmation")
    confirmation.add_argument("--value", required=True)
    confirmation.add_argument("--expected", required=True)

    sha = subparsers.add_parser("sha")
    sha.add_argument("--value", required=True)

    run_id = subparsers.add_parser("run-id")
    run_id.add_argument("--value", required=True)

    bounded_integer = subparsers.add_parser("bounded-int")
    bounded_integer.add_argument("--value", required=True)
    bounded_integer.add_argument("--minimum", required=True, type=int)
    bounded_integer.add_argument("--maximum", required=True, type=int)

    timestamp = subparsers.add_parser("utc-timestamp")
    timestamp.add_argument("--value", required=True)

    external = subparsers.add_parser("external")
    external.add_argument("--output", type=Path, required=True)
    external.add_argument("--confirmation", required=True)
    external.add_argument("--target-sha", required=True)
    external.add_argument("--control-email", required=True)
    external.add_argument("--setup-concurrency", required=True)
    external.add_argument("--run-id", required=True)
    external.add_argument("--profile", required=True)
    external.add_argument("--tournament-count", required=True)
    external.add_argument("--users-per-tournament", required=True)
    external.add_argument("--timeout-diagnostics", required=True)

    live = subparsers.add_parser("live")
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--base-url", required=True)
    live.add_argument("--provision", required=True)
    live.add_argument("--marker", required=True)
    live.add_argument("--target-sha", required=True)

    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--output", type=Path, required=True)
    cleanup.add_argument("--target-sha", required=True)
    cleanup.add_argument("--control-email", required=True)
    cleanup.add_argument("--load-run-id", required=True)
    cleanup.add_argument("--cleanup-run-id", required=True)

    deployment = subparsers.add_parser("deployment")
    deployment.add_argument("--output", type=Path, required=True)
    deployment.add_argument("--mode", required=True)
    deployment.add_argument("--runtime-profile", required=True)
    deployment.add_argument("--release-slug", required=True)
    deployment.add_argument("--target-sha", required=True)
    deployment.add_argument("--artifact-remote-dir", required=True)
    deployment.add_argument("--classifier-run-id", required=True)
    deployment.add_argument("--classifier-run-attempt", required=True)
    deployment.add_argument("--web-compression", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "email":
            validate_control_email(args.value)
            return 0
        if args.command == "marker":
            validate_live_marker(args.value)
            return 0
        if args.command == "confirmation":
            validate_confirmation(args.value, args.expected)
            return 0
        if args.command == "sha":
            validate_target_sha(args.value)
            return 0
        if args.command == "run-id":
            validate_run_id(args.value)
            return 0
        if args.command == "bounded-int":
            validate_bounded_integer(
                args.value,
                minimum=args.minimum,
                maximum=args.maximum,
            )
            return 0
        if args.command == "utc-timestamp":
            validate_utc_timestamp(args.value)
            return 0
        if args.command == "external":
            _write_private_json(args.output, _external_from_args(args))
            return 0
        if args.command == "live":
            _write_private_json(args.output, _live_from_args(args))
            return 0
        if args.command == "cleanup":
            _write_private_json(args.output, _cleanup_from_args(args))
            return 0
        if args.command == "deployment":
            _write_private_json(args.output, _deployment_from_args(args))
            return 0
    except (WorkflowInputError, SystemExit):
        # argparse's own usage output is intentionally suppressed for dispatch
        # values: neither a rejected value nor a parser echo may reach logs.
        print("workflow input is invalid", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
