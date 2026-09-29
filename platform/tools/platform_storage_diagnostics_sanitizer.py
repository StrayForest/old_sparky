#!/usr/bin/env python3
"""Project storage diagnostics into a bounded, closed public schema.

The production probe deliberately uses only commands and files that belong to
the deployed release.  Its stdout and stderr are private runner captures.  A
runner-side projection is the only boundary allowed to create the artifact:
it parses known numeric/enumerated fields, drops every other value and never
returns the captured text.  The projector runs after the SSH key cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any


# The script is executed with ``python -I`` in the workflow.  Import the
# repository-owned value-only producers explicitly without relying on
# PYTHONPATH or the current working directory.
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from platform_storage_evidence_summary import (  # noqa: E402
    EvidenceInputError,
    SERVICE_PROPERTY_KEYS,
    summarize_df,
    summarize_du,
    summarize_inode,
    summarize_journal,
    summarize_lock,
    summarize_retention,
    summarize_service,
)


SCHEMA = 1
MAX_INPUT_BYTES = 512 * 1024
MAX_LINES = 2048
MAX_COUNT = 1_000_000
MAX_REPORTED_BYTES = 1_000_000
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")

# Failure details are a public contract.  Keep these sets closed so a
# producer-controlled string can never become an artifact field, even if a
# future caller passes an unexpected value into ``_failure_summary``.
SAFE_FAILURE_PHASES = frozenset(
    {
        "capture",
        "precondition",
        "transport",
        "remote",
        "report",
        "filesystem",
        "journal",
        "service",
        "category",
        "retention",
        "cleanup",
    }
)
SAFE_FAILURE_REASONS = frozenset(
    {
        "active_release_id_invalid",
        "active_release_manifest_missing",
        "active_sha_mismatch",
        "category_usage_producer",
        "current_release_missing",
        "disk_usage_producer",
        "inode_usage_producer",
        "journal_usage_producer",
        "maintenance_tool_missing",
        "privilege",
        "python_runtime_missing",
        "remote_collection_failed",
        "remote_report_missing",
        "remote_timeout",
        "report_schema_incomplete",
        "report_truncated",
        "retention_producer",
        "retention_summary",
        "sanitizer_exception",
        "sanitizer_unavailable",
        "service_state_producer",
        "ssh_cleanup_failed",
        "ssh_transport",
    }
)
SAFE_FAILURE_ACTIONS = frozenset(
    {
        "inspect_current_release_manifest",
        "inspect_deployed_storage_tool",
        "inspect_current_release_pointer",
        "inspect_production_runner_identity",
        "inspect_release_identity",
        "inspect_remote_collection",
        "inspect_remote_diagnostics_command",
        "inspect_remote_diagnostics_contract",
        "inspect_remote_output_volume",
        "inspect_remote_transport_timeout",
        "inspect_runner_evidence_projector",
        "inspect_runner_ssh_cleanup",
        "inspect_service_state_producer",
        "inspect_shared_runtime",
        "inspect_ssh_transport_and_host_key",
        "inspect_category_usage_producer",
        "inspect_disk_usage_producer",
        "inspect_inode_usage_producer",
        "inspect_journal_usage_producer",
        "inspect_retention_dry_run",
        "inspect_retention_summary",
        "recalculate_expected_sha_from_release",
    }
)

EXPECTED_SECTIONS = (
    "lock",
    "filesystem",
    "journal",
    "services",
    "categories",
    "retention",
)

SAFE_CATEGORIES = (
    "root",
    "tmp",
    "var_tmp",
    "platform_runtime",
    "logs",
)
SAFE_DU_CATEGORIES = (
    "source_release_artifacts",
    "browser_test_artifacts",
    "preprod_screenshots",
    "live_qa_runtime",
    "backups",
)
SAFE_SERVICES = ("deadlock-api", "deadlock-worker", "deadlock-web")

_MARKERS: tuple[tuple[bytes, str, str, str], ...] = (
    (
        b"Production diagnostics must run as root.",
        "precondition",
        "privilege",
        "inspect_production_runner_identity",
    ),
    (
        b"Current release symlink is missing.",
        "precondition",
        "current_release_missing",
        "inspect_current_release_pointer",
    ),
    (
        b"Shared Python runtime is missing.",
        "precondition",
        "python_runtime_missing",
        "inspect_shared_runtime",
    ),
    (
        b"Storage maintenance helper is missing or unsafe.",
        "precondition",
        "maintenance_tool_missing",
        "inspect_deployed_storage_tool",
    ),
    (
        b"active release manifest is missing.",
        "precondition",
        "active_release_manifest_missing",
        "inspect_current_release_manifest",
    ),
    (
        b"active release SHA differs from expected_sha.",
        "precondition",
        "active_sha_mismatch",
        "recalculate_expected_sha_from_release",
    ),
    (
        b"active release identifier is invalid.",
        "precondition",
        "active_release_id_invalid",
        "inspect_release_identity",
    ),
    (
        b"storage retention producer failed.",
        "retention",
        "retention_producer",
        "inspect_retention_dry_run",
    ),
    (
        b"disk usage producer failed.",
        "filesystem",
        "disk_usage_producer",
        "inspect_disk_usage_producer",
    ),
    (
        b"inode usage producer failed.",
        "filesystem",
        "inode_usage_producer",
        "inspect_inode_usage_producer",
    ),
    (
        b"journal usage producer failed.",
        "journal",
        "journal_usage_producer",
        "inspect_journal_usage_producer",
    ),
    (
        b"service state producer failed.",
        "service",
        "service_state_producer",
        "inspect_service_state_producer",
    ),
    (
        b"category size producer failed.",
        "category",
        "category_usage_producer",
        "inspect_category_usage_producer",
    ),
    (
        b"storage retention summary failed.",
        "retention",
        "retention_summary",
        "inspect_retention_summary",
    ),
)


def _safe_sha(value: object) -> str:
    return value if isinstance(value, str) and SHA_RE.fullmatch(value) else "unavailable"


def _safe_int(value: object, *, maximum: int = MAX_COUNT) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number <= maximum else None


def _safe_bool(value: object) -> bool:
    return value is True or (isinstance(value, str) and value == "true")


def _read_bounded(path: Path) -> tuple[bytes, bool]:
    """Read a bounded regular file, rejecting symlinks and special files."""

    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_NONBLOCK
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise ValueError("capture is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("capture is unavailable")
        # Read in bounded chunks rather than asking the kernel for an
        # attacker-sized file in one call.  The extra byte distinguishes a
        # complete capture at the boundary from a truncated one.
        chunks: list[bytes] = []
        remaining = MAX_INPUT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    return raw[:MAX_INPUT_BYTES], len(raw) > MAX_INPUT_BYTES


def _empty_sections() -> dict[str, object]:
    return {
        "lock": None,
        "filesystem": {},
        "journal": None,
        "services": {},
        "categories": {},
        "retention": None,
    }


def _safe_projection(function: Any, raw: str, **kwargs: object) -> dict[str, object] | None:
    try:
        result = function(raw, **kwargs)
    except (EvidenceInputError, TypeError, ValueError, OverflowError):
        return None
    return result if isinstance(result, dict) else None


def _split_report(raw: str) -> tuple[dict[str, list[str]], bool]:
    """Split only fixed section/record labels; discard unlabeled structure."""

    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    malformed = "\ufffd" in raw or any(
        ord(character) == 0x7F
        or (ord(character) < 0x20 and character not in "\t\n\r")
        for character in raw
    )
    known_sections = {
        "=== retained_load_lock ===": "lock",
        "=== filesystem_usage ===": "filesystem",
        "=== journal_usage ===": "journal",
        "=== service_sandbox ===": "services",
        "=== known_category_usage ===": "categories",
        "=== storage_retention_dry_run ===": "retention",
    }
    seen_sections = {"header"}
    section_order = tuple(known_sections.values())
    last_section_index = -1
    lines = raw.splitlines()
    if len(lines) > MAX_LINES:
        malformed = True
    for line in lines[:MAX_LINES]:
        if line in known_sections:
            current = known_sections[line]
            if current in seen_sections:
                malformed = True
            section_index = section_order.index(current)
            if section_index <= last_section_index:
                malformed = True
            last_section_index = max(last_section_index, section_index)
            seen_sections.add(current)
            sections[current] = []
        elif line.startswith("==="):
            malformed = True
            sections.setdefault(current, []).append(line)
        else:
            sections.setdefault(current, []).append(line)
    if set(sections) - {"header", *EXPECTED_SECTIONS}:
        malformed = True
    return sections, malformed


def _parse_fixed_record(line: str, prefix: str) -> tuple[str, str] | None:
    if not line.startswith(prefix):
        return None
    value = line[len(prefix) :]
    name, separator, payload = value.partition(" ")
    if not name:
        return None
    return name, payload if separator else ""


def _fixed_records(lines: list[str], prefix: str) -> list[tuple[str, str]]:
    """Collect bounded multiline records introduced by one fixed label."""

    records: list[tuple[str, str]] = []
    current_name: str | None = None
    current_lines: list[str] = []
    for line in lines:
        parsed = _parse_fixed_record(line, prefix)
        if parsed is not None:
            if current_name is not None:
                records.append((current_name, "\n".join(current_lines)))
            current_name, payload = parsed
            current_lines = [payload]
        elif line.startswith("--- "):
            if current_name is not None:
                records.append((current_name, "\n".join(current_lines)))
                current_name = None
                current_lines = []
        elif current_name is not None:
            current_lines.append(line)
    if current_name is not None:
        records.append((current_name, "\n".join(current_lines)))
    return records


def _parse_provenance(lines: list[str]) -> tuple[str, str, bool]:
    """Validate the one-line producer envelope without echoing its values."""

    candidates = [line for line in lines if line.startswith("{")]
    if len(candidates) != 1:
        return "unavailable", "unavailable", False
    try:
        payload = json.loads(candidates[0])
    except (json.JSONDecodeError, TypeError):
        return "unavailable", "unavailable", False
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "kind",
        "active_release_id",
        "active_source_sha",
    }:
        return "unavailable", "unavailable", False
    if payload.get("schema") != SCHEMA or payload.get("kind") != "platform_storage_diagnostics":
        return "unavailable", "unavailable", False
    release_id = payload.get("active_release_id")
    source_sha = payload.get("active_source_sha")
    if not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None:
        return "unavailable", "unavailable", False
    if not isinstance(source_sha, str) or SHA_RE.fullmatch(source_sha) is None:
        return "unavailable", "unavailable", False
    return release_id, source_sha, True


def _records_by_name(
    records: list[tuple[str, str]], allowed: tuple[str, ...]
) -> tuple[dict[str, str], bool]:
    names = {name for name, _ in records}
    if len(names) != len(records) or names != set(allowed):
        return {}, False
    return dict(records), True


def _record_labels_valid(lines: list[str], prefixes: tuple[str, ...]) -> bool:
    """Reject an unknown record label instead of silently dropping it."""

    for line in lines:
        if line.startswith("--- ") and not line.startswith(prefixes):
            return False
    return True


def _service_summary_complete(value: object) -> bool:
    if not isinstance(value, dict) or value.get("status") != "ok":
        return False
    properties = value.get("properties")
    if not isinstance(properties, dict) or set(properties) != set(SERVICE_PROPERTY_KEYS):
        return False
    # Unknown enum values indicate that systemd returned a producer shape we
    # do not understand.  Do not call that a successful strict report.
    for key in ("ActiveState", "SubState", "Result"):
        if properties.get(key) in (None, "unknown"):
            return False
    return True


def _strict_section_success(result: dict[str, object]) -> bool:
    """Require every producer to have yielded its complete closed summary."""

    lock = result.get("lock")
    if not isinstance(lock, dict) or lock.get("status") != "ok":
        return False
    journal = result.get("journal")
    if not isinstance(journal, dict) or journal.get("status") != "ok":
        return False
    filesystem = result.get("filesystem")
    if not isinstance(filesystem, dict) or set(filesystem) != set(SAFE_CATEGORIES):
        return False
    for value in filesystem.values():
        if (
            not isinstance(value, dict)
            or value.get("status") != "ok"
            or not isinstance(value.get("inode"), dict)
            or value["inode"].get("status") != "ok"
        ):
            return False
    services = result.get("services")
    if not isinstance(services, dict) or set(services) != set(SAFE_SERVICES):
        return False
    if not all(_service_summary_complete(value) for value in services.values()):
        return False
    categories = result.get("categories")
    if not isinstance(categories, dict) or set(categories) != set(SAFE_DU_CATEGORIES):
        return False
    if not all(
        isinstance(value, dict) and value.get("status") == "ok"
        for value in categories.values()
    ):
        return False
    retention = result.get("retention")
    if not isinstance(retention, dict):
        return False
    return (
        retention.get("status") == "ok"
        and retention.get("ok") is True
        and retention.get("mode") == "dry-run"
    )


def _project_report(raw: str) -> tuple[dict[str, object], bool]:
    sections, malformed = _split_report(raw)
    result = _empty_sections()
    active_release_id, active_source_sha, provenance_valid = _parse_provenance(
        sections.get("header", [])
    )

    lock_raw = "\n".join(sections.get("lock", ()))
    if lock_raw:
        result["lock"] = _safe_projection(summarize_lock, lock_raw)

    filesystem: dict[str, object] = {}
    filesystem_lines = sections.get("filesystem", [])
    filesystem_labels_valid = _record_labels_valid(
        filesystem_lines, ("--- df ", "--- inode ")
    )
    filesystem_records, filesystem_records_valid = _records_by_name(
        _fixed_records(filesystem_lines, "--- df "), SAFE_CATEGORIES
    )
    for category, payload in filesystem_records.items():
        if category in SAFE_CATEGORIES:
            summary = _safe_projection(summarize_df, payload, category=category)
            if summary is not None:
                filesystem[category] = summary
    inode_records, inode_records_valid = _records_by_name(
        _fixed_records(filesystem_lines, "--- inode "), SAFE_CATEGORIES
    )
    for category, payload in inode_records.items():
        if category in SAFE_CATEGORIES:
            summary = _safe_projection(summarize_inode, payload, category=category)
            current = filesystem.get(category)
            if summary is not None and isinstance(current, dict):
                current["inode"] = summary
    result["filesystem"] = filesystem

    journal_raw = "\n".join(sections.get("journal", ()))
    if journal_raw:
        result["journal"] = _safe_projection(summarize_journal, journal_raw)

    services: dict[str, object] = {}
    service_labels_valid = _record_labels_valid(
        sections.get("services", []), ("--- service ",)
    )
    service_records, service_records_valid = _records_by_name(
        _fixed_records(sections.get("services", []), "--- service "), SAFE_SERVICES
    )
    for service, payload in service_records.items():
        if service in SAFE_SERVICES:
            summary = _safe_projection(
                summarize_service, payload, service=service
            )
            if summary is not None:
                services[service] = summary
    result["services"] = services

    categories: dict[str, object] = {}
    category_labels_valid = _record_labels_valid(
        sections.get("categories", []), ("--- category ",)
    )
    category_records, category_records_valid = _records_by_name(
        _fixed_records(sections.get("categories", []), "--- category "), SAFE_DU_CATEGORIES
    )
    for category, payload in category_records.items():
        if category in SAFE_DU_CATEGORIES:
            summary = _safe_projection(
                summarize_du, payload, category=category
            )
            if summary is not None:
                categories[category] = summary
    result["categories"] = categories

    retention_raw = "\n".join(sections.get("retention", ()))
    if retention_raw:
        result["retention"] = _safe_projection(summarize_retention, retention_raw)

    def filesystem_entry_complete(category: str) -> bool:
        entry = filesystem.get(category)
        return isinstance(entry, dict) and isinstance(entry.get("inode"), dict)

    complete = bool(
        not malformed
        and provenance_valid
        and active_release_id != "unavailable"
        and active_source_sha != "unavailable"
        and filesystem_records_valid
        and inode_records_valid
        and service_records_valid
        and category_records_valid
        and filesystem_labels_valid
        and service_labels_valid
        and category_labels_valid
        and _strict_section_success(result)
        and all(filesystem_entry_complete(category) for category in SAFE_CATEGORIES)
    )
    return {
        "active_release_id": active_release_id,
        "active_source_sha": active_source_sha,
        "sections": result,
    }, complete


def _classify_failure(
    stderr: bytes,
    *,
    exit_code: int | None,
    report_present: bool,
    report_truncated: bool,
    report_complete: bool,
) -> tuple[str, str, str]:
    if exit_code == 255:
        return "transport", "ssh_transport", "inspect_ssh_transport_and_host_key"
    if exit_code == 124:
        return "transport", "remote_timeout", "inspect_remote_transport_timeout"
    if report_truncated:
        return "capture", "report_truncated", "inspect_remote_output_volume"
    for marker, phase, reason, action in _MARKERS:
        if marker in stderr:
            return phase, reason, action
    if not report_present:
        return "remote", "remote_report_missing", "inspect_remote_diagnostics_command"
    if report_present and not report_complete:
        return "report", "report_schema_incomplete", "inspect_remote_diagnostics_contract"
    return "remote", "remote_collection_failed", "inspect_remote_diagnostics_command"


def _failure_summary(
    *,
    expected_sha: object,
    remote_exit_code: object,
    remote_stderr_bytes: object,
    report_present: object,
    stderr: bytes = b"",
    stderr_truncated: bool = False,
    report_truncated: bool = False,
    phase: str | None = None,
    reason: str | None = None,
    action: str | None = None,
) -> dict[str, object]:
    exit_code = _safe_int(remote_exit_code, maximum=255)
    stderr_bytes = _safe_int(remote_stderr_bytes, maximum=MAX_REPORTED_BYTES)
    if remote_stderr_bytes is not None and stderr_bytes is None:
        stderr_truncated = True
    report = _safe_bool(report_present)
    if phase is None or reason is None or action is None:
        phase, reason, action = _classify_failure(
            stderr,
            exit_code=exit_code,
            report_present=report,
            report_truncated=report_truncated,
            report_complete=False,
        )
    if not isinstance(phase, str) or phase not in SAFE_FAILURE_PHASES:
        phase = "capture"
    if not isinstance(reason, str) or reason not in SAFE_FAILURE_REASONS:
        reason = "remote_collection_failed"
    if not isinstance(action, str) or action not in SAFE_FAILURE_ACTIONS:
        action = "inspect_remote_collection"
    return {
        "schema": SCHEMA,
        "kind": "platform_storage_diagnostics_failure",
        "status": "failed",
        "raw_output_included": False,
        "expected_sha": _safe_sha(expected_sha),
        "remote_exit_code": exit_code,
        "remote_stderr_bytes": stderr_bytes,
        "stderr_truncated": bool(stderr_truncated),
        "report_present": report,
        "report_truncated": bool(report_truncated),
        "phase": phase,
        "reason": reason,
        "action": action,
        "active_release_id": "unavailable",
        "active_source_sha": "unavailable",
        "sections": _empty_sections(),
    }


def project_public_artifact(
    *,
    expected_sha: object,
    remote_exit_code: object,
    remote_stderr_bytes: object,
    report_path: Path,
    stderr_path: Path,
    report_present: object,
) -> dict[str, object]:
    """Return one safe artifact, including a fixed failure fallback."""

    stderr = b""
    stderr_truncated = False
    try:
        stderr, stderr_truncated = _read_bounded(stderr_path)
    except (OSError, ValueError):
        stderr = b""
    try:
        report_bytes, report_truncated = _read_bounded(report_path)
    except (OSError, ValueError):
        return _failure_summary(
            expected_sha=expected_sha,
            remote_exit_code=remote_exit_code,
            remote_stderr_bytes=remote_stderr_bytes,
            report_present=False,
            stderr=stderr,
            stderr_truncated=stderr_truncated,
            report_truncated=False,
        )

    report_payload, report_complete = _project_report(
        report_bytes.decode("utf-8", errors="replace")
    )
    exit_code = _safe_int(remote_exit_code, maximum=255)
    expected = _safe_sha(expected_sha)
    active_sha = report_payload["active_source_sha"]
    report_sha_matches = expected != "unavailable" and active_sha == expected
    report_ok = (
        _safe_bool(report_present)
        and not report_truncated
        and report_complete
        and report_sha_matches
        and not stderr_truncated
        and _safe_int(remote_stderr_bytes, maximum=MAX_REPORTED_BYTES) == 0
    )
    if exit_code == 0 and report_ok:
        return {
            "schema": SCHEMA,
            "kind": "platform_storage_diagnostics",
            "status": "passed",
            "raw_output_included": False,
            "expected_sha": _safe_sha(expected_sha),
            "remote_exit_code": 0,
            "remote_stderr_bytes": _safe_int(
                remote_stderr_bytes, maximum=MAX_REPORTED_BYTES
            ),
            "stderr_truncated": bool(stderr_truncated),
            "report_present": True,
            "report_truncated": False,
            **report_payload,
        }

    if report_complete and not report_sha_matches:
        phase, reason, action = (
            "precondition",
            "active_sha_mismatch",
            "recalculate_expected_sha_from_release",
        )
    else:
        phase, reason, action = _classify_failure(
            stderr,
            exit_code=exit_code,
            report_present=_safe_bool(report_present),
            report_truncated=report_truncated,
            report_complete=report_complete,
        )
    failure = _failure_summary(
        expected_sha=expected_sha,
        remote_exit_code=remote_exit_code,
        remote_stderr_bytes=remote_stderr_bytes,
        report_present=report_present,
        stderr=stderr,
        stderr_truncated=stderr_truncated,
        report_truncated=report_truncated,
        phase=phase,
        reason=reason,
        action=action,
    )
    failure["sections"] = report_payload["sections"]
    failure["active_release_id"] = report_payload["active_release_id"]
    failure["active_source_sha"] = report_payload["active_source_sha"]
    return failure


def _write_nofollow(path: Path, payload: dict[str, object]) -> None:
    """Create a mode-600 regular artifact without following a symlink."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    encoded = (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    metadata = path.lstat()
    if not path.is_file() or path.is_symlink() or metadata.st_mode & 0o777 != 0o600:
        raise OSError("artifact is not a private regular file")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--stderr-path", type=Path, required=True)
    parser.add_argument("--output-path", type=Path, required=True)
    parser.add_argument("--expected-sha", default="")
    parser.add_argument("--remote-exit-code", default="")
    parser.add_argument("--remote-stderr-bytes", default="")
    parser.add_argument("--report-present", choices=("true", "false"), default="false")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = project_public_artifact(
            expected_sha=args.expected_sha,
            remote_exit_code=args.remote_exit_code,
            remote_stderr_bytes=args.remote_stderr_bytes,
            report_path=args.report_path,
            stderr_path=args.stderr_path,
            report_present=args.report_present,
        )
        _write_nofollow(args.output_path, report)
    except Exception:
        fallback = _failure_summary(
            expected_sha=args.expected_sha,
            remote_exit_code=args.remote_exit_code,
            remote_stderr_bytes=args.remote_stderr_bytes,
            report_present=args.report_present,
            phase="capture",
            reason="sanitizer_exception",
            action="inspect_runner_evidence_projector",
        )
        try:
            _write_nofollow(args.output_path, fallback)
        except Exception:
            print("Storage diagnostics evidence projector failed closed.", file=sys.stderr)
            return 1
        print("Storage diagnostics evidence projector failed closed.", file=sys.stderr)
        return 1
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
