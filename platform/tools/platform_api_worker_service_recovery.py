#!/usr/bin/env python3
"""Restore only API and worker services for the exact active release.

The service-recovery workflow embeds this file verbatim because its SSH job
must not check out or fetch repository code. Keep the embedding contract in
``test_platform_service_recovery_workflow``. This controller changes no
release pointers, systemd unit files, enablement, timers, environment files,
database schema, or backup state. Starting the normal services resumes their
ordinary application processing.

Worker readiness targets the local systemd worker's default ``celery@hostname``
node name so a different worker sharing the broker cannot satisfy the probe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import stat
import subprocess
import time
from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
SLUG_RE = re.compile(r"gha-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}-[0-9a-f]{12}\Z")
SERVICE_NAMES = ("deadlock-api", "deadlock-worker")
SERVICE_USERS = {"deadlock-api": "oldsparky-api", "deadlock-worker": "oldsparky-worker"}
PRESTATE = {
    "ActiveState": "inactive",
    "SubState": "dead",
    "Result": "success",
    "ExecMainStatus": "0",
    "MainPID": "0",
}
SHOW_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainStatus",
    "MainPID",
    "FragmentPath",
    "DropInPaths",
    "UnitFileState",
    "User",
    "Group",
)
MAX_CONFIG_BYTES = 1024 * 1024
MIN_FREE_BYTES = 5 * 1024**3
MAX_USED_PERCENT = 85.0
API_READY_URL = "http://127.0.0.1:8010/api/v1/health/ready"


class RecoveryRefusal(RuntimeError):
    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _owned_file(path: Path, *, mode: int, gid: int = 0, max_bytes: int = MAX_CONFIG_BYTES) -> bytes:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RecoveryRefusal("file_identity") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != gid
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != mode
        or resolved != path
        or metadata.st_size > max_bytes
    ):
        raise RecoveryRefusal("file_identity")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)

        def fingerprint(item: os.stat_result) -> tuple[int, ...]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_uid,
                item.st_gid,
                item.st_nlink,
                stat.S_IMODE(item.st_mode),
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )
        if fingerprint(opened) != fingerprint(metadata):
            raise RecoveryRefusal("file_identity")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
        if fingerprint(after) != fingerprint(opened):
            raise RecoveryRefusal("file_changed")
    except OSError as exc:
        raise RecoveryRefusal("file_read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > max_bytes:
        raise RecoveryRefusal("file_size")
    return payload


def _owned_directory(path: Path, *, mode: int, minimum_links: int = 2) -> os.stat_result:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RecoveryRefusal("directory_identity") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink < minimum_links
        or stat.S_IMODE(metadata.st_mode) != mode
        or resolved != path
    ):
        raise RecoveryRefusal("directory_identity")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY)
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise RecoveryRefusal("directory_identity") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
        raise RecoveryRefusal("directory_identity")
    return metadata


def _release_identity(release: Path, *, expected_slug: str | None = None) -> tuple[str, str, str]:
    _owned_directory(release, mode=0o755)
    slug = release.name
    if SLUG_RE.fullmatch(slug) is None:
        raise RecoveryRefusal("release_slug")
    raw = _owned_file(release / "RELEASE.json", mode=0o444, max_bytes=64 * 1024)
    try:
        metadata = json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RecoveryRefusal("release_metadata") from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("artifact_format_version") != 1
        or metadata.get("release_slug") != slug
        or not isinstance(metadata.get("source_git_commit"), str)
        or SHA_RE.fullmatch(metadata["source_git_commit"]) is None
        or not slug.endswith("-" + metadata["source_git_commit"][:12])
        or (expected_slug is not None and slug != expected_slug)
    ):
        raise RecoveryRefusal("release_metadata")
    return slug, metadata["source_git_commit"], hashlib.sha256(raw).hexdigest()


def _pointer_identity(app_dir: Path, name: str) -> tuple[str, str, tuple[int, int]]:
    pointer = app_dir / name
    try:
        link = pointer.lstat()
        target = os.readlink(pointer)
        resolved = pointer.resolve(strict=True)
    except OSError as exc:
        raise RecoveryRefusal("pointer_identity") from exc
    if not stat.S_ISLNK(link.st_mode) or target.startswith("/"):
        raise RecoveryRefusal("pointer_identity")
    expected_prefix = "releases/"
    if not target.startswith(expected_prefix) or target.count("/") != 1:
        raise RecoveryRefusal("pointer_identity")
    release = app_dir / target
    try:
        release_relative = release.relative_to(app_dir / "releases")
    except ValueError as exc:
        raise RecoveryRefusal("pointer_identity") from exc
    if len(release_relative.parts) != 1 or resolved != release:
        raise RecoveryRefusal("pointer_identity")
    _owned_directory(release, mode=0o755)
    return target, str(resolved), (link.st_dev, link.st_ino)


def _command(
    command: list[str], *, timeout: float = 10.0, cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=cwd,
            env=env or {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryRefusal("command_unavailable") from exc


def _systemctl(systemctl: str, *arguments: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return _command([systemctl, *arguments], timeout=timeout)


def _show_unit(systemctl: str, unit: str) -> dict[str, str]:
    result = _systemctl(
        systemctl,
        "show",
        unit,
        "--property=" + ",".join(SHOW_PROPERTIES),
        "--no-pager",
    )
    if result.returncode != 0:
        raise RecoveryRefusal("unit_state")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in SHOW_PROPERTIES or key in values:
            raise RecoveryRefusal("unit_state")
        values[key] = value
    if set(values) != set(SHOW_PROPERTIES):
        raise RecoveryRefusal("unit_state")
    return values


def _enabled_state(systemctl: str, unit: str) -> str:
    result = _systemctl(systemctl, "is-enabled", unit)
    value = result.stdout.strip()
    if (result.returncode, value) in {(0, "enabled"), (1, "disabled")}:
        return value
    raise RecoveryRefusal("unit_enablement")


def _assert_service_unit(
    *,
    release: Path,
    systemd_dir: Path,
    systemctl: str,
    service: str,
    before: bool,
    enabled_before: str | None = None,
) -> tuple[dict[str, str], str]:
    unit = service + ".service"
    source = release / "deploy" / "systemd" / unit
    installed = systemd_dir / unit
    source_bytes = _owned_file(source, mode=0o644, max_bytes=128 * 1024)
    installed_bytes = _owned_file(installed, mode=0o644, max_bytes=128 * 1024)
    if source_bytes != installed_bytes:
        raise RecoveryRefusal("unit_identity")
    state = _show_unit(systemctl, unit)
    if (
        state["LoadState"] != "loaded"
        or state["FragmentPath"] != str(installed)
        or state["DropInPaths"] != ""
        or state["User"] != SERVICE_USERS[service]
        or state["Group"] != SERVICE_USERS[service]
    ):
        raise RecoveryRefusal("unit_identity")
    enabled = _enabled_state(systemctl, unit)
    if state["UnitFileState"] != enabled:
        raise RecoveryRefusal("unit_enablement")
    if before:
        if any(state[key] != value for key, value in PRESTATE.items()):
            raise RecoveryRefusal("service_prestate")
    elif (
        state["ActiveState"] != "active"
        or state["SubState"] != "running"
        or state["Result"] != "success"
        or state["ExecMainStatus"] != "0"
        or not state["MainPID"].isdigit()
        or int(state["MainPID"]) <= 1
    ):
        raise RecoveryRefusal("service_health")
    if enabled_before is not None and enabled != enabled_before:
        raise RecoveryRefusal("unit_enablement_changed")
    return state, enabled


def _assert_active(systemctl: str, units: tuple[str, ...]) -> None:
    for unit in units:
        result = _systemctl(systemctl, "is-active", "--quiet", unit)
        if result.returncode != 0:
            raise RecoveryRefusal("backing_service")


def _assert_release_preflight(app_dir: Path, release: Path, expected_sha: str, expected_slug: str) -> None:
    helper = release / "tools" / "platform_release_preflight.sh"
    _owned_file(helper, mode=0o755, max_bytes=2 * 1024 * 1024)
    result = _command(["/bin/bash", str(helper), "--require-previous"], timeout=180.0)
    marker = next((line for line in result.stdout.splitlines() if line.startswith("RELEASE_PREFLIGHT ")), "")
    expected = f"RELEASE_PREFLIGHT schema=1 status=passed class=preflight release_slug={expected_slug} source_sha={expected_sha}"
    if result.returncode != 0 or marker != expected or result.stdout.splitlines()[-1:] != [expected]:
        raise RecoveryRefusal("release_preflight")


def _config_snapshot(app_dir: Path) -> dict[str, str]:
    shared = app_dir / "shared"
    _owned_directory(shared, mode=0o755)
    source_env = _owned_file(shared / ".env.platform", mode=0o600)
    env_dir = shared / "env"
    _owned_directory(env_dir, mode=0o711)
    rendered: dict[str, bytes] = {}
    for service in SERVICE_NAMES:
        user = SERVICE_USERS[service]
        try:
            gid = pwd.getpwnam(user).pw_gid
        except KeyError as exc:
            raise RecoveryRefusal("service_identity") from exc
        rendered[service] = _owned_file(env_dir / f"{service.removeprefix('deadlock-')}.env", mode=0o640, gid=gid)
    result = {"source_env": hashlib.sha256(source_env).hexdigest()}
    result.update(
        {
            f"{service}_env": hashlib.sha256(payload).hexdigest()
            for service, payload in rendered.items()
        }
    )
    return result


def _assert_disk(app_dir: Path) -> None:
    try:
        usage = shutil.disk_usage(app_dir)
    except OSError as exc:
        raise RecoveryRefusal("disk_state") from exc
    if usage.total <= 0 or usage.free < MIN_FREE_BYTES:
        raise RecoveryRefusal("disk_state")
    used_percent = (usage.used / usage.total) * 100
    if used_percent > MAX_USED_PERCENT:
        raise RecoveryRefusal("disk_state")


def _api_ready() -> bool:
    request = Request(API_READY_URL, headers={"Accept": "application/json", "User-Agent": "oldsparky-service-recovery/1"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
            if response.status != 200:
                return False
            body = response.read(4097)
    except (OSError, TimeoutError, URLError, ValueError):
        return False
    if len(body) > 4096:
        return False
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("service") == "deadlock-platform-api"
    )


def _wait_api_ready(*, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _api_ready():
            return True
        time.sleep(0.5)
    return False


def _worker_ready(app_dir: Path, release: Path, systemd_dir: Path) -> bool:
    shared = app_dir / "shared"
    code = (
        "import os; from apps.platform_worker.worker import celery_app; "
        "expected=os.environ['PLATFORM_EXPECTED_CELERY_NODE']; "
        "replies=celery_app.control.ping(timeout=5.0, limit=1, destination=[expected]); "
        "ok=replies==[{expected:'pong'}]; "
        "print('WORKER_PING status=passed' if ok else 'WORKER_PING status=failed'); "
        "raise SystemExit(0 if ok else 1)"
    )
    command = [
        "/usr/sbin/runuser", "-u", SERVICE_USERS["deadlock-worker"], "--",
        "/usr/bin/env",
        "PLATFORM_RUNTIME_SERVICE=worker",
        f"PLATFORM_ROOT_DIR={release}",
        f"PLATFORM_APP_DIR={app_dir}",
        f"PLATFORM_SHARED_DIR={shared}",
        f"PLATFORM_ENV_FILE={shared / 'env/worker.env'}",
        f"PLATFORM_PYTHON_BIN={shared / 'venv/bin/python'}",
        "PLATFORM_WORKER_CONCURRENCY=2",
        f"PLATFORM_EXPECTED_CELERY_NODE=celery@{socket.gethostname()}",
        f"PYTHONPATH={release}",
        str(shared / "venv/bin/python"), "-c", code,
    ]
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            _assert_service_unit(
                release=release,
                systemd_dir=systemd_dir,
                systemctl="/usr/bin/systemctl",
                service="deadlock-worker",
                before=False,
            )
        except RecoveryRefusal:
            return False
        remaining = deadline - time.monotonic()
        try:
            result = _command(
                command,
                timeout=min(7.0, remaining),
                cwd=release,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
        except RecoveryRefusal as exc:
            if exc.stage != "command_unavailable":
                return False
            result = subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        if result.returncode == 0 and result.stdout.splitlines() == ["WORKER_PING status=passed"]:
            return True
        if time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return False


def _receipt(
    *,
    status: str,
    stage: str,
    source_sha: str,
    release_slug: str,
    service_before: dict[str, dict[str, str]],
    enabled_before: dict[str, str],
    service_after: dict[str, dict[str, str]],
    enabled_after: dict[str, str],
    rollback: str,
) -> None:
    values = [
        "API_WORKER_RECOVERY",
        "schema=1",
        f"status={status}",
        f"stage={stage}",
        f"source_sha={source_sha or 'unavailable'}",
        f"release_slug={release_slug or 'unavailable'}",
        f"api_before={service_before.get('deadlock-api', {}).get('ActiveState', 'unknown')}",
        f"api_enabled_before={enabled_before.get('deadlock-api', 'unknown')}",
        f"api_after={service_after.get('deadlock-api', {}).get('ActiveState', 'unknown')}",
        f"worker_before={service_before.get('deadlock-worker', {}).get('ActiveState', 'unknown')}",
        f"worker_enabled_before={enabled_before.get('deadlock-worker', 'unknown')}",
        f"worker_after={service_after.get('deadlock-worker', {}).get('ActiveState', 'unknown')}",
        "worker_readiness=celery_control_ping",
        "post_release_qa=required",
        f"rollback={rollback}",
    ]
    print(" ".join(values))


def recover(app_dir: Path, expected_sha: str, expected_slug: str, systemd_dir: Path) -> int:
    source_sha = ""
    release_slug = ""
    service_before: dict[str, dict[str, str]] = {}
    enabled_before: dict[str, str] = {}
    service_after: dict[str, dict[str, str]] = {}
    enabled_after: dict[str, str] = {}
    attempted: list[str] = []
    stage = "input"
    rollback = "not_needed"
    config_before: dict[str, str] = {}
    pointers_before: dict[str, tuple[str, str, tuple[int, int]]] = {}
    release_before: tuple[str, str, str] | None = None
    unit_digests: dict[str, str] = {}
    try:
        if SHA_RE.fullmatch(expected_sha) is None or SLUG_RE.fullmatch(expected_slug) is None:
            raise RecoveryRefusal("input")
        if not expected_slug.endswith("-" + expected_sha[:12]):
            raise RecoveryRefusal("input")
        if os.geteuid() != 0:
            raise RecoveryRefusal("privilege")
        _owned_directory(app_dir, mode=0o755)
        releases = app_dir / "releases"
        _owned_directory(releases, mode=0o755)
        for marker in (".release-operation.json", ".release-systemd-state.json", ".release-rollback.json"):
            if (app_dir / "shared" / marker).exists() or (app_dir / "shared" / marker).is_symlink():
                raise RecoveryRefusal("pending_release_state")
        pointers_before["current"] = _pointer_identity(app_dir, "current")
        pointers_before["previous"] = _pointer_identity(app_dir, "previous")
        release = Path(pointers_before["current"][1])
        release_slug, source_sha, release_digest = _release_identity(release, expected_slug=expected_slug)
        if source_sha != expected_sha:
            raise RecoveryRefusal("release_sha")
        _release_identity(Path(pointers_before["previous"][1]))
        release_before = (release_slug, source_sha, release_digest)
        _assert_release_preflight(app_dir, release, expected_sha, expected_slug)
        config_before = _config_snapshot(app_dir)
        _assert_disk(app_dir)
        _assert_active("/usr/bin/systemctl", ("postgresql.service", "redis-server.service", "deadlock-web.service", "nginx.service"))

        for service in SERVICE_NAMES:
            unit = service + ".service"
            source_unit = release / "deploy" / "systemd" / unit
            unit_bytes = _owned_file(source_unit, mode=0o644, max_bytes=128 * 1024)
            unit_digests[service] = hashlib.sha256(unit_bytes).hexdigest()
            service_before[service], enabled_before[service] = _assert_service_unit(
                release=release,
                systemd_dir=systemd_dir,
                systemctl="/usr/bin/systemctl",
                service=service,
                before=True,
            )

        for service in SERVICE_NAMES:
            stage = "start_api" if service == "deadlock-api" else "start_worker"
            attempted.append(service)
            result = _systemctl("/usr/bin/systemctl", "start", service + ".service", timeout=35.0)
            if result.returncode != 0:
                raise RecoveryRefusal(stage)
            if service == "deadlock-api":
                stage = "api_readiness"
                if not _wait_api_ready():
                    raise RecoveryRefusal(stage)

        stage = "worker_readiness"
        if not _worker_ready(app_dir, release, systemd_dir):
            raise RecoveryRefusal(stage)

        stage = "service_health"
        for service in SERVICE_NAMES:
            service_after[service], enabled_after[service] = _assert_service_unit(
                release=release,
                systemd_dir=systemd_dir,
                systemctl="/usr/bin/systemctl",
                service=service,
                before=False,
                enabled_before=enabled_before[service],
            )
        _assert_active("/usr/bin/systemctl", ("deadlock-web.service", "nginx.service", "postgresql.service", "redis-server.service"))
        stage = "state_stability"
        if _config_snapshot(app_dir) != config_before:
            raise RecoveryRefusal(stage)
        if {name: _pointer_identity(app_dir, name) for name in ("current", "previous")} != pointers_before:
            raise RecoveryRefusal(stage)
        if _release_identity(release, expected_slug=expected_slug) != release_before:
            raise RecoveryRefusal(stage)
        for service in SERVICE_NAMES:
            current_unit = _owned_file(release / "deploy" / "systemd" / (service + ".service"), mode=0o644, max_bytes=128 * 1024)
            installed_unit = _owned_file(systemd_dir / (service + ".service"), mode=0o644, max_bytes=128 * 1024)
            if hashlib.sha256(current_unit).hexdigest() != unit_digests[service] or current_unit != installed_unit:
                raise RecoveryRefusal(stage)
    except Exception as exc:
        stage = exc.stage if isinstance(exc, RecoveryRefusal) else "internal"
        if attempted:
            rollback = "verified"
            for service in reversed(attempted):
                stop_result = _systemctl("/usr/bin/systemctl", "stop", service + ".service", timeout=35.0)
                if stop_result.returncode != 0:
                    rollback = "unverified"
                clear_result = _systemctl("/usr/bin/systemctl", "reset-failed", service + ".service", timeout=10.0)
                if clear_result.returncode != 0:
                    rollback = "unverified"
            if rollback == "verified":
                try:
                    for service in SERVICE_NAMES:
                        service_after[service], enabled_after[service] = _assert_service_unit(
                            release=release,
                            systemd_dir=systemd_dir,
                            systemctl="/usr/bin/systemctl",
                            service=service,
                            before=True,
                            enabled_before=enabled_before.get(service),
                        )
                    if _config_snapshot(app_dir) != config_before:
                        rollback = "unverified"
                    if {name: _pointer_identity(app_dir, name) for name in ("current", "previous")} != pointers_before:
                        rollback = "unverified"
                except RecoveryRefusal:
                    rollback = "unverified"
        _receipt(
            status="failed",
            stage=stage,
            source_sha=source_sha,
            release_slug=release_slug,
            service_before=service_before,
            enabled_before=enabled_before,
            service_after=service_after,
            enabled_after=enabled_after,
            rollback=rollback,
        )
        return 1

    _receipt(
        status="passed",
        stage="complete",
        source_sha=source_sha,
        release_slug=release_slug,
        service_before=service_before,
        enabled_before=enabled_before,
        service_after=service_after,
        enabled_after=enabled_after,
        rollback="not_needed",
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--expected-release-slug", required=True)
    parser.add_argument("--systemd-dir", default="/etc/systemd/system")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return recover(
        Path(args.app_dir),
        args.expected_source_sha,
        args.expected_release_slug,
        Path(args.systemd_dir),
    )


if __name__ == "__main__":
    raise SystemExit(main())
