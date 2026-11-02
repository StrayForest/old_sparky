#!/usr/bin/env python3
"""Collect bounded, read-only production release and service readiness evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import platform_host_tools_bundle as host_tools_bundle
import platform_host_tools_pin as host_tools_pin


APP_ROOT = Path("/opt/oldsparky/platform")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
MAX_JSON_BYTES = 512 * 1024
LOCKS = (
    "/run/lock/oldsparky-platform-release.lock",
    "/run/lock/oldsparky-retained-load-matrix.lock",
)
PLANS = (
    Path("/run/oldsparky-platform/performance-diagnostic-plan.api.json"),
    Path("/run/oldsparky-platform/performance-diagnostic-plan.web.json"),
)
UNITS = {
    "deadlock-api.service": (
        "oldsparky-api",
        "oldsparky-api",
        {
            "PLATFORM_API_WORKERS",
            "PLATFORM_AUTHENTICATED_READ_ADMISSION_ENABLED",
            "PLATFORM_AUTHENTICATED_READ_ADMISSION_CONCURRENCY",
            "PLATFORM_AUTHENTICATED_READ_ADMISSION_MAX_WAITERS",
            "PLATFORM_AUTHENTICATED_READ_ADMISSION_WAIT_TIMEOUT_MS",
            "PLATFORM_DB_POOL_SIZE",
        },
    ),
    "deadlock-web.service": (
        "oldsparky-web",
        "oldsparky-web",
        {"PLATFORM_WEB_WORKERS", "PLATFORM_WEB_SERVER_AUTH_TRANSPORT"},
    ),
    "deadlock-worker.service": (
        "oldsparky-worker",
        "oldsparky-worker",
        {"PLATFORM_WORKER_CONCURRENCY", "PLATFORM_WORKER_DB_POOL_SIZE"},
    ),
}
API_PROFILE = {
    "PLATFORM_API_WORKERS": "2",
    "PLATFORM_AUTHENTICATED_READ_ADMISSION_ENABLED": "true",
    "PLATFORM_AUTHENTICATED_READ_ADMISSION_CONCURRENCY": "24",
    "PLATFORM_AUTHENTICATED_READ_ADMISSION_MAX_WAITERS": "8",
    "PLATFORM_AUTHENTICATED_READ_ADMISSION_WAIT_TIMEOUT_MS": "250",
    "PLATFORM_DB_POOL_SIZE": "24",
}


class ReadinessError(ValueError):
    """A bounded read-only readiness validation failed."""


def _read_json_with_digest(path: Path) -> tuple[dict[str, object], str]:
    descriptor = -1
    try:
        path_before = path.lstat()
        if not stat.S_ISREG(path_before.st_mode) or stat.S_ISLNK(path_before.st_mode) or path_before.st_nlink != 1:
            raise ReadinessError("bounded JSON path metadata is invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != (path_before.st_dev, path_before.st_ino)
            or before.st_nlink != 1
            or before.st_size > MAX_JSON_BYTES
        ):
            raise ReadinessError("bounded JSON metadata is invalid")
        raw = bytearray()
        while len(raw) <= MAX_JSON_BYTES:
            chunk = os.read(descriptor, MAX_JSON_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
            or len(raw) != after.st_size
            or len(raw) > MAX_JSON_BYTES
        ):
            raise ReadinessError("bounded JSON changed while being read")
        path_after = path.lstat()
        if (
            not stat.S_ISREG(path_after.st_mode)
            or stat.S_ISLNK(path_after.st_mode)
            or (path_after.st_dev, path_after.st_ino) != (after.st_dev, after.st_ino)
            or path_after.st_nlink != 1
        ):
            raise ReadinessError("bounded JSON path changed while being read")
    except ReadinessError:
        raise
    except OSError as exc:
        raise ReadinessError("bounded JSON is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        value = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ReadinessError("bounded JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ReadinessError("bounded JSON must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return _read_json_with_digest(path)[0]


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReadinessError("bounded JSON contains duplicate keys")
        result[key] = value
    return result


def _run(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReadinessError("fixed read-only command failed") from exc


def _systemd_show(unit: str, properties: tuple[str, ...]) -> dict[str, str]:
    result = _run(
        ["/usr/bin/systemctl", "show", unit, *(f"--property={item}" for item in properties)]
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def _read_allowlisted_environment(pid: int, allowed: set[str]) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as exc:
        raise ReadinessError("service environment is unavailable") from exc
    if len(raw) > 1024 * 1024:
        raise ReadinessError("service environment exceeds its bound")
    observed: dict[str, str] = {}
    try:
        for item in raw.split(b"\0"):
            key, separator, value = item.partition(b"=")
            if not separator:
                continue
            name = key.decode("ascii", "strict")
            if name in allowed:
                if name in observed:
                    raise ReadinessError("duplicate allowlisted environment key")
                observed[name] = value.decode("ascii", "strict")
    except UnicodeError as exc:
        raise ReadinessError("allowlisted environment encoding is invalid") from exc
    return observed


def _release(
    root: Path,
    expected_sha: str,
    expected_manifest_sha: str,
    label: str,
) -> tuple[Path, dict[str, object]]:
    releases = (root / "releases").resolve(strict=True)
    pointer = root / label
    try:
        pointer_metadata = pointer.lstat()
    except OSError as exc:
        raise ReadinessError(f"{label} release pointer is unavailable") from exc
    if not stat.S_ISLNK(pointer_metadata.st_mode):
        raise ReadinessError(f"{label} release pointer is not a symlink")
    release_path = pointer.resolve(strict=True)
    if release_path.parent != releases or not release_path.is_dir():
        raise ReadinessError(f"{label} release pointer is invalid")
    release_json, manifest_sha = _read_json_with_digest(release_path / "RELEASE.json")
    slug = release_json.get("release_slug")
    if (
        release_json.get("source_git_commit") != expected_sha
        or manifest_sha != expected_manifest_sha
        or not isinstance(slug, str)
        or SLUG_RE.fullmatch(slug) is None
        or slug != release_path.name
    ):
        raise ReadinessError(f"{label} release identity differs")
    try:
        final_pointer = pointer.lstat()
        final_release_path = pointer.resolve(strict=True)
    except OSError as exc:
        raise ReadinessError(f"{label} release pointer changed") from exc
    if (
        not stat.S_ISLNK(final_pointer.st_mode)
        or (final_pointer.st_dev, final_pointer.st_ino)
        != (pointer_metadata.st_dev, pointer_metadata.st_ino)
        or final_release_path != release_path
    ):
        raise ReadinessError(f"{label} release pointer changed")
    return release_path, {
        "slug": slug,
        "source_sha": expected_sha,
        "release_json_sha256": manifest_sha,
    }


def _check_source_pin(current: Path, expected_host_tools_sha: str) -> dict[str, object]:
    payload = host_tools_pin._read_pin(current)
    if set(payload) != {"schema", "repository", "host_tools_sha", "closure"}:
        raise ReadinessError("deployed host-tools pin schema is invalid")
    if (
        type(payload.get("schema")) is not int
        or payload.get("schema") != host_tools_pin.SCHEMA
        or payload.get("repository") != host_tools_pin.EXPECTED_REPOSITORY
        or payload.get("host_tools_sha") != expected_host_tools_sha
    ):
        raise ReadinessError("deployed host-tools pin identity differs")
    try:
        host_tools_pin._validate_closure(current, payload.get("closure"))
    except host_tools_pin.HostToolsPinError as exc:
        raise ReadinessError("deployed host-tools source closure differs from its pin") from exc
    return payload


def _check_service_environment() -> dict[str, object]:
    services: dict[str, object] = {}
    for unit, (expected_user, expected_group, allowed) in UNITS.items():
        fields = _systemd_show(
            unit,
            ("ActiveState", "MainPID", "ControlGroup", "User", "Group", "InvocationID", "NRestarts"),
        )
        try:
            pid = int(fields.get("MainPID", "0"))
            restarts = int(fields.get("NRestarts", "-1"))
        except ValueError as exc:
            raise ReadinessError("systemd service identity is invalid") from exc
        if (
            fields.get("ActiveState") != "active"
            or pid <= 0
            or fields.get("User") != expected_user
            or fields.get("Group") != expected_group
            or not fields.get("InvocationID")
            or restarts != 0
        ):
            raise ReadinessError("systemd service state or identity is invalid")
        observed = _read_allowlisted_environment(pid, allowed)
        services[unit] = {
            "active_state": fields["ActiveState"],
            "main_pid": pid,
            "control_group": fields.get("ControlGroup"),
            "user": expected_user,
            "group": expected_group,
            "invocation_id": fields["InvocationID"],
            "restart_count": restarts,
            "observed_allowlisted": observed,
        }

    api = services["deadlock-api.service"]
    if not isinstance(api, dict) or api["observed_allowlisted"] != API_PROFILE:
        raise ReadinessError("API runtime profile differs")

    web = services["deadlock-web.service"]
    if not isinstance(web, dict):
        raise ReadinessError("web runtime profile is invalid")
    web_values = web["observed_allowlisted"]
    if (
        not isinstance(web_values, dict)
        or web_values.get("PLATFORM_WEB_SERVER_AUTH_TRANSPORT") != "fetch"
        or web_values.get("PLATFORM_WEB_WORKERS", "1") != "1"
    ):
        raise ReadinessError("web runtime profile differs")
    exec_start = _systemd_show("deadlock-web.service", ("ExecStart",)).get("ExecStart", "")
    if "/opt/oldsparky/platform/current/tools/platform_run_web.sh" not in exec_start:
        raise ReadinessError("web launcher identity differs")
    web["effective_allowlisted"] = {
        "PLATFORM_WEB_WORKERS": web_values.get("PLATFORM_WEB_WORKERS", "1"),
        "PLATFORM_WEB_SERVER_AUTH_TRANSPORT": "fetch",
    }
    web["value_sources"] = {
        "PLATFORM_WEB_WORKERS": "environment" if "PLATFORM_WEB_WORKERS" in web_values else "platform_run_web.sh default",
        "PLATFORM_WEB_SERVER_AUTH_TRANSPORT": "environment",
    }

    worker = services["deadlock-worker.service"]
    if not isinstance(worker, dict):
        raise ReadinessError("worker runtime profile is invalid")
    worker_values = worker["observed_allowlisted"]
    if (
        not isinstance(worker_values, dict)
        or worker_values.get("PLATFORM_WORKER_CONCURRENCY") != "2"
        or ("PLATFORM_WORKER_DB_POOL_SIZE" in worker_values and worker_values["PLATFORM_WORKER_DB_POOL_SIZE"] != "2")
    ):
        raise ReadinessError("worker runtime profile differs")
    config_path = Path("/opt/oldsparky/platform/current/python_packages/platform_infra/config.py")
    config = config_path.read_text(encoding="utf-8")
    if re.search(r"platform_worker_db_pool_size:\s*int\s*=\s*Field\(default=2,", config) is None:
        raise ReadinessError("worker DB-pool default source differs")
    worker["effective_allowlisted"] = {
        "PLATFORM_WORKER_CONCURRENCY": "2",
        "PLATFORM_WORKER_DB_POOL_SIZE": worker_values.get("PLATFORM_WORKER_DB_POOL_SIZE", "2"),
    }
    worker["value_sources"] = {
        "PLATFORM_WORKER_CONCURRENCY": "environment",
        "PLATFORM_WORKER_DB_POOL_SIZE": "environment" if "PLATFORM_WORKER_DB_POOL_SIZE" in worker_values else "platform_infra.config.py default",
    }
    return services


def _check_operation_marker_and_plans() -> None:
    operation_marker = APP_ROOT / "current" / ".release-operation.json"
    if os.path.lexists(operation_marker) or any(os.path.lexists(path) for path in PLANS):
        raise ReadinessError("release marker or diagnostic plan is present")


def _check_locks() -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for name in LOCKS:
        path = Path(name)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ReadinessError("canonical lock file is unavailable") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ReadinessError("canonical lock file metadata is invalid")
        result = _run(["/usr/bin/flock", "-n", name, "/bin/true"], check=False)
        if result.returncode != 0:
            raise ReadinessError("canonical lock is held")
        results.append({
            "dev": metadata.st_dev,
            "ino": metadata.st_ino,
            "mode": "0600",
            "held": False,
            "nonblocking_acquire_release": "passed",
        })
    return results


def _check_maintenance_state() -> tuple[dict[str, str], dict[str, str]]:
    timer = _systemd_show(
        "deadlock-maintenance.timer",
        ("ActiveState", "UnitFileState", "NextElapseUSecRealtime"),
    )
    service = _systemd_show("deadlock-maintenance.service", ("ActiveState", "SubState", "MainPID"))
    jobs = _run(["/usr/bin/systemctl", "list-jobs", "--no-legend", "--no-pager"]).stdout.splitlines()
    if jobs or timer.get("ActiveState") != "active" or timer.get("UnitFileState") != "enabled":
        raise ReadinessError("maintenance timer or systemd job state is not ready")
    if service.get("MainPID") != "0" or service.get("SubState") not in {"failed", "dead"}:
        raise ReadinessError("maintenance service is not terminal idle")
    return timer, service


def _check_final_consistency(
    *,
    root: Path,
    expected_app_sha: str,
    expected_previous_app_sha: str,
    expected_release_manifest_sha: str,
    expected_previous_release_manifest_sha: str,
    current_identity: tuple[Path, dict[str, object]],
    previous_identity: tuple[Path, dict[str, object]],
    services: dict[str, object],
    lock_results: list[dict[str, object]],
    timer: dict[str, str],
    maintenance_service: dict[str, str],
) -> None:
    current_final = _release(root, expected_app_sha, expected_release_manifest_sha, "current")
    previous_final = _release(
        root,
        expected_previous_app_sha,
        expected_previous_release_manifest_sha,
        "previous",
    )
    if current_final != current_identity or previous_final != previous_identity:
        raise ReadinessError("release pointers changed during readiness collection")
    if _check_service_environment() != services:
        raise ReadinessError("service identities changed during readiness collection")
    if _check_locks() != lock_results:
        raise ReadinessError("canonical locks changed during readiness collection")
    _check_operation_marker_and_plans()
    timer_final, service_final = _check_maintenance_state()
    if timer_final != timer or service_final != maintenance_service:
        raise ReadinessError("maintenance state changed during readiness collection")


def collect_readiness(
    *,
    expected_app_sha: str,
    expected_previous_app_sha: str,
    expected_release_manifest_sha: str,
    expected_previous_release_manifest_sha: str,
    expected_host_tools_sha: str,
    expected_previous_host_tools_sha: str,
    expected_toolset_version: str,
) -> dict[str, object]:
    for value in (expected_app_sha, expected_previous_app_sha, expected_host_tools_sha, expected_previous_host_tools_sha):
        if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
            raise ReadinessError("expected source SHA is invalid")
    for value in (expected_release_manifest_sha, expected_previous_release_manifest_sha):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ReadinessError("expected release-manifest SHA is invalid")
    if expected_toolset_version != host_tools_bundle.TOOLSET_VERSION:
        raise ReadinessError("expected host-tools version is unsupported")
    if os.geteuid() != 0:
        raise ReadinessError("read-only readiness collector requires root")

    root = APP_ROOT.resolve(strict=True)
    current, current_info = _release(root, expected_app_sha, expected_release_manifest_sha, "current")
    previous, previous_info = _release(root, expected_previous_app_sha, expected_previous_release_manifest_sha, "previous")
    pin = _check_source_pin(current, expected_host_tools_sha)
    generation = root / "shared/host-tools" / expected_host_tools_sha
    try:
        host_result = host_tools_bundle.verify_installed_generation(
            generation,
            expected_source_sha=expected_host_tools_sha,
            expected_toolset_version=expected_toolset_version,
        )
    except host_tools_bundle.HostToolsBundleError as exc:
        raise ReadinessError("installed host-tools generation differs from its manifest") from exc

    old_generation = root / "shared/host-tools" / expected_previous_host_tools_sha
    try:
        old_metadata = old_generation.lstat()
    except OSError as exc:
        raise ReadinessError("expected previous host-tools generation directory is unavailable") from exc
    if (
        not stat.S_ISDIR(old_metadata.st_mode)
        or stat.S_ISLNK(old_metadata.st_mode)
        or old_metadata.st_uid != 0
        or old_metadata.st_gid != 0
        or old_metadata.st_nlink != 2
        or stat.S_IMODE(old_metadata.st_mode) != 0o555
    ):
        raise ReadinessError("expected previous host-tools generation directory metadata is invalid")

    services = _check_service_environment()
    compression_path = current / "apps/platform_web/.next/required-server-files.json"
    compression = _read_json(compression_path).get("config")
    if not isinstance(compression, dict) or compression.get("compress") is not True:
        raise ReadinessError("compiled web compression is disabled")

    health: dict[str, int] = {}
    for key, url in (
        ("api_ready", "http://127.0.0.1:8010/api/v1/health/ready"),
        ("web_root", "http://127.0.0.1:3000/"),
    ):
        result = _run([
            "/usr/bin/curl", "--fail", "--silent", "--show-error", "--max-time", "5",
            "--output", "/dev/null", "--write-out", "%{http_code}", url,
        ])
        if result.stdout not in {"200", "200\n"}:
            raise ReadinessError("local service health check failed")
        health[key] = 200

    _check_operation_marker_and_plans()
    lock_results = _check_locks()
    timer, service = _check_maintenance_state()

    venv = (root / "shared/venv").stat()
    resolver = current / "tools/platform_verify_venv_reuse.py"
    result = _run([
        "/usr/bin/python3.12", "-I", "-B", str(resolver), "--resolve-shared-venv-origin",
        "--app", str(root), "--current", str(current), "--venv", str(root / "shared/venv"),
    ])
    if result.stderr or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}\n", result.stdout) is None:
        raise ReadinessError("shared-venv origin resolver failed")
    origin_slug = result.stdout.strip()
    origin_path = root / "releases" / origin_slug
    try:
        origin_metadata = origin_path.lstat()
        resolved_origin = origin_path.resolve(strict=True)
    except OSError as exc:
        raise ReadinessError("shared-venv origin release is unavailable") from exc
    if (
        SLUG_RE.fullmatch(origin_slug) is None
        or not stat.S_ISDIR(origin_metadata.st_mode)
        or stat.S_ISLNK(origin_metadata.st_mode)
        or resolved_origin.parent != (root / "releases").resolve(strict=True)
        or resolved_origin.name != origin_slug
    ):
        raise ReadinessError("shared-venv origin release path is invalid")
    origin_info = _read_json(origin_path / "RELEASE.json")
    if (
        origin_info.get("release_slug") != origin_slug
        or not isinstance(origin_info.get("source_git_commit"), str)
        or SHA_RE.fullmatch(origin_info["source_git_commit"]) is None
    ):
        raise ReadinessError("shared-venv origin identity is invalid")

    space = os.statvfs("/")
    _check_final_consistency(
        root=root,
        expected_app_sha=expected_app_sha,
        expected_previous_app_sha=expected_previous_app_sha,
        expected_release_manifest_sha=expected_release_manifest_sha,
        expected_previous_release_manifest_sha=expected_previous_release_manifest_sha,
        current_identity=(current, current_info),
        previous_identity=(previous, previous_info),
        services=services,
        lock_results=lock_results,
        timer=timer,
        maintenance_service=service,
    )
    return {
        "schema": 1,
        "status": "ready",
        "current": current_info,
        "previous": previous_info,
        "host_tools": {
            "source_sha": expected_host_tools_sha,
            "toolset_version": expected_toolset_version,
            "closure_files_verified": host_result["files_verified"],
            "manifest_sha256": host_result["manifest_sha256"],
            "normalized_modes_verified": host_result["normalized_modes_verified"],
            "previous_generation_directory_present": True,
            "expected_previous_generation_sha": expected_previous_host_tools_sha,
            "pin_closure_verified": len(pin["closure"]),
        },
        "venv_origin": origin_slug,
        "shared_venv": {"dev": venv.st_dev, "ino": venv.st_ino},
        "services": services,
        "compiled_web_compression_enabled": True,
        "health_http_status": health,
        "locks": lock_results,
        "operation_marker_absent": True,
        "diagnostic_plans_absent": True,
        "pending_systemd_jobs": 0,
        "maintenance_timer": timer,
        "maintenance_service": service,
        "final_consistency_recheck": "passed",
        "available_bytes": space.f_bavail * space.f_frsize,
    }


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-app-sha", required=True)
    parser.add_argument("--expected-previous-app-sha", required=True)
    parser.add_argument("--expected-release-manifest-sha", required=True)
    parser.add_argument("--expected-previous-release-manifest-sha", required=True)
    parser.add_argument("--expected-host-tools-sha", required=True)
    parser.add_argument("--expected-previous-host-tools-sha", required=True)
    parser.add_argument("--expected-toolset-version", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _arguments(argv)
        result = collect_readiness(
            expected_app_sha=args.expected_app_sha,
            expected_previous_app_sha=args.expected_previous_app_sha,
            expected_release_manifest_sha=args.expected_release_manifest_sha,
            expected_previous_release_manifest_sha=args.expected_previous_release_manifest_sha,
            expected_host_tools_sha=args.expected_host_tools_sha,
            expected_previous_host_tools_sha=args.expected_previous_host_tools_sha,
            expected_toolset_version=args.expected_toolset_version,
        )
    except (ReadinessError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"READINESS status=failed reason={type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
