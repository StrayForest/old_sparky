#!/usr/bin/env python3
"""Convert production storage command output into fixed, value-only evidence.

The storage workflows may inspect the host, but their output is an external
CI artifact.  This helper deliberately accepts command output only as input
and returns counters, byte values, percentages, and closed enums.  It never
returns a source path, filesystem name, mount source, user, process command,
or arbitrary error text.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


SCHEMA = 1
MAX_INPUT_BYTES = 256 * 1024
MAX_LINES = 512
_INTEGER_RE = re.compile(r"^[0-9]{1,20}$")
_PERCENT_RE = re.compile(r"^([0-9]{1,3})(?:\.[0-9]{1,2})?%$")
_JOURNAL_SIZE_RE = re.compile(
    r"\btake\s+up\s+([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?)(?:i?B)?\b",
    re.IGNORECASE,
)
_UNIT_MULTIPLIERS = {
    "": 1,
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
    "t": 1024**4,
    "p": 1024**5,
    "e": 1024**6,
}

SAFE_CATEGORIES = frozenset(
    {
        "root",
        "tmp",
        "var_tmp",
        "platform_runtime",
        "logs",
        "source_release_artifacts",
        "browser_test_artifacts",
        "preprod_screenshots",
        "live_qa_runtime",
        "backups",
        "journal",
    }
)
SAFE_SERVICES = frozenset(
    {"deadlock-api", "deadlock-worker", "deadlock-web", "nginx", "maintenance"}
)
SERVICE_PROPERTY_KEYS = (
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "NRestarts",
    "MemoryCurrent",
    "MemoryPeak",
    "MemoryMax",
    "TasksCurrent",
    "TasksMax",
    "CPUUsageNSec",
)
_ENUMS = {
    "ActiveState": frozenset(
        {"active", "inactive", "failed", "activating", "deactivating", "reloading"}
    ),
    "SubState": frozenset(
        {"running", "dead", "exited", "failed", "auto-restart", "start", "stop"}
    ),
    "Result": frozenset(
        {
            "success",
            "exit-code",
            "signal",
            "core-dump",
            "timeout",
            "watchdog",
            "start-limit-hit",
            "resources",
            "protocol",
            "dependency",
            "assert",
            "condition",
        }
    ),
    "ExecMainCode": frozenset({"exited", "killed", "dumped", "dead", "invalid"}),
}
class EvidenceInputError(ValueError):
    """Input was not the expected bounded command contract."""


def _read_input() -> str:
    payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        raise EvidenceInputError("input exceeds bound")
    return payload.decode("utf-8", errors="replace")


def _safe_int(value: Any, *, maximum: int = 10**18) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0 or number > maximum:
        return None
    return number


def _safe_percent(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number <= 100 else None


def _fixed_category(value: str) -> str:
    if not isinstance(value, str) or value not in SAFE_CATEGORIES:
        raise EvidenceInputError("storage category is not allowlisted")
    return value


def summarize_df(raw: str, *, category: str) -> dict[str, object]:
    category = _fixed_category(category)
    rows: list[tuple[int, int, int, float]] = []
    for line in raw.splitlines()[:MAX_LINES]:
        fields = line.split()
        if len(fields) < 4:
            continue
        if not all(_INTEGER_RE.fullmatch(field) for field in fields[:3]):
            continue
        match = _PERCENT_RE.fullmatch(fields[3])
        if match is None:
            continue
        size, used, available = (_safe_int(field) for field in fields[:3])
        percent = _safe_percent(fields[3].rstrip("%"))
        if None in {size, used, available, percent}:
            continue
        if used > size or available > size:
            continue
        rows.append((size, used, available, percent))  # type: ignore[arg-type]
    if len(rows) != 1:
        raise EvidenceInputError("disk usage producer returned an unexpected row count")
    size, used, available, percent = rows[0]
    return {
        "schema": SCHEMA,
        "kind": "disk_usage",
        "status": "ok",
        "category": category,
        "total_bytes": size,
        "used_bytes": used,
        "free_bytes": available,
        "used_percent": percent,
    }


def summarize_inode(raw: str, *, category: str) -> dict[str, object]:
    category = _fixed_category(category)
    rows: list[tuple[int, int, float]] = []
    for line in raw.splitlines()[:MAX_LINES]:
        fields = line.split()
        if len(fields) < 3 or not all(_INTEGER_RE.fullmatch(field) for field in fields[:2]):
            continue
        match = _PERCENT_RE.fullmatch(fields[2])
        if match is None:
            continue
        used, available = (_safe_int(field) for field in fields[:2])
        percent = _safe_percent(fields[2].rstrip("%"))
        if None in {used, available, percent}:
            continue
        rows.append((used, available, percent))  # type: ignore[arg-type]
    if len(rows) != 1:
        raise EvidenceInputError("inode usage producer returned an unexpected row count")
    used, available, percent = rows[0]
    return {
        "schema": SCHEMA,
        "kind": "inode_usage",
        "status": "ok",
        "category": category,
        "used_inodes": used,
        "free_inodes": available,
        "used_percent": percent,
    }


def summarize_du(raw: str, *, category: str) -> dict[str, object]:
    category = _fixed_category(category)
    values = []
    for line in raw.splitlines()[:MAX_LINES]:
        field = line.split(maxsplit=1)[:1]
        if field and _INTEGER_RE.fullmatch(field[0]):
            value = _safe_int(field[0])
            if value is not None:
                values.append(value)
    if len(values) != 1:
        raise EvidenceInputError("category size producer returned an unexpected row count")
    return {
        "schema": SCHEMA,
        "kind": "category_size",
        "status": "ok",
        "category": category,
        "size_bytes": values[0],
    }


def summarize_journal(raw: str) -> dict[str, object]:
    matches = _JOURNAL_SIZE_RE.findall(raw[:MAX_INPUT_BYTES])
    if not matches:
        raise EvidenceInputError("journal usage producer returned no size")
    amount, unit = matches[-1]
    multiplier = _UNIT_MULTIPLIERS[unit.lower()]
    size = int(float(amount) * multiplier)
    if size < 0 or size > 10**18:
        raise EvidenceInputError("journal usage is outside the safe range")
    return {
        "schema": SCHEMA,
        "kind": "journal_usage",
        "status": "ok",
        "size_bytes": size,
    }


def _safe_service_value(key: str, value: str) -> object:
    if key in _ENUMS:
        normalized = value.strip().lower()
        return normalized if normalized in _ENUMS[key] else "unknown"
    number = _safe_int(value.strip())
    return number


def summarize_service(raw: str, *, service: str) -> dict[str, object]:
    if not isinstance(service, str) or service not in SAFE_SERVICES:
        raise EvidenceInputError("service is not allowlisted")
    values: dict[str, object] = {key: None for key in SERVICE_PROPERTY_KEYS}
    recognized = 0
    for line in raw.splitlines()[:MAX_LINES]:
        key, separator, value = line.partition("=")
        if not separator or key not in values:
            continue
        values[key] = _safe_service_value(key, value)
        recognized += 1
    result = str(values["Result"] or "unknown")
    if result == "success":
        error_class = "none"
    elif result == "timeout":
        error_class = "timeout"
    elif result in {"signal", "core-dump", "watchdog"}:
        error_class = "crash"
    elif result == "unknown":
        error_class = "unknown"
    else:
        error_class = "failure"
    return {
        "schema": SCHEMA,
        "kind": "service_state",
        "status": "ok" if recognized else "empty",
        "service": service,
        "error_class": error_class,
        "properties": values,
    }


def summarize_lock(raw: str) -> dict[str, object]:
    state = "unknown"
    for line in raw.splitlines()[:MAX_LINES]:
        key, separator, value = line.partition("=")
        if key == "state" and separator:
            normalized = value.strip().lower()
            if normalized in {"held", "unlocked"}:
                state = normalized
    if state == "unknown":
        raise EvidenceInputError("lock state producer returned no known state")
    return {
        "schema": SCHEMA,
        "kind": "lock_state",
        "status": "ok",
        "state": state,
        "lock_held": state == "held",
        "holder_count": 1 if state == "held" else 0,
    }


def summarize_backup(raw: str, *, phase: str) -> dict[str, object]:
    if not isinstance(phase, str) or phase not in {"create", "check"}:
        raise EvidenceInputError("backup phase is not allowlisted")
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EvidenceInputError("backup report is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EvidenceInputError("backup report is not an object")
    status = "ok" if payload.get("ok") is True else "failed"
    restore_verified = payload.get("restore_verified")
    if not isinstance(restore_verified, bool):
        restore_verified = None
    alembic_verified = payload.get("alembic_revision_verified")
    if not isinstance(alembic_verified, bool):
        alembic_verified = None
    duration_seconds = payload.get("duration_seconds")
    if (
        not isinstance(duration_seconds, (int, float))
        or isinstance(duration_seconds, bool)
        or duration_seconds < 0
        or duration_seconds > 10**9
    ):
        duration_seconds = None
    age_hours = payload.get("age_hours")
    if (
        not isinstance(age_hours, (int, float))
        or isinstance(age_hours, bool)
        or age_hours < 0
        or age_hours > 10**6
    ):
        age_hours = None
    removed = payload.get("removed")
    removed_count = len(removed) if isinstance(removed, list) else 0
    return {
        "schema": SCHEMA,
        "kind": "backup_evidence",
        "phase": phase,
        "status": status,
        "error_class": "none" if status == "ok" else "backup",
        "restore_verified": restore_verified,
        "alembic_revision_verified": alembic_verified,
        "checksum_present": isinstance(payload.get("sha256"), str),
        "size_bytes": _safe_int(payload.get("size_bytes")),
        "duration_seconds": duration_seconds,
        "age_hours": age_hours,
        "restored_table_count": _safe_int(payload.get("restored_table_count")),
        "removed_count": removed_count,
    }


def _section_summary(section: Any) -> dict[str, object]:
    if not isinstance(section, dict):
        section = {}
    def count(name: str) -> int:
        value = section.get(name)
        return len(value) if isinstance(value, list) else 0
    reclaimable = _safe_int(section.get("reclaimable_bytes"), maximum=10**18) or 0
    return {
        "protected_count": count("protected"),
        "retained_count": count("retained"),
        "deleted_count": count("deleted"),
        "reclaimable_bytes": reclaimable,
    }


def summarize_retention(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EvidenceInputError("retention report is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EvidenceInputError("retention report is not an object")
    categories = {
        "production_releases": _section_summary(payload.get("production_releases")),
        "source_release_artifacts": _section_summary(
            payload.get("source_release_artifacts")
        ),
        "live_qa_runtime": _section_summary(payload.get("live_qa_runtime_caches")),
    }
    transient = payload.get("transient")
    transient_bytes: dict[str, int] = {}
    transient_summary: dict[str, dict[str, int]] = {}
    if isinstance(transient, dict):
        raw_bytes = transient.get("reclaimable_bytes")
        if isinstance(raw_bytes, dict):
            for category in (
                "failed_builds",
                "browser_test_artifacts",
                "preprod_screenshots",
            ):
                transient_bytes[category] = _safe_int(raw_bytes.get(category)) or 0
        for category in (
            "failed_builds",
            "browser_test_artifacts",
            "preprod_screenshots",
        ):
            section = transient.get(category)
            if not isinstance(section, dict):
                section = {}
            transient_summary[category] = {
                "count": _safe_int(section.get("count")) or 0,
                "reclaimable_bytes": transient_bytes.get(category, 0),
            }
    disk_after = payload.get("disk_after")
    if not isinstance(disk_after, dict):
        disk_after = {}
    free_bytes = _safe_int(disk_after.get("free_bytes"))
    used_percent = _safe_percent(disk_after.get("used_percent"))
    disk_before = payload.get("disk_before")
    if not isinstance(disk_before, dict):
        disk_before = {}
    limits = payload.get("limits")
    if not isinstance(limits, dict):
        limits = {}
    duration_seconds = payload.get("duration_seconds")
    if (
        not isinstance(duration_seconds, (int, float))
        or isinstance(duration_seconds, bool)
        or duration_seconds < 0
        or duration_seconds > 10**9
    ):
        duration_seconds = None
    backup = payload.get("backup")
    if not isinstance(backup, dict):
        backup = {}
    backup_status = backup.get("status")
    if not isinstance(backup_status, str) or backup_status not in {"completed", "skipped"}:
        backup_status = "unknown"
    restore_verified = backup.get("restore_verified")
    if not isinstance(restore_verified, bool):
        restore_verified = None
    backup_duration_seconds = backup.get("duration_seconds")
    if (
        not isinstance(backup_duration_seconds, (int, float))
        or isinstance(backup_duration_seconds, bool)
        or backup_duration_seconds < 0
        or backup_duration_seconds > 10**9
    ):
        backup_duration_seconds = None
    return {
        "schema": SCHEMA,
        "kind": "storage_retention",
        "status": "ok" if isinstance(payload.get("ok"), bool) else "unknown",
        "ok": payload.get("ok") is True,
        "mode": (
            payload.get("mode")
            if isinstance(payload.get("mode"), str)
            and payload.get("mode") in {"apply", "dry-run"}
            else "unknown"
        ),
        "categories": categories,
        "transient": transient_summary,
        "transient_reclaimable_bytes": transient_bytes,
        "duration_seconds": duration_seconds,
        "limits": {
            "minimum_free_bytes": _safe_int(limits.get("minimum_free_bytes")),
            "maximum_used_percent": _safe_percent(limits.get("maximum_used_percent")),
        },
        "disk_before": {
            "free_bytes": _safe_int(disk_before.get("free_bytes")),
            "used_percent": _safe_percent(disk_before.get("used_percent")),
        },
        "disk_after": {
            "free_bytes": free_bytes,
            "used_percent": used_percent,
        },
        "backup": {
            "status": backup_status,
            "restore_verified": restore_verified,
            "alembic_revision_verified": (
                backup.get("alembic_revision_verified")
                if isinstance(backup.get("alembic_revision_verified"), bool)
                else None
            ),
            "checksum_present": backup.get("checksum_present") is True,
            "size_bytes": _safe_int(backup.get("size_bytes")),
            "duration_seconds": backup_duration_seconds,
            "restored_table_count": _safe_int(backup.get("restored_table_count")),
            "removed_count": _safe_int(backup.get("removed_count")),
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emit fixed-schema storage evidence.")
    parser.add_argument(
        "--mode",
        required=True,
        choices=(
            "df",
            "inode",
            "du",
            "journal",
            "service",
            "lock",
            "backup",
            "retention",
        ),
    )
    parser.add_argument("--category", default="root")
    parser.add_argument("--service", default="deadlock-web")
    parser.add_argument("--phase", choices=("create", "check"), default="create")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        raw = _read_input()
        if args.mode == "df":
            result = summarize_df(raw, category=args.category)
        elif args.mode == "inode":
            result = summarize_inode(raw, category=args.category)
        elif args.mode == "du":
            result = summarize_du(raw, category=args.category)
        elif args.mode == "journal":
            result = summarize_journal(raw)
        elif args.mode == "service":
            result = summarize_service(raw, service=args.service)
        elif args.mode == "lock":
            result = summarize_lock(raw)
        elif args.mode == "backup":
            result = summarize_backup(raw, phase=args.phase)
        else:
            result = summarize_retention(raw)
    except EvidenceInputError:
        print("storage evidence unavailable: input_contract", file=sys.stderr)
        return 2
    except Exception:
        # A malformed producer value must not turn an internal traceback into
        # a public diagnostic, even when this helper is called directly.
        print("storage evidence unavailable: internal", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
