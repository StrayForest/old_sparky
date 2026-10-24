#!/usr/bin/env python3
"""Restore API and worker services for the exact active release.

The service-recovery workflow embeds this file verbatim because its SSH job
must not check out or fetch repository code. Keep the embedding contract in
``test_platform_service_recovery_workflow``. This controller changes no
release pointers, systemd unit files, enablement, timers, environment files,
database schema, or backup state. Starting the normal services resumes their
ordinary application processing.

Worker readiness targets the local systemd worker's default ``celery@hostname``
node name so a different worker sharing the broker cannot satisfy the probe.
The explicit ``api-worker-nginx`` mode additionally supports an nginx unit that
is already healthy or inactive with a successful prior result; it validates the
effective configuration before starting it and checks the local TLS proxy route.
The loopback TLS probe pins the peer certificate to the root-owned origin
certificate configured for the active nginx site.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import ssl
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
TIMER_NAMES = (
    "deadlock-maintenance.timer",
    "deadlock-logrotate.timer",
    "deadlock-cloudflare-ips.timer",
    "deadlock-health-monitor.timer",
)
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
TIMER_SHOW_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "FragmentPath",
    "DropInPaths",
    "UnitFileState",
)
MAX_CONFIG_BYTES = 1024 * 1024
MIN_FREE_BYTES = 5 * 1024**3
MIN_FREE_PERCENT = 10
MAX_USED_PERCENT = 90.0
# The stopped-worker read-only probe took up to 8.27s, including a 5.10s
# Celery ping timeout; keep this bounded child cap above that measured cold path.
WORKER_PROBE_TIMEOUT = 10.0
API_READY_URL = "http://127.0.0.1:8010/api/v1/health/ready"
NGINX_UNIT_PATH = Path("/usr/lib/systemd/system/nginx.service")
NGINX_READY_HOST = "old-sparky.com"
NGINX_CERT_PATH = Path("/opt/oldsparky/platform/shared/tls/old-sparky.com-origin.pem")
NGINX_KEY_PATH = Path("/opt/oldsparky/platform/shared/tls/old-sparky.com-origin.key")
NGINX_CONFIG_MAX_BYTES = 1024 * 1024
NGINX_CERT_MAX_BYTES = 64 * 1024
NGINX_CERT_MIN_REMAINING_SECONDS = 30 * 86_400
WORKER_READINESS_BOOTSTRAP = """\
set -euo pipefail
TOOLS_DIR="$PLATFORM_ROOT_DIR/tools"
source "$TOOLS_DIR/platform_runtime_common.sh"
platform_require_isolated_service_env worker
platform_load_env_file
platform_require_python
cd "$PLATFORM_ROOT_DIR"
export PYTHONPATH="$PLATFORM_ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PLATFORM_EXPECTED_CELERY_NODE="$2"
exec "$PLATFORM_PYTHON_BIN" -c "$1"
"""
WORKER_READINESS_CODE = """\
import os
try:
    from apps.platform_worker.worker import celery_app
    expected = os.environ["PLATFORM_EXPECTED_CELERY_NODE"]
except Exception:
    print("WORKER_PING status=failed reason=exception")
    raise SystemExit(1)
try:
    replies = celery_app.control.ping(timeout=5.0, limit=1, destination=[expected])
except Exception:
    print("WORKER_PING status=failed reason=exception")
    raise SystemExit(1)
# Celery 5.6.3 wraps the worker's pong with its `ok()` result envelope.
if replies == [{expected: {"ok": "pong"}}]:
    print("WORKER_PING status=passed")
    raise SystemExit(0)
reason = "no_reply" if replies == [] else "unexpected_reply"
print(f"WORKER_PING status=failed reason={reason}")
raise SystemExit(1)
"""


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
    if (
        not stat.S_ISLNK(link.st_mode)
        or link.st_uid != 0
        or link.st_gid != 0
        or link.st_nlink != 1
    ):
        raise RecoveryRefusal("pointer_identity")
    releases = app_dir / "releases"
    if target.startswith("/"):
        release = Path(target)
        if release.parent != releases or str(release) != target:
            raise RecoveryRefusal("pointer_identity")
        release_name = release.name
    else:
        if not target.startswith("releases/") or target.count("/") != 1:
            raise RecoveryRefusal("pointer_identity")
        release_name = target.removeprefix("releases/")
        if target != f"releases/{release_name}":
            raise RecoveryRefusal("pointer_identity")
        release = releases / release_name
    if not release_name or release_name in {".", ".."}:
        raise RecoveryRefusal("pointer_identity")
    try:
        release_relative = release.relative_to(releases)
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
    except subprocess.TimeoutExpired as exc:
        raise RecoveryRefusal("command_timeout") from exc
    except OSError as exc:
        raise RecoveryRefusal("command_unavailable") from exc


def _systemctl(systemctl: str, *arguments: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return _command([systemctl, *arguments], timeout=timeout)


def _show_unit(
    systemctl: str,
    unit: str,
    *,
    properties: tuple[str, ...] = SHOW_PROPERTIES,
) -> dict[str, str]:
    result = _systemctl(
        systemctl,
        "show",
        unit,
        "--property=" + ",".join(properties),
        "--no-pager",
    )
    if result.returncode != 0:
        raise RecoveryRefusal("unit_state")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in properties or key in values:
            raise RecoveryRefusal("unit_state")
        values[key] = value
    if set(values) != set(properties):
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


def _assert_nginx_unit(
    systemctl: str,
    *,
    expected_active: str,
    enabled_before: str | None = None,
) -> tuple[dict[str, str], str, str]:
    if expected_active not in {"active", "inactive", "either"}:
        raise RecoveryRefusal("nginx_prestate")
    unit_bytes = _owned_file(NGINX_UNIT_PATH, mode=0o644, max_bytes=128 * 1024)
    state = _show_unit(systemctl, "nginx.service")
    if (
        state["LoadState"] != "loaded"
        or state["FragmentPath"] != str(NGINX_UNIT_PATH)
        or state["DropInPaths"] != ""
        or state["User"] != ""
        or state["Group"] != ""
    ):
        raise RecoveryRefusal("nginx_unit_identity")
    enabled = _enabled_state(systemctl, "nginx.service")
    if enabled != "enabled" or state["UnitFileState"] != enabled:
        raise RecoveryRefusal("nginx_unit_enablement")
    active = state["ActiveState"]
    if expected_active == "either":
        if active == "active":
            healthy = (
                state["SubState"] == "running"
                and state["Result"] == "success"
                and state["ExecMainStatus"] == "0"
                and state["MainPID"].isdigit()
                and int(state["MainPID"]) > 1
            )
        else:
            healthy = (
                active == "inactive"
                and state["SubState"] == "dead"
                and state["Result"] == "success"
                and state["ExecMainStatus"] == "0"
                and state["MainPID"] == "0"
            )
    elif expected_active == "active":
        healthy = (
            active == "active"
            and state["SubState"] == "running"
            and state["Result"] == "success"
            and state["ExecMainStatus"] == "0"
            and state["MainPID"].isdigit()
            and int(state["MainPID"]) > 1
        )
    else:
        healthy = (
            active == "inactive"
            and state["SubState"] == "dead"
            and state["Result"] == "success"
            and state["ExecMainStatus"] == "0"
            and state["MainPID"] == "0"
        )
    if not healthy:
        raise RecoveryRefusal("nginx_prestate" if expected_active == "either" else "nginx_health")
    if enabled_before is not None and enabled != enabled_before:
        raise RecoveryRefusal("nginx_enablement_changed")
    return state, enabled, hashlib.sha256(unit_bytes).hexdigest()


def _nginx_configuration_identity() -> str:
    tested = _command(["/usr/sbin/nginx", "-t", "-q"], timeout=15.0)
    if tested.returncode != 0:
        raise RecoveryRefusal("nginx_config_test")
    dumped = _command(["/usr/sbin/nginx", "-T"], timeout=15.0)
    try:
        payload = dumped.stdout.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise RecoveryRefusal("nginx_config_identity") from exc
    if dumped.returncode != 0 or not payload or len(payload) > NGINX_CONFIG_MAX_BYTES:
        raise RecoveryRefusal("nginx_config_identity")
    certificate_der = _nginx_certificate_der()
    key_identity = _owned_file_identity(NGINX_KEY_PATH, mode=0o600, max_bytes=64 * 1024)
    for arguments in (
        ("x509", "-in", str(NGINX_CERT_PATH), "-noout", "-checkhost", NGINX_READY_HOST),
        (
            "x509", "-in", str(NGINX_CERT_PATH), "-noout", "-checkend",
            str(NGINX_CERT_MIN_REMAINING_SECONDS),
        ),
    ):
        checked = _command(["/usr/bin/openssl", *arguments], timeout=10.0)
        if checked.returncode != 0:
            raise RecoveryRefusal("nginx_certificate_validity")
    key_fingerprint = ":".join(str(value) for value in key_identity)
    return hashlib.sha256(payload + b"\x00" + certificate_der + b"\x00" + key_fingerprint.encode("ascii")).hexdigest()


def _owned_file_identity(path: Path, *, mode: int, max_bytes: int) -> tuple[int, ...]:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RecoveryRefusal("file_identity") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
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
    except OSError as exc:
        raise RecoveryRefusal("file_identity") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    def identity(item: os.stat_result) -> tuple[int, ...]:
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
    opened_identity = identity(opened)
    if opened_identity != identity(metadata):
        raise RecoveryRefusal("file_changed")
    return opened_identity


def _nginx_certificate_der() -> bytes:
    try:
        pem = _owned_file(NGINX_CERT_PATH, mode=0o644, max_bytes=NGINX_CERT_MAX_BYTES).decode("ascii")
        return ssl.PEM_cert_to_DER_cert(pem)
    except (RecoveryRefusal, UnicodeError, ValueError) as exc:
        raise RecoveryRefusal("nginx_certificate_identity") from exc


def _proxy_api_ready() -> bool:
    request = (
        b"GET /api/v1/health/ready HTTP/1.1\r\n"
        b"Host: old-sparky.com\r\n"
        b"Accept: application/json\r\n"
        b"Connection: close\r\n\r\n"
    )
    connection = None
    try:
        connection = socket.create_connection(("127.0.0.1", 443), timeout=2.0)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        tls = context.wrap_socket(
            connection,
            server_hostname=NGINX_READY_HOST,
        )
        connection = tls
        tls.settimeout(2.0)
        if tls.getpeercert(binary_form=True) != _nginx_certificate_der():
            return False
        tls.sendall(request)
        response = http.client.HTTPResponse(tls)
        response.begin()
        if response.status != 200:
            return False
        body = response.read(4097)
    except (OSError, TimeoutError, ValueError, http.client.HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()
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


def _wait_proxy_api_ready(*, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _proxy_api_ready():
            return True
        time.sleep(0.5)
    return False


def _assert_release_preflight(app_dir: Path, release: Path, expected_sha: str, expected_slug: str) -> None:
    helper = release / "tools" / "platform_release_preflight.sh"
    _owned_file(helper, mode=0o755, max_bytes=2 * 1024 * 1024)
    result = _command(["/bin/bash", str(helper), "--require-previous"], timeout=180.0)
    marker = next((line for line in result.stdout.splitlines() if line.startswith("RELEASE_PREFLIGHT ")), "")
    expected = f"RELEASE_PREFLIGHT schema=1 status=passed class=preflight release_slug={expected_slug} source_sha={expected_sha}"
    if result.returncode != 0 or marker != expected or result.stdout.splitlines()[-1:] != [expected]:
        raise RecoveryRefusal("release_preflight")


def _timer_snapshot(release: Path, systemd_dir: Path, systemctl: str) -> dict[str, tuple[str, ...]]:
    snapshot: dict[str, tuple[str, ...]] = {}
    for unit in TIMER_NAMES:
        source = release / "deploy" / "systemd" / unit
        installed = systemd_dir / unit
        source_bytes = _owned_file(source, mode=0o644, max_bytes=128 * 1024)
        installed_bytes = _owned_file(installed, mode=0o644, max_bytes=128 * 1024)
        if source_bytes != installed_bytes:
            raise RecoveryRefusal("timer_unit_identity")
        state = _show_unit(systemctl, unit, properties=TIMER_SHOW_PROPERTIES)
        enabled = _enabled_state(systemctl, unit)
        if (
            state["LoadState"] != "loaded"
            or state["FragmentPath"] != str(installed)
            or state["DropInPaths"] != ""
            or enabled not in {"enabled", "disabled"}
            or state["UnitFileState"] != enabled
        ):
            raise RecoveryRefusal("timer_unit_identity")
        if state["ActiveState"] == "active":
            valid_state = (
                state["SubState"] == "waiting"
                and state["Result"] == "success"
            )
        else:
            valid_state = (
                state["ActiveState"] == "inactive"
                and state["SubState"] == "dead"
                and state["Result"] == "success"
            )
        if not valid_state:
            raise RecoveryRefusal("timer_state")
        snapshot[unit] = (
            hashlib.sha256(source_bytes).hexdigest(),
            enabled,
            *(state[key] for key in TIMER_SHOW_PROPERTIES),
        )
    return snapshot


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
    required_free_bytes = max(MIN_FREE_BYTES, (usage.total * MIN_FREE_PERCENT + 99) // 100)
    if usage.free < required_free_bytes:
        raise RecoveryRefusal("disk_state")
    # Use the same available-free accounting as the 10% floor. `usage.used`
    # excludes blocks reserved from non-root users and can disagree with it.
    used_percent = ((usage.total - usage.free) / usage.total) * 100
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


def _worker_ready(app_dir: Path, release: Path, systemd_dir: Path) -> str:
    shared = app_dir / "shared"
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
        "/bin/bash", "-c", WORKER_READINESS_BOOTSTRAP,
        "worker-readiness", WORKER_READINESS_CODE, f"celery@{socket.gethostname()}",
    ]
    deadline = time.monotonic() + 30.0
    last_reason = "timeout"
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
            return "unit_changed"
        remaining = deadline - time.monotonic()
        command_completed = True
        try:
            result = _command(
                command,
                timeout=min(WORKER_PROBE_TIMEOUT, remaining),
                cwd=release,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
        except RecoveryRefusal as exc:
            if exc.stage == "command_timeout":
                last_reason = "timeout"
            elif exc.stage == "command_unavailable":
                last_reason = "command_unavailable"
            else:
                return "invalid_output"
            command_completed = False
            result = subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        else:
            last_reason = "invalid_output"
        output = result.stdout.splitlines()
        if command_completed and result.returncode != 0 and not output:
            return "bootstrap_failure"
        if result.returncode == 0 and output == ["WORKER_PING status=passed"]:
            return "passed"
        for reason in ("exception", "no_reply", "unexpected_reply"):
            if output == [f"WORKER_PING status=failed reason={reason}"]:
                last_reason = reason
                break
        if time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return last_reason


def _receipt(
    *,
    mode: str,
    status: str,
    stage: str,
    requested_source_sha: str,
    requested_release_slug: str,
    source_sha: str,
    release_slug: str,
    service_before: dict[str, dict[str, str]],
    enabled_before: dict[str, str],
    service_after: dict[str, dict[str, str]],
    enabled_after: dict[str, str],
    nginx_before: dict[str, str],
    nginx_enabled_before: str,
    nginx_after: dict[str, str],
    proxy_readiness: str,
    timer_status: str,
    rollback: str,
) -> None:
    values = [
        "API_WORKER_RECOVERY",
        "schema=1",
        f"mode={mode}",
        f"status={status}",
        f"stage={stage}",
        f"requested_source_sha={requested_source_sha}",
        f"requested_release_slug={requested_release_slug}",
        f"source_sha={source_sha or 'unavailable'}",
        f"release_slug={release_slug or 'unavailable'}",
        f"api_before={service_before.get('deadlock-api', {}).get('ActiveState', 'unknown')}",
        f"api_enabled_before={enabled_before.get('deadlock-api', 'unknown')}",
        f"api_after={service_after.get('deadlock-api', {}).get('ActiveState', 'unknown')}",
        f"worker_before={service_before.get('deadlock-worker', {}).get('ActiveState', 'unknown')}",
        f"worker_enabled_before={enabled_before.get('deadlock-worker', 'unknown')}",
        f"worker_after={service_after.get('deadlock-worker', {}).get('ActiveState', 'unknown')}",
        "worker_readiness=celery_control_ping",
        f"nginx_before={nginx_before.get('ActiveState', 'unknown')}",
        f"nginx_enabled_before={nginx_enabled_before or 'unknown'}",
        f"nginx_after={nginx_after.get('ActiveState', 'unknown')}",
        f"proxy_readiness={proxy_readiness}",
        f"timers={timer_status}",
        "post_release_qa=required",
        f"rollback={rollback}",
    ]
    print(" ".join(values))


def recover(
    app_dir: Path,
    expected_sha: str,
    expected_slug: str,
    systemd_dir: Path,
    *,
    mode: str = "api-worker",
) -> int:
    source_sha = ""
    release_slug = ""
    service_before: dict[str, dict[str, str]] = {}
    enabled_before: dict[str, str] = {}
    service_after: dict[str, dict[str, str]] = {}
    enabled_after: dict[str, str] = {}
    nginx_before: dict[str, str] = {}
    nginx_enabled_before = ""
    nginx_after: dict[str, str] = {}
    nginx_unit_digest = ""
    nginx_config_before = ""
    proxy_readiness = "not_requested"
    timer_snapshot: dict[str, tuple[str, ...]] = {}
    timer_status = "not_checked"
    attempted: list[str] = []
    stage = "input"
    rollback = "not_needed"
    config_before: dict[str, str] = {}
    pointers_before: dict[str, tuple[str, str, tuple[int, int]]] = {}
    release_before: tuple[str, str, str] | None = None
    unit_digests: dict[str, str] = {}
    try:
        if mode not in {"api-worker", "api-worker-nginx"}:
            raise RecoveryRefusal("input")
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
        timer_snapshot = _timer_snapshot(release, systemd_dir, "/usr/bin/systemctl")
        timer_status = "captured"
        config_before = _config_snapshot(app_dir)
        _assert_disk(app_dir)
        _assert_active("/usr/bin/systemctl", ("postgresql.service", "redis-server.service", "deadlock-web.service"))
        if mode == "api-worker":
            _assert_active("/usr/bin/systemctl", ("nginx.service",))
        else:
            nginx_before, nginx_enabled_before, nginx_unit_digest = _assert_nginx_unit(
                "/usr/bin/systemctl",
                expected_active="either",
            )
            nginx_config_before = _nginx_configuration_identity()

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
            result = _systemctl("/usr/bin/systemctl", "start", service + ".service", timeout=35.0)
            if result.returncode != 0:
                if _show_unit("/usr/bin/systemctl", service).get("ActiveState") == "active":
                    attempted.append(service)
                raise RecoveryRefusal(stage)
            attempted.append(service)
            if service == "deadlock-api":
                stage = "api_readiness"
                if not _wait_api_ready():
                    raise RecoveryRefusal(stage)

        stage = "worker_readiness"
        worker_result = _worker_ready(app_dir, release, systemd_dir)
        if worker_result != "passed":
            raise RecoveryRefusal(f"{stage}_{worker_result}")

        if mode == "api-worker-nginx":
            stage = "nginx_prestart_config"
            if _nginx_configuration_identity() != nginx_config_before:
                raise RecoveryRefusal("nginx_config_changed")
            if nginx_before["ActiveState"] == "inactive":
                stage = "start_nginx"
                result = _systemctl("/usr/bin/systemctl", "start", "nginx.service", timeout=35.0)
                if result.returncode != 0:
                    if _show_unit("/usr/bin/systemctl", "nginx").get("ActiveState") == "active":
                        attempted.append("nginx")
                    raise RecoveryRefusal(stage)
                attempted.append("nginx")
            stage = "nginx_readiness"
            if not _wait_proxy_api_ready():
                raise RecoveryRefusal(stage)
            proxy_readiness = "loopback_pinned_tls_api_health"
            nginx_after, _nginx_enabled_after, after_unit_digest = _assert_nginx_unit(
                "/usr/bin/systemctl",
                expected_active="active",
                enabled_before=nginx_enabled_before,
            )
            if after_unit_digest != nginx_unit_digest:
                raise RecoveryRefusal("nginx_unit_changed")

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
        if _timer_snapshot(release, systemd_dir, "/usr/bin/systemctl") != timer_snapshot:
            raise RecoveryRefusal("timer_state_changed")
        timer_status = "preserved"
        for service in SERVICE_NAMES:
            current_unit = _owned_file(release / "deploy" / "systemd" / (service + ".service"), mode=0o644, max_bytes=128 * 1024)
            installed_unit = _owned_file(systemd_dir / (service + ".service"), mode=0o644, max_bytes=128 * 1024)
            if hashlib.sha256(current_unit).hexdigest() != unit_digests[service] or current_unit != installed_unit:
                raise RecoveryRefusal(stage)
        if mode == "api-worker-nginx":
            nginx_after, _nginx_enabled_after, after_unit_digest = _assert_nginx_unit(
                "/usr/bin/systemctl",
                expected_active="active",
                enabled_before=nginx_enabled_before,
            )
            if after_unit_digest != nginx_unit_digest:
                raise RecoveryRefusal(stage)
            if _nginx_configuration_identity() != nginx_config_before:
                raise RecoveryRefusal(stage)
    except Exception as exc:
        stage = exc.stage if isinstance(exc, RecoveryRefusal) else "internal"
        if attempted:
            rollback = "verified"
            for service in reversed(attempted):
                unit = "nginx.service" if service == "nginx" else service + ".service"
                stop_result = _systemctl("/usr/bin/systemctl", "stop", unit, timeout=35.0)
                if stop_result.returncode != 0:
                    rollback = "unverified"
                clear_result = _systemctl("/usr/bin/systemctl", "reset-failed", unit, timeout=10.0)
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
                    if mode == "api-worker-nginx":
                        nginx_after, _enabled, after_unit_digest = _assert_nginx_unit(
                            "/usr/bin/systemctl",
                            expected_active=nginx_before["ActiveState"],
                            enabled_before=nginx_enabled_before,
                        )
                        if after_unit_digest != nginx_unit_digest or _nginx_configuration_identity() != nginx_config_before:
                            rollback = "unverified"
                except RecoveryRefusal:
                    rollback = "unverified"
        if timer_snapshot and timer_status != "preserved":
            try:
                timer_status = (
                    "preserved"
                    if _timer_snapshot(release, systemd_dir, "/usr/bin/systemctl") == timer_snapshot
                    else "unverified"
                )
            except RecoveryRefusal:
                timer_status = "unverified"
        _receipt(
            mode=mode,
            status="failed",
            stage=stage,
            requested_source_sha=expected_sha,
            requested_release_slug=expected_slug,
            source_sha=source_sha,
            release_slug=release_slug,
            service_before=service_before,
            enabled_before=enabled_before,
            service_after=service_after,
            enabled_after=enabled_after,
            nginx_before=nginx_before,
            nginx_enabled_before=nginx_enabled_before,
            nginx_after=nginx_after,
            proxy_readiness=proxy_readiness,
            timer_status=timer_status,
            rollback=rollback,
        )
        return 1

    _receipt(
        mode=mode,
        status="passed",
        stage="complete",
        requested_source_sha=expected_sha,
        requested_release_slug=expected_slug,
        source_sha=source_sha,
        release_slug=release_slug,
        service_before=service_before,
        enabled_before=enabled_before,
        service_after=service_after,
        enabled_after=enabled_after,
        nginx_before=nginx_before,
        nginx_enabled_before=nginx_enabled_before,
        nginx_after=nginx_after,
        proxy_readiness=proxy_readiness,
        timer_status=timer_status,
        rollback="not_needed",
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--expected-release-slug", required=True)
    parser.add_argument("--systemd-dir", default="/etc/systemd/system")
    parser.add_argument("--mode", choices=("api-worker", "api-worker-nginx"), default="api-worker")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return recover(
        Path(args.app_dir),
        args.expected_source_sha,
        args.expected_release_slug,
        Path(args.systemd_dir),
        mode=args.mode,
    )


if __name__ == "__main__":
    raise SystemExit(main())
