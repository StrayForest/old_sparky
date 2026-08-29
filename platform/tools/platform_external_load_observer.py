#!/usr/bin/env python3
"""Collect origin resource evidence while an external load is running."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import time

from platform_production_qa import (
    SystemSampler,
    collect_api_journal_lines,
    load_env_file,
    summarize_request_perf_logs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Observe one external load window on the origin.")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--max-runtime", type=float, default=9_000.0)
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("external load observer must run as root")
    if not 0.25 <= args.interval <= 60:
        raise ValueError("observer interval must be between 0.25 and 60 seconds")
    if not 1 <= args.max_runtime <= 18_000:
        raise ValueError("observer max-runtime is outside the supported bound")
    load_env_file(args.env_file)

    sampler = SystemSampler(interval_seconds=args.interval)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signal_name, stop_event.set)
        except NotImplementedError:
            pass

    started_at = datetime.now(UTC)
    journal_since = started_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    started_monotonic = time.monotonic()
    await sampler.start()
    timed_out = False
    try:
        while not args.stop_file.exists() and not stop_event.is_set():
            if time.monotonic() - started_monotonic >= args.max_runtime:
                timed_out = True
                break
            await asyncio.sleep(min(1.0, args.interval))
    finally:
        await sampler.stop()

    finished_at = datetime.now(UTC)
    journal_until = finished_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    request_perf_lines = collect_api_journal_lines(journal_since, journal_until)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "stop_file_seen": args.stop_file.exists(),
        "timed_out": timed_out,
        "system": sampler.summary(),
        "server_request_perf_logs": summarize_request_perf_logs(
            request_perf_lines,
            tournament_slug=None,
        ),
    }
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0 if not timed_out else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
