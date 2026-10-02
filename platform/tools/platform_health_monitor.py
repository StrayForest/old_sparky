#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import importlib.util
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def _load_staged_disk_policy() -> Any:
    helper_path = Path(__file__).resolve().with_name("platform_disk_policy.py")
    spec = importlib.util.spec_from_file_location(
        "_oldsparky_platform_disk_policy", helper_path
    )
    if spec is None or spec.loader is None:
        raise ImportError("platform disk policy helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_staged_backup_manifest() -> Any:
    helper_path = Path(__file__).resolve().with_name("platform_backup_manifest.py")
    spec = importlib.util.spec_from_file_location(
        "_oldsparky_platform_backup_manifest", helper_path
    )
    if spec is None or spec.loader is None:
        raise ImportError("platform backup manifest helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


try:
    from .platform_disk_policy import (
        DEFAULT_MAX_USED_PERCENT,
        DEFAULT_MIN_FREE_GIB,
        is_healthy,
        minimum_free_bytes,
        snapshot_for_path,
    )
except ImportError:  # Direct execution from the tools directory.
    try:
        from tools.platform_disk_policy import (
            DEFAULT_MAX_USED_PERCENT,
            DEFAULT_MIN_FREE_GIB,
            is_healthy,
            minimum_free_bytes,
            snapshot_for_path,
        )
    except ImportError:
        _disk_policy = _load_staged_disk_policy()
        DEFAULT_MAX_USED_PERCENT = _disk_policy.DEFAULT_MAX_USED_PERCENT
        DEFAULT_MIN_FREE_GIB = _disk_policy.DEFAULT_MIN_FREE_GIB
        is_healthy = _disk_policy.is_healthy
        minimum_free_bytes = _disk_policy.minimum_free_bytes
        snapshot_for_path = _disk_policy.snapshot_for_path


try:
    from .platform_backup_manifest import (
        BackupManifestError,
        MANIFEST_FORMAT_VERSION,
        read_manifest_file,
        sha256_private_file,
    )
except ImportError:  # Direct execution from the tools directory.
    try:
        from tools.platform_backup_manifest import (
            BackupManifestError,
            MANIFEST_FORMAT_VERSION,
            read_manifest_file,
            sha256_private_file,
        )
    except ImportError:
        _backup_manifest = _load_staged_backup_manifest()
        BackupManifestError = _backup_manifest.BackupManifestError
        MANIFEST_FORMAT_VERSION = _backup_manifest.MANIFEST_FORMAT_VERSION
        read_manifest_file = _backup_manifest.read_manifest_file
        sha256_private_file = _backup_manifest.sha256_private_file


DEFAULT_SERVICES = ("deadlock-api", "deadlock-worker", "deadlock-web", "nginx")
SERVICE_CHECK_TIMEOUT_SECONDS = 10.0
CERTIFICATE_CHECK_TIMEOUT_SECONDS = 10.0
API_CHECK_TIMEOUT_SECONDS = 5.0
HEALTH_OPERATION_BUDGET_SECONDS = (
    len(DEFAULT_SERVICES) * SERVICE_CHECK_TIMEOUT_SECONDS
    + API_CHECK_TIMEOUT_SECONDS
    + CERTIFICATE_CHECK_TIMEOUT_SECONDS
)
HEALTH_SERVICE_TIMEOUT_SECONDS = HEALTH_OPERATION_BUDGET_SECONDS + 35.0


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
    parser.add_argument("--disk-min-free-gib", type=float, default=DEFAULT_MIN_FREE_GIB)
    parser.add_argument(
        "--disk-max-used-percent", type=float, default=DEFAULT_MAX_USED_PERCENT
    )
    parser.add_argument("--memory-min-available-percent", type=float, default=10.0)
    parser.add_argument(
        "--certificate",
        type=Path,
        default=Path("/opt/oldsparky/platform/shared/tls/old-sparky.com-origin.pem"),
    )
    parser.add_argument("--certificate-min-days", type=int, default=30)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--service", action="append", dest="services")
    args = parser.parse_args()
    if not math.isfinite(args.disk_min_free_gib) or args.disk_min_free_gib < 0:
        parser.error("--disk-min-free-gib must be finite and non-negative")
    if (
        not math.isfinite(args.disk_max_used_percent)
        or not 0 < args.disk_max_used_percent <= 100
    ):
        parser.error("--disk-max-used-percent must be within (0, 100]")
    if (
        not math.isfinite(args.http_timeout)
        or args.http_timeout <= 0
        or args.http_timeout > API_CHECK_TIMEOUT_SECONDS
    ):
        parser.error(
            f"--http-timeout must be within (0, {API_CHECK_TIMEOUT_SECONDS:g}]"
        )
    return args


def check_service(name: str) -> Check:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=SERVICE_CHECK_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(
            name=f"service:{name}",
            ok=False,
            detail={"state": "unavailable", "error": type(exc).__name__},
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


def check_disk(
    path: Path,
    *,
    min_free_gib: float = DEFAULT_MIN_FREE_GIB,
    max_used_percent: float = DEFAULT_MAX_USED_PERCENT,
) -> Check:
    try:
        snapshot = snapshot_for_path(path)
    except OSError as exc:
        return Check("disk", False, {"error": type(exc).__name__, "path": str(path)})
    try:
        min_free_bytes = minimum_free_bytes(min_free_gib)
    except ValueError:
        return Check("disk", False, {"error": "invalid_threshold", "path": str(path)})
    detail: dict[str, Any] = {
        "path": str(path),
        "used_percent": round(snapshot.used_percent, 1),
        "free_gib": round(snapshot.free_bytes / (1024**3), 2),
        "minimum_free_gib": min_free_gib,
        "threshold_percent": max_used_percent,
    }
    if not snapshot.valid:
        detail["error"] = "invalid_usage"
    return Check(
        "disk",
        is_healthy(
            snapshot,
            min_free_bytes=min_free_bytes,
            max_used_percent=max_used_percent,
        ),
        detail,
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


def check_backup(directory: Path, *, max_age_hours: float, now: datetime | None = None) -> Check:
    current_time = now or datetime.now(UTC)
    try:
        directory_stat = directory.lstat()
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
            raise ValueError("backup directory is not a regular directory")
        candidates = sorted(
            directory.glob("platformdb-*.json"),
            key=lambda item: (item.stat().st_mtime_ns, item.name),
        )
        if not candidates:
            raise FileNotFoundError("backup metadata missing")
        metadata_path = candidates[-1]
        manifest_file = read_manifest_file(
            metadata_path,
            expected_owner=os.geteuid(),
            expected_group=os.getegid(),
            expected_dump_file=metadata_path.with_suffix(".dump").name,
        )
        manifest = manifest_file.manifest
        if manifest.format_version != MANIFEST_FORMAT_VERSION:
            raise ValueError("latest backup uses a legacy manifest")
        if not manifest.restore_verified or not manifest.alembic_revision_verified:
            raise ValueError("latest backup is not restore verified")
        dump_path = directory / manifest.dump_file
        actual_sha256, dump_stat = sha256_private_file(
            dump_path,
            label="Platform backup dump",
            expected_owner=os.geteuid(),
            expected_group=os.getegid(),
        )
        if actual_sha256 != manifest.sha256 or dump_stat.st_size != manifest.size_bytes:
            raise ValueError("latest backup checksum or size does not match its manifest")
        age_hours = max(0.0, (current_time - manifest.completed_at_utc).total_seconds() / 3600)
        return Check(
            "backup",
            age_hours <= max_age_hours,
            {
                "age_hours": round(age_hours, 1),
                "max_age_hours": max_age_hours,
                "restore_verified": True,
                "archive_bytes": dump_stat.st_size,
            },
        )
    except (BackupManifestError, OSError, ValueError, json.JSONDecodeError) as exc:
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
            timeout=CERTIFICATE_CHECK_TIMEOUT_SECONDS,
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
            check_disk(
                args.disk_path,
                min_free_gib=args.disk_min_free_gib,
                max_used_percent=args.disk_max_used_percent,
            ),
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
