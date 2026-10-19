#!/usr/bin/env python3
"""Project one bounded backup-failure receipt into closed diagnostic values.

This dependency-free source is embedded verbatim in the production storage
diagnostics workflow. Tests check that copy and exercise this implementation
with synthetic receipts; it is not installed on production hosts.
"""

from __future__ import annotations

import json
import re
import sys

MAX_BYTES = 64 * 1024
MAX_MEMORY_BYTES = 1 << 30
MAX_RSS_BYTES = 1 << 40


class ReceiptError(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError
        result[key] = value
    return result


def _integer(value: object, *, maximum: int = 10**18, nullable: bool = False) -> bool:
    return (nullable and value is None) or (type(value) is int and 0 <= value <= maximum)


def project_receipt(raw: bytes, run_id: str, attempt: str, source_sha: str) -> dict[str, object]:
    if (
        re.fullmatch(r"[1-9][0-9]{0,19}", run_id) is None
        or re.fullmatch(r"[1-9][0-9]{0,5}", attempt) is None
        or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
        or not 1 <= len(raw) <= MAX_BYTES
    ):
        raise ReceiptError
    try:
        payload = json.loads(raw.decode("ascii"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ReceiptError) as exc:
        raise ReceiptError from exc
    if not isinstance(payload, dict):
        raise ReceiptError
    required = {
        "schema", "event", "run_id", "attempt", "operation", "source_sha",
        "bundle_sha256", "stage", "public_summary_error",
        "public_summary_normalizer_diagnostic", "exit_code", "report_state",
        "report_error_class", "restore_diagnostic", "scratch_unit_diagnostic",
        "scratch_admission_diagnostic", "backup_disk_diagnostic",
        "scratch_host_guard_diagnostic", "scratch_first_fault",
        "scratch_resource_attribution", "scratch_phase_attribution",
        "scratch_cleanup_status", "report_bytes", "report_sha256", "stderr_state",
        "stderr_exception_class", "stderr_bytes", "stderr_sha256",
    }
    if set(payload) not in (required, required | {"scratch_allocation_attribution"}):
        raise ReceiptError
    if (
        type(payload.get("schema")) is not int or payload["schema"] != 1
        or payload.get("event") != "platform_backup_failure"
        or payload.get("run_id") != run_id
        or payload.get("attempt") != attempt
        or payload.get("source_sha") != source_sha
        or payload.get("operation") != "create"
    ):
        raise ReceiptError

    def enum(value: object, choices: set[str]) -> str:
        if not isinstance(value, str) or value not in choices:
            raise ReceiptError
        return value

    stage = enum(payload["stage"], {
        "preflight", "helper_bundle_validation", "backup_create",
        "backup_verify_existing", "summary_validation", "public_summary",
        "private_capture_cleanup", "helper_cleanup", "report_cleanup", "complete",
    })
    report_state = enum(payload["report_state"], {
        "absent", "unsafe", "changed", "read", "invalid_json", "typed_failure",
        "success_payload", "unclassified_json",
    })
    report_error = enum(payload["report_error_class"], {
        "none", "backup", "lock", "integrity", "configuration", "storage",
    })
    if type(payload["exit_code"]) is not int or not 1 <= payload["exit_code"] <= 999:
        raise ReceiptError

    projected: dict[str, object] = {
        "schema": 1,
        "kind": "backup_failure_diagnostic",
        "status": "available",
        "run_id": run_id,
        "attempt": attempt,
        "source_sha": source_sha,
        "operation": "create",
        "stage": stage,
        "report_state": report_state,
        "report_error_class": report_error,
        "exit_code": payload["exit_code"],
        "backup_disk_diagnostic": None, "restore_diagnostic": None,
        "scratch_admission_diagnostic": None,
    }
    # The remaining writer-owned diagnostic values are bounded by the 64 KiB
    # receipt and discarded. Never copy or serialize those private structures.
    for field in (
        "restore_diagnostic", "scratch_unit_diagnostic", "scratch_admission_diagnostic",
        "backup_disk_diagnostic", "scratch_host_guard_diagnostic", "scratch_first_fault",
        "scratch_resource_attribution", "scratch_phase_attribution", "scratch_cleanup_status",
    ):
        value = payload[field]
        if value is not None and not isinstance(value, dict):
            raise ReceiptError

    disk = payload["backup_disk_diagnostic"]
    if disk is not None:
        if set(disk) != {"schema", "phase", "guard_reason", "free_bytes", "required_free_bytes"}:
            raise ReceiptError
        if (
            type(disk["schema"]) is not int or disk["schema"] != 1
            or disk["phase"] not in {"backup_admission", "pg_dump_pre_spawn", "pg_dump_post_start", "pg_dump_monitor", "pg_dump_post_exit"}
            or disk["guard_reason"] not in {"disk_floor", "disk_unavailable", "child_unstopped", "command_start_failed", "command_failed"}
            or not _integer(disk["free_bytes"], nullable=True)
            or not _integer(disk["required_free_bytes"], nullable=True)
        ):
            raise ReceiptError
        if disk["guard_reason"] == "disk_floor" and (
            type(disk["free_bytes"]) is not int or type(disk["required_free_bytes"]) is not int
            or disk["free_bytes"] >= disk["required_free_bytes"]
        ):
            raise ReceiptError
        projected["backup_disk_diagnostic"] = {
            "phase": disk["phase"], "guard_reason": disk["guard_reason"],
            "free_bytes": disk["free_bytes"], "required_free_bytes": disk["required_free_bytes"],
        }

    admission = payload["scratch_admission_diagnostic"]
    if admission is not None:
        keys = {
            "schema", "phase", "guard_reason", "available_bytes", "required_bytes",
            "helper_rss_bytes", "direct_parent_rss_bytes", "observed_pair_rss_bytes",
            "observed_process_count",
        }
        if set(admission) != keys:
            raise ReceiptError
        helper_rss = admission["helper_rss_bytes"]
        parent_rss = admission["direct_parent_rss_bytes"]
        count = admission["observed_process_count"]
        pair = admission["observed_pair_rss_bytes"]
        if (
            type(admission["schema"]) is not int or admission["schema"] != 1
            or admission["phase"] != "pre_create_admission"
            or admission["guard_reason"] != "memory_reserve"
            or not _integer(admission["available_bytes"], maximum=MAX_MEMORY_BYTES - 1)
            or type(admission["required_bytes"]) is not int or admission["required_bytes"] != MAX_MEMORY_BYTES
            or any(not _integer(value, maximum=MAX_RSS_BYTES, nullable=True) for value in (helper_rss, parent_rss))
            or type(count) is not int or count != sum(value is not None for value in (helper_rss, parent_rss))
            or count not in {0, 1, 2}
            or (count == 2 and (type(pair) is not int or pair != helper_rss + parent_rss or pair > 2 * MAX_RSS_BYTES))
            or (count < 2 and pair is not None)
        ):
            raise ReceiptError
        projected["scratch_admission_diagnostic"] = {
            "phase": admission["phase"], "guard_reason": admission["guard_reason"],
            "available_bytes": admission["available_bytes"], "required_bytes": admission["required_bytes"],
            "observed_process_count": count,
        }

    restore = payload["restore_diagnostic"]
    if restore is not None:
        keys = {
            "schema", "restore_stage", "guard_reason", "free_bytes", "required_free_bytes",
            "temporary_database_created", "drop_outcome", "temporary_database_absent",
            "archive_sha256", "archive_size_bytes",
        }
        stages = {
            "pre_create_admission", "create_database", "extension_setup", "schema_setup",
            "restore_platform", "restore_public", "validate_table_count", "validate_connectivity",
            "validate_revision", "validate_extensions", "drop_database",
        }
        drops = {"not_required", "drop_failed", "database_present", "absence_unconfirmed", "confirmed_absent"}
        if set(restore) != keys or (
            type(restore["schema"]) is not int or restore["schema"] != 1
            or restore["restore_stage"] not in stages
            or restore["guard_reason"] not in {"none", "disk_floor", "disk_unavailable", "child_unstopped", "command_start_failed", "command_failed"}
            or restore["drop_outcome"] not in drops
            or any(value is not None and type(value) is not bool for value in (restore["temporary_database_created"], restore["temporary_database_absent"]))
            or not _integer(restore["free_bytes"], nullable=True)
            or not _integer(restore["required_free_bytes"], nullable=True)
            or not isinstance(restore["archive_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", restore["archive_sha256"]) is None
            or not _integer(restore["archive_size_bytes"])
        ):
            raise ReceiptError
        projected["restore_diagnostic"] = {
            "restore_stage": restore["restore_stage"], "guard_reason": restore["guard_reason"],
            "free_bytes": restore["free_bytes"], "required_free_bytes": restore["required_free_bytes"],
            "temporary_database_created": restore["temporary_database_created"],
            "drop_outcome": restore["drop_outcome"],
            "temporary_database_absent": restore["temporary_database_absent"],
            "archive_size_bytes": restore["archive_size_bytes"],
        }
    return projected


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print("{\"state\":\"unavailable\",\"reason\":\"invalid_binding\"}")
        return 0
    run_id, attempt, source_sha = args
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    try:
        result = project_receipt(raw, run_id, attempt, source_sha)
    except (ReceiptError, ValueError, TypeError):
        result = {
            "schema": 1,
            "kind": "backup_failure_diagnostic",
            "status": "unavailable",
            "run_id": run_id,
            "attempt": attempt,
            "source_sha": source_sha,
            "reason": "receipt_contract",
        }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
