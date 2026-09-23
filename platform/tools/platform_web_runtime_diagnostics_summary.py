#!/usr/bin/env python3
"""Emit fixed, aggregate-only web runtime diagnostics.

The outage workflow may receive systemd/journal text containing request paths,
hosts, addresses or command lines.  This module consumes that text through a
bounded binary stream and returns only allowlisted properties, counters and
timestamps.  It never echoes source lines or arbitrary property values.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import re
import sys
from typing import BinaryIO, Iterable, Iterator


SCHEMA = 1
MAX_LINES = 600
MAX_LINE_BYTES = 8192
MAX_INPUT_BYTES = (MAX_LINES + 1) * (MAX_LINE_BYTES + 1)
TIMESTAMP_BODY = (
    rb"20[0-9]{2}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:"
    rb"[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?"
)
JOURNAL_TIMESTAMP_RE = re.compile(rb"^\s*(" + TIMESTAMP_BODY + rb")")
SYSTEMD_TIMESTAMP_RE = re.compile(
    rb"^\s*(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+)?(" + TIMESTAMP_BODY + rb")"
)
DURATION_RE = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?)(?:ns|us|ms|s|min|h|d)")

PROPERTY_KEYS = (
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
    "NRestarts",
    "Restart",
    "RestartUSec",
    "MemoryCurrent",
    "MemoryPeak",
    "MemoryMax",
    "TasksCurrent",
    "TasksMax",
    "CPUUsageNSec",
    "OOMPolicy",
    "WatchdogUSec",
)
NUMERIC_PROPERTIES = frozenset(
    {
        "ExecMainStatus",
        "NRestarts",
        "MemoryCurrent",
        "MemoryPeak",
        "MemoryMax",
        "TasksCurrent",
        "TasksMax",
        "CPUUsageNSec",
    }
)
DURATION_PROPERTIES = frozenset({"RestartUSec", "WatchdogUSec"})
ENUM_VALUES = {
    "ActiveState": frozenset(
        {"active", "inactive", "failed", "activating", "deactivating", "reloading"}
    ),
    "SubState": frozenset(
        {
            "dead",
            "running",
            "exited",
            "failed",
            "start",
            "stop",
            "auto-restart",
            "condition",
            "reload",
        }
    ),
    "Result": frozenset(
        {
            "success",
            "exit-code",
            "signal",
            "core-dump",
            "timeout",
            "watchdog",
            "resources",
            "start-limit-hit",
            "oom-kill",
        }
    ),
    "ExecMainCode": frozenset({"exited", "killed", "dumped"}),
    "Restart": frozenset(
        {
            "no",
            "on-success",
            "on-failure",
            "on-abnormal",
            "on-watchdog",
            "on-abort",
            "always",
        }
    ),
    "OOMPolicy": frozenset({"stop", "continue", "kill"}),
}

WEB_LOG_CLASSES: tuple[tuple[str, tuple[bytes, ...]], ...] = (
    (
        "oom",
        (
            b"out of memory",
            b"oom-kill",
            b"oom_reaper",
            b"killed process",
            b"memory cgroup",
            b"invoked oom-killer",
        ),
    ),
    (
        "shutdown",
        (b"sigterm", b"signal=15", b"shutdown", b"grace period", b"stopping"),
    ),
    ("restart", (b"restart", b"replaced", b"auto-restart")),
    ("error", (b"error", b"exception", b"fatal", b"failed", b"failure")),
    ("startup", (b"started", b"listening", b"ready", b"accepting connections")),
)
KERNEL_LOG_CLASSES: tuple[tuple[str, tuple[bytes, ...]], ...] = (
    WEB_LOG_CLASSES[0],
)


def _bounded_input_lines(
    stream: BinaryIO,
    *,
    max_lines: int,
) -> Iterator[bytes]:
    """Read at most ``max_lines + 1`` lines and a bounded byte budget.

    ``readline(size)`` prevents one malformed/giant log line from being
    materialized in memory.  Continuation chunks are drained only up to the
    same total budget so the diagnostic command remains resource-bounded.
    """

    remaining_bytes = min(MAX_INPUT_BYTES, (max_lines + 1) * (MAX_LINE_BYTES + 1))
    emitted = 0
    while emitted <= max_lines and remaining_bytes > 0:
        chunk = stream.readline(min(MAX_LINE_BYTES + 1, remaining_bytes))
        if not chunk:
            return
        remaining_bytes -= len(chunk)
        emitted += 1
        yield chunk
        if chunk.endswith(b"\n"):
            continue
        while remaining_bytes > 0:
            continuation = stream.readline(min(MAX_LINE_BYTES + 1, remaining_bytes))
            if not continuation:
                return
            remaining_bytes -= len(continuation)
            if continuation.endswith(b"\n"):
                break


def _timestamp(raw_line: bytes, *, journal_prefix: bool = False) -> str | None:
    matcher = JOURNAL_TIMESTAMP_RE if journal_prefix else SYSTEMD_TIMESTAMP_RE
    match = matcher.search(raw_line[:MAX_LINE_BYTES])
    if match is None:
        return None
    return match.group(1).decode("ascii")


def _classify_log_line(
    raw_line: bytes,
    *,
    classes: tuple[tuple[str, tuple[bytes, ...]], ...],
) -> str:
    lowered = raw_line[:MAX_LINE_BYTES].lower()
    for label, markers in classes:
        if any(marker in lowered for marker in markers):
            return label
    return "other"


def summarize_log_lines(
    lines: Iterable[bytes],
    *,
    kind: str,
    max_lines: int = MAX_LINES,
) -> dict[str, object]:
    """Summarize journal-like lines into fixed classes and timestamps."""

    if kind not in {"web_journal", "kernel_oom"}:
        raise ValueError("unsupported log summary kind")
    if not 1 <= max_lines <= MAX_LINES:
        raise ValueError(f"max_lines must be between 1 and {MAX_LINES}")
    classes = WEB_LOG_CLASSES if kind == "web_journal" else KERNEL_LOG_CLASSES
    class_names = tuple(label for label, _ in classes) + ("other",)
    counts: Counter[str] = Counter({label: 0 for label in class_names})
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    line_count = 0
    overlong_lines = 0
    truncated = False
    for raw_line in lines:
        if line_count >= max_lines:
            truncated = True
            break
        line_count += 1
        if len(raw_line) > MAX_LINE_BYTES:
            overlong_lines += 1
        counts[_classify_log_line(raw_line, classes=classes)] += 1
        # Journal timestamps are valid only at the record prefix.  This keeps
        # a date embedded in a request URL/query from becoming artifact data.
        timestamp = _timestamp(raw_line, journal_prefix=True)
        if timestamp is not None:
            first_timestamp = first_timestamp or timestamp
            last_timestamp = timestamp
    return {
        "schema": SCHEMA,
        "kind": kind,
        "status": "ok" if line_count else "empty",
        "line_count": line_count,
        "overlong_lines": overlong_lines,
        "truncated": truncated,
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "class_counts": dict(sorted(counts.items())),
    }


def _empty_properties() -> dict[str, object]:
    return {key: None for key in PROPERTY_KEYS}


def _safe_property_value(key: str, value: str) -> object:
    if key in NUMERIC_PROPERTIES:
        if value.isdigit() and len(value) <= 20:
            return int(value)
        if value == "infinity":
            return value
        return None
    if key in DURATION_PROPERTIES:
        normalized = value.strip().lower()
        return normalized if DURATION_RE.fullmatch(normalized) else None
    if key.endswith("Timestamp"):
        return _timestamp(value.encode("utf-8", errors="ignore"))
    if key in ENUM_VALUES:
        normalized = value.strip().lower()
        return normalized if normalized in ENUM_VALUES[key] else "unknown"
    return None


def summarize_properties(
    lines: Iterable[bytes],
    *,
    service: str,
) -> dict[str, object]:
    """Return only allowlisted, normalized systemd property values."""

    if service not in {"web", "nginx"}:
        raise ValueError("unsupported service")
    values = _empty_properties()
    recognized = 0
    for raw_line in lines:
        key_bytes, separator, value_bytes = raw_line.partition(b"=")
        if not separator:
            continue
        try:
            key = key_bytes.decode("ascii")
            value = value_bytes.decode("utf-8", errors="ignore").rstrip("\r\n")
        except UnicodeError:
            continue
        if key not in values:
            continue
        values[key] = _safe_property_value(key, value)
        recognized += 1
    return {
        "schema": SCHEMA,
        "kind": f"{service}_systemd_properties",
        "status": "ok" if recognized else "empty",
        "properties": {key: values[key] for key in PROPERTY_KEYS},
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emit fixed aggregate-only web runtime diagnostics."
    )
    parser.add_argument(
        "--mode",
        choices=("properties", "journal", "kernel"),
        required=True,
    )
    parser.add_argument("--service", choices=("web", "nginx"), default="web")
    parser.add_argument("--max-lines", type=int, default=MAX_LINES)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, stream: BinaryIO | None = None) -> int:
    args = parse_args(argv)
    source = stream or sys.stdin.buffer
    try:
        if args.mode == "properties":
            summary = summarize_properties(
                _bounded_input_lines(source, max_lines=64),
                service=args.service,
            )
        else:
            summary = summarize_log_lines(
                _bounded_input_lines(source, max_lines=args.max_lines),
                kind="web_journal" if args.mode == "journal" else "kernel_oom",
                max_lines=args.max_lines,
            )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
