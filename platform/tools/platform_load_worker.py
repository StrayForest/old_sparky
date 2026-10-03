#!/usr/bin/env python3
"""Private child entry point for the supervised external load runner."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

try:
    from tools.platform_load import get_profile, run_profile_worker
    from tools.platform_load_runtime import worker_entry
except ModuleNotFoundError:  # Direct execution from platform/tools.
    from platform_load import get_profile, run_profile_worker
    from platform_load_runtime import worker_entry


def _worker(config: Mapping[str, Any]) -> Mapping[str, Any]:
    profile_id = config.get("profile_id")
    manifest_path = config.get("manifest_path")
    report_path = config.get("worker_report_path")
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("worker profile_id is invalid")
    if not isinstance(manifest_path, str) or not manifest_path:
        raise ValueError("worker manifest_path is invalid")
    if not isinstance(report_path, str) or not report_path:
        raise ValueError("worker_report_path is invalid")
    profile = get_profile(profile_id)
    exit_code = run_profile_worker(
        profile,
        Path(manifest_path),
        Path(report_path),
        timeout_diagnostics_run_id=(
            config.get("timeout_diagnostics_run_id")
            if isinstance(config.get("timeout_diagnostics_run_id"), str)
            else None
        ),
        runtime_config=config,
    )
    payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("worker report is not an object")
    payload["worker_exit_code"] = int(exit_code)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one supervised load worker")
    parser.add_argument("--config", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return worker_entry(_worker, config_path=args.config)


if __name__ == "__main__":
    raise SystemExit(main())
