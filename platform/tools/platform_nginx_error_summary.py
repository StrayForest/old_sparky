#!/usr/bin/env python3
"""Summarize bounded Nginx error evidence without returning request data.

The production outage workflow may read a short tail of the Nginx error log,
but this tool emits only counts and stable error classes.  It deliberately
never prints a source line, client address, virtual host, request URI,
upstream URL or arbitrary log text.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import re
import sys
from typing import BinaryIO, Iterable


MAX_LINES = 300
MAX_LINE_BYTES = 8192
MAX_INPUT_BYTES = (MAX_LINES + 1) * (MAX_LINE_BYTES + 1)
SEVERITY_RE = re.compile(rb"\[([A-Za-z]+)\]")
KNOWN_SEVERITIES = frozenset(
    {"debug", "info", "notice", "warn", "error", "crit", "alert", "emerg"}
)
ERROR_CLASSES: tuple[tuple[str, tuple[bytes, ...]], ...] = (
    (
        "upstream_connect_failed",
        (b"connect() failed", b"connection refused", b"no route to host"),
    ),
    ("upstream_timeout", (b"upstream timed out", b"connection timed out")),
    (
        "upstream_reset",
        (b"connection reset by peer", b"broken pipe", b"connection aborted"),
    ),
    (
        "upstream_premature_close",
        (b"upstream prematurely closed", b"prematurely closed connection"),
    ),
    (
        "upstream_invalid_response",
        (b"upstream sent invalid", b"invalid header", b"invalid response"),
    ),
    (
        "client_closed",
        (b"client prematurely closed", b"client closed connection"),
    ),
    ("client_timeout", (b"client timed out",)),
    ("worker_process", (b"worker process", b"signal process")),
)


def _bounded_input_lines(
    stream: BinaryIO,
    *,
    max_lines: int,
) -> Iterable[bytes]:
    """Read bounded chunks without materializing an arbitrarily long line."""

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


def _severity(raw_line: bytes) -> str:
    match = SEVERITY_RE.search(raw_line[:MAX_LINE_BYTES])
    if match is None:
        return "unknown"
    severity = match.group(1).decode("ascii", errors="ignore").lower()
    return severity if severity in KNOWN_SEVERITIES else "unknown"


def _error_class(raw_line: bytes) -> str:
    lowered = raw_line[:MAX_LINE_BYTES].lower()
    for label, markers in ERROR_CLASSES:
        if any(marker in lowered for marker in markers):
            return label
    return "other"


def summarize_lines(
    lines: Iterable[bytes],
    *,
    max_lines: int = MAX_LINES,
) -> dict[str, object]:
    """Return bounded metadata for an iterable of raw Nginx log lines."""

    if not 1 <= max_lines <= MAX_LINES:
        raise ValueError(f"max_lines must be between 1 and {MAX_LINES}")

    severities: Counter[str] = Counter(
        {severity: 0 for severity in (*sorted(KNOWN_SEVERITIES), "unknown")}
    )
    class_names = tuple(label for label, _ in ERROR_CLASSES) + ("other",)
    error_classes: Counter[str] = Counter({label: 0 for label in class_names})
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
        severities[_severity(raw_line)] += 1
        error_classes[_error_class(raw_line)] += 1

    return {
        "schema": 1,
        "kind": "nginx_error",
        "status": "ok" if line_count else "empty",
        "line_count": line_count,
        "overlong_lines": overlong_lines,
        "truncated": truncated,
        "severity_counts": dict(sorted(severities.items())),
        "error_class_counts": dict(sorted(error_classes.items())),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emit bounded, request-free Nginx error metadata."
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=MAX_LINES,
        help=f"Maximum input lines to inspect (1-{MAX_LINES}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, stream: BinaryIO | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = summarize_lines(
            _bounded_input_lines(stream or sys.stdin.buffer, max_lines=args.max_lines),
            max_lines=args.max_lines,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
