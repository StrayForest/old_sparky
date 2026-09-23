#!/usr/bin/env python3
"""Project legacy media migration output into a closed public schema.

``platform_migrate_legacy_r2.py`` has a deliberately detailed JSON contract for
the migration operator.  That contract is private: cursors, source metadata,
paths, and producer error codes must never cross the diagnostics workflow
boundary.  This module is the only producer-specific projection used by that
workflow.  It accepts the detailed report as input and emits only fixed enum,
boolean, and bounded counter fields.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path


SCHEMA = 1
MAX_COUNT = 1_000_000
MAX_INPUT_BYTES = 256 * 1024

STATUSES = frozenset({"passed", "failed"})
ERROR_CLASSES = frozenset(
    {
        "none",
        "cutover_required",
        "source_conflict",
        "manual_conflict",
        "inventory_bound",
        "legacy_references",
        "mutation_detected",
        "unexpected_exit",
        "producer",
        "internal",
        "remote_or_transport",
    }
)
SOURCE_CLASSES = frozenset({"none", "r2", "local", "both", "mixed", "unknown"})
KNOWN_SOURCE_CLASSES = frozenset({"r2", "local", "both"})
KNOWN_OPERATION_FIELDS = (
    "r2_gets",
    "local_reads",
    "r2_head_objects",
    "cdn_gets",
    "list_objects",
    "legacy_r2_deletes",
    "local_deletes",
)
def _bounded_count(value: object) -> int:
    """Convert a producer count to a non-negative, bounded integer."""

    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        try:
            number = int(value.strip(), 10)
        except (TypeError, ValueError, OverflowError):
            return 0
    else:
        return 0
    return min(MAX_COUNT, max(0, number))


def _is_bounded_count(value: object) -> bool:
    """Whether a value can safely drive a derived boolean."""

    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value <= MAX_COUNT
    if isinstance(value, str) and value.strip().isdigit():
        try:
            return 0 <= int(value.strip(), 10) <= MAX_COUNT
        except (TypeError, ValueError, OverflowError):
            return False
    return False


def _safe_process_exit(value: object) -> tuple[int, bool]:
    """Return a bounded status byte and whether the input was a real status."""

    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        return 255, False
    return value, True


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    # Do not treat strings or mappings as collections of migration results.
    return value if isinstance(value, (list, tuple)) else ()


def _inventory_count(inventory: Mapping[str, object], field: str) -> int:
    return _bounded_count(inventory.get(field))


def _source_class(payload: Mapping[str, object]) -> str:
    """Classify sources without returning a source name or location."""

    observed: set[str] = set()
    unknown = False

    locations = payload.get("source_locations")
    if isinstance(locations, Mapping):
        for location in locations:
            if isinstance(location, str) and location in KNOWN_SOURCE_CLASSES:
                observed.add(location)
            else:
                unknown = True
    elif locations is not None:
        unknown = True

    results = payload.get("source_results")
    if isinstance(results, (list, tuple)):
        for item in results:
            if not isinstance(item, Mapping):
                unknown = True
                continue
            location = item.get("location")
            if isinstance(location, str) and location in KNOWN_SOURCE_CLASSES:
                observed.add(location)
            elif location is not None:
                unknown = True
    elif results is not None:
        unknown = True

    if unknown:
        return "unknown"
    if not observed:
        return "none"
    if len(observed) == 1:
        return next(iter(observed))
    return "mixed"


def _error_class(payload: Mapping[str, object], *, producer_exit_code: int) -> str:
    if producer_exit_code not in {0, 2}:
        return "unexpected_exit"
    if payload.get("mutated") is True:
        return "mutation_detected"

    code = payload.get("code")
    if not isinstance(code, str):
        if code is not None:
            return "internal"
        results = payload.get("source_results")
        if isinstance(results, (list, tuple)) and any(
            isinstance(item, Mapping) and item.get("ok") is False
            for item in results
        ):
            return "source_conflict"
        return "internal" if payload.get("ok") is False else "none"
    return {
        "ready": "none",
        "legacy_media_cutover_required": "cutover_required",
        "legacy_source_conflicts_present": "source_conflict",
        "manual_conflicts_present": "manual_conflict",
        "legacy_inventory_bound_exceeded": "inventory_bound",
        "checkpoint_bound_exceeded": "inventory_bound",
        "legacy_references_remain": "legacy_references",
    }.get(code, "internal")


def _report_shape_is_safe(payload: Mapping[str, object]) -> bool:
    """Require the private producer's outer shape before reporting success."""

    if not (
        isinstance(payload.get("ok"), bool)
        and isinstance(payload.get("mutated"), bool)
        and payload.get("mode") == "check"
        and isinstance(payload.get("inventory_before"), Mapping)
    ):
        return False
    for field, expected in (
        ("inventory_after", Mapping),
        ("source_locations", Mapping),
        ("operations", Mapping),
    ):
        if field in payload and not isinstance(payload[field], expected):
            return False
    results = payload.get("source_results")
    return results is None or (
        isinstance(results, (list, tuple))
        and all(isinstance(item, Mapping) for item in results)
    )


def public_summary(
    *,
    payload: Mapping[str, object] | None,
    producer_exit_code: object = 0,
    stderr_bytes: object = 0,
    parse_ok: bool = True,
) -> dict[str, object]:
    """Return the sole public contract for media migration diagnostics.

    ``payload`` is treated as hostile input.  In particular, no input string,
    mapping, list, cursor, path, source location, or producer code is copied to
    the result.  Invalid or missing fields collapse to fixed values.
    """

    data = _mapping(payload)
    # An exit status is useful for an operator, but it is not allowed to grow
    # beyond a normal process status byte in public evidence.
    exit_code, valid_exit_code = _safe_process_exit(producer_exit_code)
    error_bytes = _bounded_count(stderr_bytes)

    before = _mapping(data.get("inventory_before"))
    after = _mapping(data.get("inventory_after"))
    source_results = _sequence(data.get("source_results"))
    source_failures = sum(
        1
        for item in source_results
        if isinstance(item, Mapping) and item.get("ok") is False
    )
    source_result_count = min(MAX_COUNT, len(source_results))

    operations = _mapping(data.get("operations"))
    operation_counts = {
        field: _bounded_count(operations.get(field))
        for field in KNOWN_OPERATION_FIELDS
    }

    before_present = isinstance(data.get("inventory_before"), Mapping)
    after_present = isinstance(data.get("inventory_after"), Mapping)
    mutated = data.get("mutated") is True
    source_class = _source_class(data) if parse_ok else "unknown"
    valid_process = (
        parse_ok
        and isinstance(payload, Mapping)
        and valid_exit_code
        and exit_code in {0, 2}
        and _report_shape_is_safe(data)
        and not mutated
    )
    if not valid_exit_code:
        error_class = "unexpected_exit"
    elif parse_ok and isinstance(payload, Mapping):
        error_class = _error_class(data, producer_exit_code=exit_code)
    else:
        error_class = "producer"
    if not valid_process and error_class == "none":
        error_class = "producer"

    # A check report is only considered read-only when the producer explicitly
    # reported check mode and an exact boolean false for mutation.
    read_only = data.get("mode") == "check" and data.get("mutated") is False
    inventory_clear = (
        after_present
        and _is_bounded_count(after.get("legacy_upload_references"))
        and _is_bounded_count(after.get("manual_conflicts"))
        and _inventory_count(after, "legacy_upload_references") == 0
        and _inventory_count(after, "manual_conflicts") == 0
    )

    return {
        "schema": SCHEMA,
        "kind": "media_migration_diagnostics",
        "status": "passed" if valid_process else "failed",
        "error_class": error_class if error_class in ERROR_CLASSES else "internal",
        "source_class": source_class,
        "producer_exit_code": exit_code,
        "stderr_bytes": error_bytes,
        "mutated": mutated,
        "read_only": read_only,
        "inventory_before_present": before_present,
        "inventory_after_present": after_present,
        "inventory_clear": inventory_clear,
        "source_present": parse_ok and source_class != "none",
        "source_failures_present": source_failures > 0,
        "inventory_before_legacy_upload_references": _inventory_count(
            before, "legacy_upload_references"
        ),
        "inventory_before_packaged_asset_references": _inventory_count(
            before, "packaged_asset_references"
        ),
        "inventory_before_manual_conflicts": _inventory_count(
            before, "manual_conflicts"
        ),
        "inventory_after_legacy_upload_references": _inventory_count(
            after, "legacy_upload_references"
        ),
        "inventory_after_packaged_asset_references": _inventory_count(
            after, "packaged_asset_references"
        ),
        "inventory_after_manual_conflicts": _inventory_count(
            after, "manual_conflicts"
        ),
        "source_result_count": source_result_count,
        "source_failure_count": min(MAX_COUNT, source_failures),
        "processed_count": _bounded_count(data.get("processed")),
        "succeeded_count": _bounded_count(data.get("succeeded")),
        "failed_count": _bounded_count(data.get("failed")),
        **{f"operation_{field}": value for field, value in operation_counts.items()},
    }


def _read_json(path: Path) -> Mapping[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("private media report is unavailable")
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("private media report exceeds bound")
    value = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(value, Mapping):
        raise ValueError("private media report is not an object")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--producer-exit-code", type=int, default=0)
    parser.add_argument("--stderr-bytes", type=int, default=0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = _read_json(args.input)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        report = public_summary(
            payload=None,
            producer_exit_code=args.producer_exit_code,
            stderr_bytes=args.stderr_bytes,
            parse_ok=False,
        )
        print(json.dumps(report, separators=(",", ":")))
        return 1

    report = public_summary(
        payload=payload,
        producer_exit_code=args.producer_exit_code,
        stderr_bytes=args.stderr_bytes,
    )
    print(json.dumps(report, separators=(",", ":")))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
