#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_SERVICES = ("deadlock-api", "deadlock-worker", "deadlock-web", "nginx")


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a lightweight, read-only production health gate and emit one "
            "redacted JSON record for journald."
        )
    )
    parser.add_argument("--api-ready-url", default="http://127.0.0.1:8010/api/v1/health/ready")
    parser.add_argument("--backup-dir", type=Path, default=Path("/opt/oldsparky/platform/shared/backups"))
    parser.add_argument("--backup-max-age-hours", type=float, default=36.0)
    parser.add_argument("--disk-path", type=Path, default=Path("/opt/oldsparky/platform"))
    parser.add_argument("--disk-max-used-percent", type=float, default=80.0)
    parser.add_argument("--memory-min-available-percent", type=float, default=10.0)
    parser.add_argument(
        "--certificate",
        type=Path,
        default=Path("/opt/oldsparky/platform/shared/tls/old-sparky.com-origin.pem"),
    )
    parser.add_argument("--certificate-min-days", type=int, default=30)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--service", action="append", dest="services")
    return parser.parse_args()


def check_service(name: str) -> Check:
    result = subprocess.run(
        ["systemctl", "is-active", name],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    state = (result.stdout.strip() or result.stderr.strip() or "unknown")[:80]
    return Check(name=f"service:{name}", ok=result.returncode == 0 and state == "active", detail={"state": state})


def check_api_ready(url: str, *, timeout: float) -> Check:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        return Check("api_ready", False, {"error": "non_loopback_url"})
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "oldsparky-health/1"})
    try:
        # The operator URL is constrained to loopback above.
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            body = response.read(8_192)
            payload = json.loads(body)
            ok = response.status == 200 and payload == {
                "status": "ok",
                "service": "deadlock-platform-api",
            }
            return Check("api_ready", ok, {"status": response.status})
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        return Check("api_ready", False, {"error": type(exc).__name__})


def check_disk(path: Path, *, max_used_percent: float) -> Check:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return Check("disk", False, {"error": type(exc).__name__, "path": str(path)})
    used_percent = ((usage.total - usage.free) / usage.total) * 100 if usage.total else 100.0
    return Check(
        "disk",
        used_percent < max_used_percent,
        {
            "path": str(path),
            "used_percent": round(used_percent, 1),
            "free_gib": round(usage.free / (1024**3), 2),
            "threshold_percent": max_used_percent,
        },
    )


def read_memory_info(path: Path = Path("/proc/meminfo")) -> tuple[int, int]:
    values: dict[str, int] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in raw_line:
            continue
        key, raw_value = raw_line.split(":", 1)
        first_value = raw_value.strip().split(maxsplit=1)[0]
        if first_value.isdigit():
            values[key] = int(first_value)
    return values["MemTotal"], values["MemAvailable"]


def check_memory(*, min_available_percent: float, meminfo_path: Path = Path("/proc/meminfo")) -> Check:
    try:
        total_kib, available_kib = read_memory_info(meminfo_path)
    except (OSError, KeyError, ValueError) as exc:
        return Check("memory", False, {"error": type(exc).__name__})
    available_percent = (available_kib / total_kib) * 100 if total_kib else 0.0
    return Check(
        "memory",
        available_percent >= min_available_percent,
        {
            "available_percent": round(available_percent, 1),
            "available_mib": round(available_kib / 1024),
            "threshold_percent": min_available_percent,
        },
    )


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(UTC)


def check_backup(directory: Path, *, max_age_hours: float, now: datetime | None = None) -> Check:
    current_time = now or datetime.now(UTC)
    try:
        candidates = sorted(directory.glob("platformdb-*.json"), key=lambda item: item.stat().st_mtime)
        if not candidates:
            raise FileNotFoundError("backup metadata missing")
        metadata_path = candidates[-1]
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        created_at = parse_timestamp(metadata.get("completed_at_utc", metadata.get("created_at")))
        dump_name = metadata.get("dump_file")
        if not isinstance(dump_name, str) or Path(dump_name).name != dump_name:
            raise ValueError("invalid dump file name")
        dump_path = directory / dump_name
        if not dump_path.is_file() or dump_path.stat().st_size <= 0:
            raise FileNotFoundError("backup archive missing")
        if metadata.get("restore_verified") is not True:
            raise ValueError("latest backup is not restore verified")
        age_hours = max(0.0, (current_time - created_at).total_seconds() / 3600)
        return Check(
            "backup",
            age_hours <= max_age_hours,
            {
                "age_hours": round(age_hours, 1),
                "max_age_hours": max_age_hours,
                "restore_verified": True,
                "archive_bytes": dump_path.stat().st_size,
            },
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return Check("backup", False, {"error": type(exc).__name__})


def check_certificate(path: Path, *, min_days: int) -> Check:
    if min_days < 0:
        return Check("certificate", False, {"error": "invalid_threshold"})
    try:
        result = subprocess.run(
            ["openssl", "x509", "-in", str(path), "-noout", "-checkend", str(min_days * 86_400)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("certificate", False, {"error": type(exc).__name__})
    return Check(
        "certificate",
        result.returncode == 0,
        {"path": str(path), "minimum_remaining_days": min_days},
    )


def run_checks(args: argparse.Namespace) -> list[Check]:
    services = tuple(args.services or DEFAULT_SERVICES)
    checks: list[Check] = [check_service(name) for name in services]
    checks.extend(
        [
            check_api_ready(args.api_ready_url, timeout=args.http_timeout),
            check_disk(args.disk_path, max_used_percent=args.disk_max_used_percent),
            check_memory(min_available_percent=args.memory_min_available_percent),
            check_backup(args.backup_dir, max_age_hours=args.backup_max_age_hours),
            check_certificate(args.certificate, min_days=args.certificate_min_days),
        ]
    )
    return checks


def main() -> int:
    args = parse_args()
    checks = run_checks(args)
    failed = [check.name for check in checks if not check.ok]
    report = {
        "event": "platform_health_monitor",
        "timestamp": datetime.now(UTC).isoformat(),
        "ok": not failed,
        "failed": failed,
        "checks": [asdict(check) for check in checks],
    }
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
