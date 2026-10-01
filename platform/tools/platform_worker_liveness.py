#!/usr/bin/env python3
"""Run one bounded Celery roundtrip against the deployed worker.

This helper is intentionally a release-smoke tool, not a health monitor.  The
release state machine invokes it through the worker service account and the
generated worker environment.  It imports the application worker only after
the account, environment, active pointer and release metadata have been
validated, so a stale or mixed release cannot publish a task.

The command emits one fixed, redacted JSON object.  It never includes broker
URLs, task IDs, task values, exception text or Redis key names.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
import importlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.parse import urlsplit


MAX_SECONDS = 15.0
CHILD_WORK_SECONDS = 11.0
CHILD_STOP_GRACE_SECONDS = 0.75
CHILD_KILL_GRACE_SECONDS = 0.75
CLEANUP_STOP_GRACE_SECONDS = 0.20
CLEANUP_KILL_GRACE_SECONDS = 0.20
DEFAULT_EXPIRES_SECONDS = 10.0
WORKER_USER = "oldsparky-worker"
WORKER_RUNTIME_SERVICE = "worker"
DEFAULT_QUEUE = "deadlock-platform-default"
PING_TASK_NAME = "platform.ping"
WORKER_ENV_NAME = "worker.env"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
RELEASE_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
TASK_ID_PATTERN = re.compile(r"^platform-release-ping-[0-9a-f]{32}$")
REDIS_SCHEME = "redis"
REDIS_PORT = 6379
RESULT_KEY_PREFIX = b"celery-task-meta-"
CONTROL_MAX_BYTES = 16 * 1024
CLEANUP_CONTROL_MAX_BYTES = 1024
CLEANUP_RESULT_URL_ENV = "PLATFORM_LIVENESS_CLEANUP_RESULT_URL"
CLEANUP_RESULT_KEY_ENV = "PLATFORM_LIVENESS_CLEANUP_RESULT_KEY"

CHECK_NAMES = (
    "worker_uid",
    "worker_env",
    "release_identity",
    "broker_namespace",
    "result_namespace",
    "task_route",
    "task_result",
)
BACKLOG_NAMES = ("high", "default", "low")
CHECK_STATES = frozenset({"not_run", "passed", "failed"})
RESULT_STATUSES = frozenset({"passed", "failed", "cleanup_unproven"})
CLEANUP_STATES = frozenset({"proven", "unproven", "not_run"})


class LivenessFailure(RuntimeError):
    """A deliberately non-sensitive liveness failure code."""

    def __init__(self, code: str, *, cleanup_unproven: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.cleanup_unproven = cleanup_unproven


@dataclass(frozen=True, slots=True)
class RunIdentity:
    app_dir: Path
    release: Path
    expected_source_sha: str
    worker_env: Path


def _new_result(status: str, checks: Mapping[str, str], cleanup: str) -> dict[str, object]:
    """Build the only output shape this helper is allowed to render."""

    safe_status = (
        status if isinstance(status, str) and status in RESULT_STATUSES else "failed"
    )
    safe_cleanup = (
        cleanup if isinstance(cleanup, str) and cleanup in CLEANUP_STATES else "unproven"
    )
    return {
        "schema": 1,
        "kind": "platform_worker_liveness",
        "status": safe_status,
        "checks": {
            name: (
                value
                if isinstance(value := checks.get(name, "not_run"), str)
                and value in CHECK_STATES
                else "failed"
            )
            for name in CHECK_NAMES
        },
        "backlog": {name: "redacted" for name in BACKLOG_NAMES},
        "cleanup": safe_cleanup,
    }


def _emit_result(payload: Mapping[str, object]) -> None:
    # Keep this a single JSON line.  No caller-controlled data is present in
    # the payload, so there is no path for secrets or task IDs to be rendered.
    print(json.dumps(payload, separators=(",", ":")))


def _expected_uid() -> int:
    try:
        return pwd.getpwnam(WORKER_USER).pw_uid
    except KeyError as exc:
        raise LivenessFailure("worker_user_missing") from exc


def _validate_sha(value: str) -> str:
    if not isinstance(value, str) or SHA_PATTERN.fullmatch(value) is None:
        raise LivenessFailure("expected_source_sha_invalid")
    return value


def _regular_file(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LivenessFailure(f"{label}_missing") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise LivenessFailure(f"{label}_unsafe")
    return metadata


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _release_source_sha(release_json: Path) -> tuple[str, str]:
    _regular_file(release_json, label="release_metadata")
    try:
        payload = json.loads(
            release_json.read_text(encoding="ascii"),
            object_pairs_hook=_strict_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LivenessFailure("release_metadata_invalid") from exc
    if not isinstance(payload, dict):
        raise LivenessFailure("release_metadata_invalid")
    source_sha = payload.get("source_git_commit")
    release_slug = payload.get("release_slug")
    if (
        not isinstance(source_sha, str)
        or SHA_PATTERN.fullmatch(source_sha) is None
        or not isinstance(release_slug, str)
        or not release_slug
    ):
        raise LivenessFailure("release_metadata_invalid")
    return source_sha, release_slug


def validate_release_identity(
    app_dir: Path,
    release: Path,
    expected_source_sha: str,
) -> RunIdentity:
    """Prove the active pointer and metadata before importing the worker."""

    expected_source_sha = _validate_sha(expected_source_sha)
    try:
        app_dir = app_dir.resolve(strict=True)
        release = release.resolve(strict=True)
        releases_root = (app_dir / "releases").resolve(strict=True)
    except OSError as exc:
        raise LivenessFailure("release_layout_unavailable") from exc
    if (
        release.parent != releases_root
        or release == releases_root
        or RELEASE_SLUG_PATTERN.fullmatch(release.name) is None
    ):
        raise LivenessFailure("release_layout_invalid")

    current = app_dir / "current"
    try:
        current_metadata = current.lstat()
        active_release = current.resolve(strict=True)
    except OSError as exc:
        raise LivenessFailure("active_pointer_unavailable") from exc
    if not stat.S_ISLNK(current_metadata.st_mode) or active_release != release:
        raise LivenessFailure("active_pointer_mismatch")

    source_sha, release_slug = _release_source_sha(release / "RELEASE.json")
    if source_sha != expected_source_sha or release_slug != release.name:
        raise LivenessFailure("release_identity_mismatch")

    worker_env = app_dir / "shared" / "env" / WORKER_ENV_NAME
    return RunIdentity(
        app_dir=app_dir,
        release=release,
        expected_source_sha=expected_source_sha,
        worker_env=worker_env,
    )


def validate_worker_execution(identity: RunIdentity) -> None:
    """Validate the fixed worker UID and generated environment boundary."""

    if os.geteuid() != _expected_uid():
        raise LivenessFailure("worker_uid_mismatch")
    if os.environ.get("PLATFORM_RUNTIME_SERVICE") != WORKER_RUNTIME_SERVICE:
        raise LivenessFailure("worker_service_mismatch")

    expected_env = str(identity.worker_env)
    if os.environ.get("PLATFORM_ENV_FILE") != expected_env:
        raise LivenessFailure("worker_env_path_mismatch")
    if os.environ.get("PLATFORM_APP_DIR") != str(identity.app_dir):
        raise LivenessFailure("worker_app_path_mismatch")
    if os.environ.get("PLATFORM_SHARED_DIR") != str(identity.app_dir / "shared"):
        raise LivenessFailure("worker_shared_path_mismatch")
    expected_python = str(identity.app_dir / "shared" / "venv" / "bin" / "python")
    if os.environ.get("PLATFORM_PYTHON_BIN") != expected_python:
        raise LivenessFailure("worker_python_path_mismatch")
    _regular_file(identity.worker_env, label="worker_env")

    try:
        worker_group = pwd.getpwnam(WORKER_USER).pw_gid
    except KeyError as exc:
        raise LivenessFailure("worker_user_missing") from exc
    metadata = identity.worker_env.stat()
    if metadata.st_uid != 0 or metadata.st_gid != worker_group:
        raise LivenessFailure("worker_env_owner_mismatch")
    if stat.S_IMODE(metadata.st_mode) != 0o640:
        raise LivenessFailure("worker_env_mode_mismatch")

    pythonpath = os.environ.get("PYTHONPATH", "")
    if pythonpath.split(os.pathsep) != [str(identity.release)]:
        raise LivenessFailure("worker_pythonpath_mismatch")


def _load_worker_environment(identity: RunIdentity) -> None:
    """Load only the generated worker env before importing the app module."""

    try:
        safe_env_path = identity.release / "tools" / "platform_safe_env_exec.py"
        spec = importlib.util.spec_from_file_location(
            "platform_worker_liveness_safe_env",
            safe_env_path,
        )
        if spec is None or spec.loader is None:
            raise ImportError
        safe_env = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(safe_env)
        values = safe_env.load_env_file(identity.worker_env)
    except Exception as exc:
        raise LivenessFailure("worker_env_parse_failed") from exc
    # The renderer owns this allowlist.  Requiring the broker/result values to
    # be present here prevents a settings default from silently selecting an
    # unrelated Redis database.
    required = {
        "PLATFORM_CELERY_BROKER_URL",
        "PLATFORM_CELERY_RESULT_BACKEND",
        "PLATFORM_REDIS_URL",
    }
    if not required.issubset(values):
        raise LivenessFailure("worker_env_incomplete")
    if any(
        key in values
        for key in (
            "PLATFORM_APP_DIR",
            "PLATFORM_ENV_FILE",
            "PLATFORM_PYTHON_BIN",
            "PLATFORM_RUNTIME_SERVICE",
            "PLATFORM_SHARED_DIR",
        )
    ):
        raise LivenessFailure("worker_env_control_override")
    os.environ.update(values)


def _url_has_credentials(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return parts.username is not None or parts.password is not None or "@" in parts.netloc
    except ValueError:
        return False


def _redis_namespace(
    url: object,
    *,
    expected_database: str,
    credentials_required: bool = False,
) -> None:
    """Validate the one local Redis URL shape used by the deployed worker."""

    if not isinstance(url, str) or not url or url != url.strip():
        raise LivenessFailure("redis_url_invalid")
    # Keep the scheme and authority in the exact canonical form used by the
    # generated worker environment.  ``urlsplit`` lower-cases schemes and
    # accepts a leading-zero port, neither of which is the contract we want to
    # bind the release smoke to.
    if not url.startswith(f"{REDIS_SCHEME}://"):
        raise LivenessFailure("redis_url_invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        raise LivenessFailure("redis_url_invalid")
    if re.search(r"%(?![0-9A-Fa-f]{2})", url) or "?" in url or "#" in url:
        raise LivenessFailure("redis_url_invalid")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        # ``SplitResult.port`` raises for malformed, empty or out-of-range
        # ports; do not let that parser exception escape or render.
        raise LivenessFailure("redis_url_invalid") from exc
    if (
        parts.scheme != REDIS_SCHEME
        or not parts.netloc
        or hostname is None
        or port != REDIS_PORT
        or parts.path != f"/{expected_database}"
        or parts.query
        or parts.fragment
    ):
        raise LivenessFailure("redis_namespace_mismatch")
    if any(delimiter in parts.netloc for delimiter in (",", ";")):
        raise LivenessFailure("redis_host_invalid")
    if parts.netloc.count("@") > 1:
        raise LivenessFailure("redis_url_invalid")
    authority = parts.netloc.rsplit("@", 1)[-1]
    if not authority.endswith(f":{REDIS_PORT}"):
        raise LivenessFailure("redis_namespace_mismatch")
    has_credentials = (
        parts.username is not None
        or parts.password is not None
        or "@" in parts.netloc
    )
    if has_credentials:
        # Local production Redis is normally unauthenticated.  Preserve a
        # deployed credential only when the generated worker env explicitly
        # requires one; it is never copied into evidence or diagnostics.
        if not credentials_required or not parts.username or not parts.password:
            raise LivenessFailure("redis_credentials_invalid")
    # Production Redis is loopback-only.  Reject names that could resolve to a
    # remote host even when they happen to use the expected logical database.
    try:
        import ipaddress

        host = ipaddress.ip_address(hostname)
    except (ValueError, TypeError) as exc:
        raise LivenessFailure("redis_host_invalid") from exc
    if not host.is_loopback or "%" in hostname:
        raise LivenessFailure("redis_host_invalid")


def _remaining(
    deadline: float,
    clock: Callable[[], float] | None = None,
) -> float:
    if clock is None:
        clock = time.monotonic
    remaining = deadline - clock()
    if remaining <= 0:
        raise LivenessFailure("deadline_exceeded", cleanup_unproven=True)
    return remaining


def _task_queue(app: Any) -> str:
    try:
        default_queue = app.conf.task_default_queue
        task = app.tasks[PING_TASK_NAME]
        route = app.amqp.router.route({}, PING_TASK_NAME, (), {})
        queue = route.get("queue")
        queue_name = getattr(queue, "name", queue)
    except Exception as exc:
        raise LivenessFailure("task_route_invalid") from exc
    if default_queue != DEFAULT_QUEUE or queue_name != DEFAULT_QUEUE:
        raise LivenessFailure("task_route_invalid")
    if getattr(task, "name", None) != PING_TASK_NAME:
        raise LivenessFailure("task_registry_invalid")
    return DEFAULT_QUEUE


def _new_task_id() -> str:
    task_id = f"platform-release-ping-{secrets.token_hex(16)}"
    if TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise LivenessFailure("task_id_invalid")
    return task_id


def _result_key(result: Any, task_id: str) -> object:
    try:
        key = result.backend.get_key_for_task(task_id)
    except Exception as exc:
        raise LivenessFailure("result_key_unavailable", cleanup_unproven=True) from exc
    if not isinstance(key, (str, bytes)):
        raise LivenessFailure("result_key_unavailable", cleanup_unproven=True)
    return key


def _validate_result_key(key: object, task_id: str) -> str | bytes:
    if not isinstance(key, (str, bytes)):
        raise LivenessFailure("result_key_unavailable", cleanup_unproven=True)
    encoded = key.encode("utf-8") if isinstance(key, str) else key
    if (
        not encoded
        or len(encoded) > 512
        or b"\x00" in encoded
        or b"\r" in encoded
        or b"\n" in encoded
        or task_id.encode("ascii") not in encoded
    ):
        raise LivenessFailure("result_key_invalid", cleanup_unproven=True)
    return key


def _validate_cleanup_key(key: object) -> str | bytes:
    if not isinstance(key, (str, bytes)):
        raise LivenessFailure("result_key_invalid", cleanup_unproven=True)
    encoded = key.encode("utf-8") if isinstance(key, str) else key
    if (
        not encoded
        or len(encoded) > 512
        or not encoded.startswith(RESULT_KEY_PREFIX)
        or b"\x00" in encoded
        or b"\r" in encoded
        or b"\n" in encoded
    ):
        raise LivenessFailure("result_key_invalid", cleanup_unproven=True)
    return key


def _cleanup_key(
    key: str | bytes,
    result_url: str,
    deadline: float,
    *,
    clock: Callable[[], float] | None = None,
) -> bool:
    """Delete and prove absence of exactly one key inside the cleanup child."""

    try:
        _remaining(deadline, clock)
        import redis

        timeout = min(1.0, _remaining(deadline, clock))
        client = redis.Redis.from_url(
            result_url,
            decode_responses=False,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
            retry_on_timeout=False,
        )
        try:
            _remaining(deadline, clock)
            client.delete(key)
            while True:
                _remaining(deadline, clock)
                if not client.exists(key):
                    return True
                time.sleep(min(0.05, _remaining(deadline, clock)))
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
    except LivenessFailure:
        raise
    except BaseException as exc:
        raise LivenessFailure("result_cleanup_unproven", cleanup_unproven=True) from exc


def _cleanup_roundtrip(
    key: str | bytes,
    result_url: str,
    deadline: float,
    control_fd: int,
) -> int:
    """Run Redis cleanup in the disposable cleanup process."""

    ok = False
    try:
        _redis_namespace(
            result_url,
            expected_database="14",
            credentials_required=_url_has_credentials(
                os.environ.get("PLATFORM_CELERY_RESULT_BACKEND")
            ),
        )
        _cleanup_key(key, result_url, deadline)
        ok = True
    except BaseException:
        # Only the fixed event below crosses the process boundary.  The parent
        # decides whether a missing/invalid event is cleanup_unproven.
        ok = False
    try:
        _child_send(control_fd, {"event": "cleanup", "ok": ok})
    finally:
        try:
            os.close(control_fd)
        except BaseException:
            pass
    return 0 if ok else 1


def _spawn_cleanup(
    identity: RunIdentity,
    key: str | bytes,
    result_url: str,
    deadline: float,
) -> tuple[subprocess.Popen[bytes], int]:
    """Start Redis cleanup in its own process group."""

    read_fd, write_fd = os.pipe()
    encoded_key = base64.b64encode(
        key.encode("utf-8") if isinstance(key, str) else key
    ).decode("ascii")
    environment = dict(os.environ)
    # Keep the URL and exact key out of argv and out of the emitted evidence.
    # The child is disposable and is stopped as part of this parent's
    # monotonic deadline supervision.
    environment[CLEANUP_RESULT_URL_ENV] = result_url
    environment[CLEANUP_RESULT_KEY_ENV] = encoded_key
    try:
        os.set_inheritable(write_fd, True)
        python_bin = os.environ.get("PLATFORM_PYTHON_BIN") or sys.executable
        command = [
            python_bin,
            str(Path(__file__).resolve()),
            "--cleanup-child",
            "--deadline",
            f"{deadline:.9f}",
            "--control-fd",
            str(write_fd),
        ]
        process = subprocess.Popen(
            command,
            cwd=str(identity.release),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(write_fd,),
            start_new_session=True,
        )
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    return process, read_fd


def _backlog_evidence(
    broker_url: str,
    deadline: float,
    *,
    clock: Callable[[], float] | None = None,
) -> None:
    """Read known queue lengths as non-gating, redacted evidence only."""

    try:
        import redis

        client = redis.Redis.from_url(
            broker_url,
            decode_responses=False,
            socket_connect_timeout=min(0.25, _remaining(deadline, clock)),
            socket_timeout=min(0.25, _remaining(deadline, clock)),
            retry_on_timeout=False,
        )
        try:
            for queue in (
                "deadlock-platform-high",
                DEFAULT_QUEUE,
                "deadlock-platform-low",
            ):
                _remaining(deadline, clock)
                client.llen(queue)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
    except BaseException:
        # The JSON contract deliberately keeps this evidence redacted and
        # never turns a best-effort backlog read into a liveness failure.
        return


@dataclass(slots=True)
class _ChildOutcome:
    key: str | bytes | None = None
    attempted: bool = False
    uncertain: bool = False
    terminal: bool = False
    success: bool = False
    route_state: str = "not_run"
    result_state: str = "not_run"


def _child_send(control_fd: int, payload: Mapping[str, object]) -> bool:
    try:
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if len(raw) > CONTROL_MAX_BYTES:
            return False
        raw += b"\n"
        offset = 0
        while offset < len(raw):
            offset += os.write(control_fd, raw[offset:])
        return True
    except BaseException:
        return False


def _encoded_key(key: str | bytes | None) -> str | None:
    if key is None:
        return None
    raw = key.encode("utf-8") if isinstance(key, str) else key
    return base64.b64encode(raw).decode("ascii")


def _child_roundtrip(task_id: str, deadline: float, control_fd: int) -> int:
    """Run the potentially blocking Celery work in the disposable child."""

    result: Any | None = None
    key: str | bytes | None = None
    publish_attempted = False
    terminal = False
    success = False
    stage = "worker_app"
    try:
        if TASK_ID_PATTERN.fullmatch(task_id) is None:
            raise LivenessFailure("task_id_invalid")
        worker_module = importlib.import_module("apps.platform_worker.worker")
        app = getattr(worker_module, "celery_app", None)
        if app is None:
            raise LivenessFailure("worker_app_unavailable")
        stage = "namespace"
        broker_url = app.conf.broker_url
        result_url = app.conf.result_backend
        _redis_namespace(
            broker_url,
            expected_database="13",
            credentials_required=_url_has_credentials(
                os.environ.get("PLATFORM_CELERY_BROKER_URL")
            ),
        )
        _redis_namespace(
            result_url,
            expected_database="14",
            credentials_required=_url_has_credentials(
                os.environ.get("PLATFORM_CELERY_RESULT_BACKEND")
            ),
        )
        stage = "task_route"
        queue_name = _task_queue(app)
        _child_send(control_fd, {"event": "route", "ok": True})
        stage = "result_key"
        task_id = _validate_task_id(task_id)
        result = app.AsyncResult(task_id)
        key = _validate_result_key(_result_key(result, task_id), task_id)
        _child_send(
            control_fd,
            {"event": "prepared", "key": _encoded_key(key)},
        )
        _remaining(deadline)
        task = app.tasks[PING_TASK_NAME]
        expires = min(DEFAULT_EXPIRES_SECONDS, _remaining(deadline))
        if expires <= 0:
            raise LivenessFailure("deadline_exceeded", cleanup_unproven=True)
        stage = "publish"
        publish_attempted = True
        result = task.apply_async(
            args=(),
            kwargs={},
            task_id=task_id,
            queue=queue_name,
            routing_key=queue_name,
            retry=False,
            expires=expires,
        )
        key = _validate_result_key(_result_key(result, task_id), task_id)
        _child_send(
            control_fd,
            {"event": "published", "key": _encoded_key(key)},
        )
        stage = "result"
        value = result.get(timeout=_remaining(deadline), propagate=False)
        state = result.state
        terminal = True
        success = state == "SUCCESS" and value == "pong"
    except BaseException:
        # The parent receives only the closed event vocabulary.  In
        # particular, never send an exception or result value through the
        # control pipe.
        _child_send(
            control_fd,
            {
                "event": "failure",
                "published": publish_attempted,
                "stage": stage,
                "key": _encoded_key(key),
            },
        )
    finally:
        forget_ok = False
        if result is not None:
            try:
                result.forget()
                forget_ok = True
            except BaseException:
                forget_ok = False
        if terminal:
            _child_send(
                control_fd,
                {
                    "event": "terminal",
                    "ok": success,
                    "forget_ok": forget_ok,
                    "key": _encoded_key(key),
                },
            )
        try:
            os.close(control_fd)
        except BaseException:
            pass
    return 0 if terminal and success else 1


def _validate_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise LivenessFailure("task_id_invalid")
    return task_id


def _spawn_child(
    identity: RunIdentity,
    task_id: str,
    deadline: float,
) -> tuple[subprocess.Popen[bytes], int]:
    read_fd, write_fd = os.pipe()
    try:
        os.set_inheritable(write_fd, True)
        python_bin = os.environ.get("PLATFORM_PYTHON_BIN") or sys.executable
        command = [
            python_bin,
            str(Path(__file__).resolve()),
            "--child",
            "--task-id",
            task_id,
            "--deadline",
            f"{deadline:.9f}",
            "--control-fd",
            str(write_fd),
        ]
        process = subprocess.Popen(
            command,
            cwd=str(identity.release),
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(write_fd,),
            start_new_session=True,
        )
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    return process, read_fd


def _signal_child_group(process: subprocess.Popen[bytes], signal_number: int) -> None:
    try:
        os.killpg(process.pid, signal_number)
    except ProcessLookupError:
        pass
    except BaseException:
        try:
            process.send_signal(signal_number)
        except BaseException:
            pass


def _stop_child_now(
    process: subprocess.Popen[bytes],
    *,
    clock: Callable[[], float],
) -> None:
    if process.poll() is None:
        _signal_child_group(process, signal.SIGTERM)
        stop_deadline = clock() + CHILD_STOP_GRACE_SECONDS
        attempts = 0
        while process.poll() is None and clock() < stop_deadline and attempts < 100:
            time.sleep(0.01)
            attempts += 1
        if process.poll() is None:
            _signal_child_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=CHILD_KILL_GRACE_SECONDS)
    except BaseException:
        _signal_child_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=CHILD_KILL_GRACE_SECONDS)
        except BaseException:
            pass


@dataclass(slots=True)
class _ChildCollection:
    events: list[dict[str, object]]
    valid: bool
    returncode: int | None
    forced_termination: bool
    natural_exit_before_deadline: bool


def _collect_child(
    process: subprocess.Popen[bytes],
    control_fd: int,
    *,
    child_deadline: float,
    supervise_deadline: float,
    clock: Callable[[], float],
) -> _ChildCollection:
    """Drain all child pipes while owning stop, kill, wait and EOF bounds."""

    selector = selectors.DefaultSelector()
    control = bytearray()
    control_overflow = False
    open_fds: set[int] = set()
    forced_termination = False
    natural_exit_before_deadline = False
    try:
        for fd in (control_fd, process.stdout.fileno() if process.stdout else None,
                   process.stderr.fileno() if process.stderr else None):
            if fd is None:
                continue
            os.set_blocking(fd, False)
            selector.register(fd, selectors.EVENT_READ)
            open_fds.add(fd)
        stop_at: float | None = None
        kill_sent = False
        while open_fds or process.poll() is None:
            now = clock()
            returncode = process.poll()
            if (
                returncode is not None
                and not forced_termination
                and now < child_deadline
            ):
                natural_exit_before_deadline = True
            if process.poll() is None and stop_at is None and now >= child_deadline:
                forced_termination = True
                _signal_child_group(process, signal.SIGTERM)
                stop_at = now + CHILD_STOP_GRACE_SECONDS
            elif process.poll() is None and stop_at is not None and not kill_sent and now >= stop_at:
                forced_termination = True
                _signal_child_group(process, signal.SIGKILL)
                kill_sent = True
            if now >= supervise_deadline:
                # The leader may have exited while a descendant still holds
                # one of the inherited log/control pipes.  Kill the whole
                # disposable group before closing those descriptors so no
                # late writer can survive the parent deadline.
                forced_termination = True
                _signal_child_group(process, signal.SIGKILL)
                break
            timeout = min(0.05, max(0.0, supervise_deadline - now))
            for selected, _ in selector.select(timeout):
                fd = selected.fd
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                except BaseException:
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(fd)
                    except BaseException:
                        pass
                    open_fds.discard(fd)
                    continue
                if fd == control_fd:
                    available = max(0, CONTROL_MAX_BYTES - len(control))
                    if len(chunk) > available:
                        control_overflow = True
                    if available:
                        control.extend(chunk[:available])
                # Child stdout/stderr are intentionally drained and discarded.
        if (
            process.poll() is not None
            and not forced_termination
            and clock() < child_deadline
        ):
            natural_exit_before_deadline = True
        if process.poll() is None:
            _stop_child_now(process, clock=clock)
        else:
            try:
                process.wait(timeout=CHILD_KILL_GRACE_SECONDS)
            except BaseException:
                _stop_child_now(process, clock=clock)
    finally:
        for fd in tuple(open_fds):
            try:
                selector.unregister(fd)
            except BaseException:
                pass
            try:
                os.close(fd)
            except BaseException:
                pass
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except BaseException:
                    pass
    events: list[dict[str, object]] = []
    valid = (
        not control_overflow
        and len(control) <= CONTROL_MAX_BYTES
        and (not control or control.endswith(b"\n"))
    )
    for line in bytes(control).splitlines():
        if not line:
            continue
        try:
            value = json.loads(line.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError):
            valid = False
            continue
        if not isinstance(value, dict) or value.get("event") not in {
            "route", "prepared", "published", "terminal", "failure"
        }:
            valid = False
            continue
        events.append(value)
    return _ChildCollection(
        events=events,
        valid=valid and bool(open_fds) is False,
        returncode=process.returncode,
        forced_termination=forced_termination,
        natural_exit_before_deadline=natural_exit_before_deadline,
    )


@dataclass(slots=True)
class _CleanupCollection:
    ok: bool
    valid: bool
    returncode: int | None
    forced_termination: bool
    natural_exit_before_deadline: bool


def _stop_cleanup_now(
    process: subprocess.Popen[bytes],
    *,
    deadline: float,
    clock: Callable[[], float],
) -> bool:
    """TERM, KILL, wait and reap one cleanup process within ``deadline``."""

    forced = False
    try:
        if process.poll() is None:
            forced = True
            _signal_child_group(process, signal.SIGTERM)
            stop_deadline = min(deadline, clock() + CLEANUP_STOP_GRACE_SECONDS)
            while process.poll() is None and clock() < stop_deadline:
                time.sleep(min(0.01, max(0.0, stop_deadline - clock())))
            if process.poll() is None:
                forced = True
                _signal_child_group(process, signal.SIGKILL)
        else:
            # A leader can have exited while a descendant still owns one of
            # the inherited protocol/log pipes.  Always close that group when
            # cleanup collection was interrupted.
            forced = True
            _signal_child_group(process, signal.SIGKILL)
        remaining = max(0.0, deadline - clock())
        try:
            process.wait(timeout=remaining)
        except BaseException:
            if process.poll() is None:
                forced = True
                _signal_child_group(process, signal.SIGKILL)
            remaining = max(0.0, deadline - clock())
            try:
                process.wait(timeout=remaining)
            except BaseException:
                pass
    except BaseException:
        forced = True
        try:
            _signal_child_group(process, signal.SIGKILL)
            process.wait(timeout=max(0.0, deadline - clock()))
        except BaseException:
            pass
    return forced


def _collect_cleanup(
    process: subprocess.Popen[bytes],
    control_fd: int,
    *,
    deadline: float,
    clock: Callable[[], float],
) -> _CleanupCollection:
    """Supervise cleanup, including protocol EOF and process-group reaping."""

    selector = selectors.DefaultSelector()
    control = bytearray()
    control_overflow = False
    open_fds: set[int] = set()
    forced_termination = False
    natural_exit_before_deadline = False
    term_at = max(
        clock(),
        deadline - CLEANUP_STOP_GRACE_SECONDS - CLEANUP_KILL_GRACE_SECONDS,
    )
    stop_at: float | None = None
    kill_sent = False
    try:
        for fd in (
            control_fd,
            process.stdout.fileno() if process.stdout else None,
            process.stderr.fileno() if process.stderr else None,
        ):
            if fd is None:
                continue
            os.set_blocking(fd, False)
            selector.register(fd, selectors.EVENT_READ)
            open_fds.add(fd)
        while open_fds or process.poll() is None:
            now = clock()
            returncode = process.poll()
            if (
                returncode is not None
                and not forced_termination
                and now < deadline
            ):
                natural_exit_before_deadline = True
            if process.poll() is None and stop_at is None and now >= term_at:
                forced_termination = True
                _signal_child_group(process, signal.SIGTERM)
                stop_at = min(deadline, now + CLEANUP_STOP_GRACE_SECONDS)
            elif process.poll() is None and stop_at is not None and not kill_sent and now >= stop_at:
                forced_termination = True
                _signal_child_group(process, signal.SIGKILL)
                kill_sent = True
            if now >= deadline:
                forced_termination = True
                _signal_child_group(process, signal.SIGKILL)
                kill_sent = True
                break
            timeout = min(0.05, max(0.0, deadline - now))
            for selected, _ in selector.select(timeout):
                fd = selected.fd
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                except BaseException:
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(fd)
                    except BaseException:
                        pass
                    open_fds.discard(fd)
                    continue
                if fd == control_fd:
                    available = max(0, CLEANUP_CONTROL_MAX_BYTES - len(control))
                    if len(chunk) > available:
                        control_overflow = True
                    if available:
                        control.extend(chunk[:available])
        if (
            process.poll() is not None
            and not forced_termination
            and clock() < deadline
        ):
            natural_exit_before_deadline = True
        if process.poll() is None:
            forced_termination = _stop_cleanup_now(
                process,
                deadline=deadline,
                clock=clock,
            ) or forced_termination
        else:
            try:
                process.wait(timeout=max(0.0, deadline - clock()))
            except BaseException:
                forced_termination = True
    finally:
        for fd in tuple(open_fds):
            try:
                selector.unregister(fd)
            except BaseException:
                pass
            try:
                os.close(fd)
            except BaseException:
                pass
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except BaseException:
                    pass

    valid = (
        not control_overflow
        and len(control) <= CLEANUP_CONTROL_MAX_BYTES
        and bool(control)
        and control.endswith(b"\n")
    )
    ok = False
    if valid:
        lines = bytes(control).splitlines()
        if len(lines) != 1:
            valid = False
        else:
            try:
                event = json.loads(
                    lines[0].decode("ascii"),
                    object_pairs_hook=_strict_object,
                )
            except (UnicodeError, json.JSONDecodeError, ValueError):
                valid = False
            else:
                if (
                    not isinstance(event, dict)
                    or set(event) != {"event", "ok"}
                    or event.get("event") != "cleanup"
                    or not isinstance(event.get("ok"), bool)
                ):
                    valid = False
                else:
                    ok = event["ok"]
    return _CleanupCollection(
        ok=ok,
        valid=valid and not open_fds,
        returncode=process.returncode,
        forced_termination=forced_termination,
        natural_exit_before_deadline=natural_exit_before_deadline,
    )


def _decode_event_key(event: Mapping[str, object], task_id: str) -> str | bytes | None:
    encoded = event.get("key")
    if encoded is None:
        return None
    if not isinstance(encoded, str) or len(encoded) > 2048:
        raise LivenessFailure("result_key_invalid", cleanup_unproven=True)
    try:
        key = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise LivenessFailure("result_key_invalid", cleanup_unproven=True) from exc
    return _validate_result_key(key, task_id)


def _summarize_child(
    events: list[dict[str, object]],
    *,
    valid: bool,
    task_id: str,
) -> _ChildOutcome:
    outcome = _ChildOutcome(uncertain=not valid or not events)
    for event in events:
        name = event.get("event")
        try:
            event_key = _decode_event_key(event, task_id)
        except LivenessFailure:
            outcome.uncertain = True
            continue
        if event_key is not None:
            outcome.key = event_key
        if name == "route":
            outcome.route_state = "passed" if event.get("ok") is True else "failed"
        elif name == "prepared":
            if event_key is None:
                outcome.uncertain = True
        elif name == "published":
            outcome.attempted = True
        elif name == "failure":
            outcome.attempted = outcome.attempted or event.get("published") is True
            if event.get("stage") in {"result_key", "publish", "result"}:
                # An AsyncResult may already have reserved or published a
                # task even when the child could not report a valid key.
                outcome.uncertain = True
            if event.get("stage") == "task_route":
                outcome.route_state = "failed"
        elif name == "terminal":
            outcome.terminal = True
            outcome.success = event.get("ok") is True
            outcome.result_state = "passed" if outcome.success else "failed"
            if event.get("forget_ok") is not True:
                # A successful Celery value is not a successful probe until
                # the child has completed its own forget path.  The parent
                # still performs the exact-key deletion and absence proof,
                # but must not claim a clean roundtrip when forget failed.
                outcome.success = False
                outcome.result_state = "failed"
                outcome.uncertain = True
            if event_key is None:
                outcome.uncertain = True
            if outcome.route_state == "not_run":
                outcome.route_state = "passed"
            outcome.attempted = True
    if not outcome.terminal and outcome.attempted:
        outcome.uncertain = True
    return outcome


def run_liveness(
    identity: RunIdentity,
    *,
    clock: Callable[[], float] | None = None,
    spawn_child: Callable[
        [RunIdentity, str, float], tuple[subprocess.Popen[bytes], int]
    ] | None = None,
    spawn_cleanup: Callable[
        [RunIdentity, str | bytes, str, float], tuple[subprocess.Popen[bytes], int]
    ]
    | None = None,
) -> dict[str, object]:
    """Supervise one roundtrip and isolated exact-key cleanup."""

    if clock is None:
        clock = time.monotonic
    if spawn_child is None:
        spawn_child = _spawn_child
    if spawn_cleanup is None:
        spawn_cleanup = _spawn_cleanup
    checks: dict[str, str] = {}
    try:
        start = clock()
    except BaseException:
        return _new_result("failed", checks, "not_run")
    deadline = start + MAX_SECONDS
    child_deadline = min(deadline, start + CHILD_WORK_SECONDS)
    supervise_deadline = min(deadline, child_deadline + CHILD_STOP_GRACE_SECONDS + CHILD_KILL_GRACE_SECONDS)
    task_id: str | None = None
    broker_url: str | None = None
    result_url: str | None = None
    handle: tuple[subprocess.Popen[bytes], int] | None = None
    collection_completed = False
    cleanup_handle: tuple[subprocess.Popen[bytes], int] | None = None
    cleanup_collection_completed = False
    outcome = _ChildOutcome(uncertain=True)
    cleanup_status = "not_run"
    status = "failed"
    try:
        validate_worker_execution(identity)
        checks["worker_uid"] = "passed"
        checks["worker_env"] = "passed"
        _load_worker_environment(identity)
        broker_url = os.environ.get("PLATFORM_CELERY_BROKER_URL")
        result_url = os.environ.get("PLATFORM_CELERY_RESULT_BACKEND")
        app_redis_url = os.environ.get("PLATFORM_REDIS_URL")
        _redis_namespace(
            broker_url,
            expected_database="13",
            credentials_required=_url_has_credentials(broker_url),
        )
        checks["broker_namespace"] = "passed"
        _redis_namespace(
            result_url,
            expected_database="14",
            credentials_required=_url_has_credentials(result_url),
        )
        checks["result_namespace"] = "passed"
        _redis_namespace(
            app_redis_url,
            expected_database="15",
            credentials_required=_url_has_credentials(app_redis_url),
        )
        checks["release_identity"] = "passed"
        task_id = _new_task_id()
        handle = spawn_child(identity, task_id, child_deadline)
        child_collection = _collect_child(
            handle[0],
            handle[1],
            child_deadline=child_deadline,
            supervise_deadline=supervise_deadline,
            clock=clock,
        )
        collection_completed = True
        outcome = _summarize_child(
            child_collection.events,
            valid=child_collection.valid,
            task_id=task_id,
        )
        # A terminal payload is not enough: a forced stop, signal, nonzero
        # return, or exit observed after the child deadline is an uncertain
        # publish and must remain cleanup_unproven.
        if not (
            child_collection.valid
            and child_collection.natural_exit_before_deadline
            and child_collection.returncode == 0
            and not child_collection.forced_termination
        ):
            outcome.uncertain = True
            outcome.success = False
            if outcome.terminal:
                outcome.result_state = "failed"
        checks["task_route"] = outcome.route_state
        checks["task_result"] = outcome.result_state
    except BaseException:
        # A cancellation, keyboard interrupt, or child-start failure never
        # escapes with a traceback or an assertion of successful cleanup.
        outcome = _ChildOutcome(uncertain=handle is not None)
    finally:
        child_running = False
        if handle is not None:
            try:
                child_running = handle[0].poll() is None
            except BaseException:
                child_running = True
                outcome.uncertain = True
        if handle is not None and (child_running or not collection_completed):
            try:
                if child_running:
                    _stop_child_now(handle[0], clock=clock)
                else:
                    _signal_child_group(handle[0], signal.SIGKILL)
            except BaseException:
                # Cancellation can arrive while the parent is in its TERM /
                # KILL reserve.  Never let it bypass exact-key cleanup or
                # render a traceback; make the outcome unproven instead.
                outcome.uncertain = True
                try:
                    _signal_child_group(handle[0], signal.SIGKILL)
                    handle[0].wait(timeout=CHILD_KILL_GRACE_SECONDS)
                except BaseException:
                    pass
        if task_id is not None and result_url is not None:
            cleanup_key = outcome.key
            if cleanup_key is None and (outcome.attempted or outcome.uncertain):
                # The deployed Redis backend uses this fixed key prefix.  A
                # fallback cleanup is attempted, but an uncertain child can
                # never be reported as proven merely because this key is gone.
                cleanup_key = RESULT_KEY_PREFIX + task_id.encode("ascii")
            cleanup_ok = False
            if cleanup_key is not None:
                try:
                    cleanup_key = _validate_result_key(cleanup_key, task_id)
                    _remaining(deadline, clock)
                    cleanup_handle = spawn_cleanup(
                        identity,
                        cleanup_key,
                        result_url,
                        deadline,
                    )
                    cleanup_collection = _collect_cleanup(
                        cleanup_handle[0],
                        cleanup_handle[1],
                        deadline=deadline,
                        clock=clock,
                    )
                    cleanup_collection_completed = True
                    cleanup_ok = (
                        cleanup_collection.valid
                        and cleanup_collection.ok
                        and cleanup_collection.returncode == 0
                        and cleanup_collection.natural_exit_before_deadline
                        and not cleanup_collection.forced_termination
                    )
                except BaseException:
                    cleanup_ok = False
                finally:
                    cleanup_running = cleanup_handle is not None and not cleanup_collection_completed
                    if cleanup_handle is not None:
                        try:
                            cleanup_running = cleanup_running or cleanup_handle[0].poll() is None
                        except BaseException:
                            cleanup_running = True
                            cleanup_ok = False
                    if cleanup_handle is not None and cleanup_running:
                        try:
                            _stop_cleanup_now(
                                cleanup_handle[0],
                                deadline=deadline,
                                clock=clock,
                            )
                        except BaseException:
                            cleanup_ok = False
                    if cleanup_handle is not None:
                        for stream in (
                            cleanup_handle[0].stdout,
                            cleanup_handle[0].stderr,
                        ):
                            if stream is not None:
                                try:
                                    stream.close()
                                except BaseException:
                                    pass
            if cleanup_ok and (outcome.terminal or not outcome.attempted) and not outcome.uncertain:
                cleanup_status = "proven"
            elif cleanup_key is not None:
                cleanup_status = "unproven"
            elif outcome.terminal or outcome.attempted or outcome.uncertain:
                cleanup_status = "unproven"
        if cleanup_status == "unproven":
            status = "cleanup_unproven"
        elif outcome.terminal and outcome.success and outcome.result_state == "passed":
            status = "passed"
        else:
            status = "failed"
        if status == "passed" and broker_url and cleanup_status == "proven":
            _backlog_evidence(broker_url, deadline, clock=clock)
        if handle is not None:
            for stream in (handle[0].stdout, handle[0].stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException:
                        pass
    return _new_result(status, checks, cleanup_status)


def _run(identity: RunIdentity) -> tuple[dict[str, object], int]:
    payload = run_liveness(identity)
    status = payload["status"]
    return payload, 0 if status == "passed" else 2 if status == "cleanup_unproven" else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the release worker liveness smoke.")
    parser.add_argument("--app-dir", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--expected-source-sha")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--cleanup-child", action="store_true")
    parser.add_argument("--task-id")
    parser.add_argument("--deadline", type=float)
    parser.add_argument("--control-fd", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    checks: dict[str, str] = {}
    try:
        args = _parse_args(argv)
        if args.child:
            if (
                not isinstance(args.task_id, str)
                or args.deadline is None
                or args.control_fd is None
            ):
                return 1
            return _child_roundtrip(args.task_id, args.deadline, args.control_fd)
        if args.cleanup_child:
            if args.deadline is None or args.control_fd is None:
                return 1
            result_url = os.environ.get(CLEANUP_RESULT_URL_ENV)
            encoded_key = os.environ.get(CLEANUP_RESULT_KEY_ENV)
            if not isinstance(result_url, str) or not isinstance(encoded_key, str):
                return 1
            try:
                key = _validate_cleanup_key(
                    base64.b64decode(encoded_key.encode("ascii"), validate=True)
                )
            except (ValueError, UnicodeError, binascii.Error, LivenessFailure):
                return 1
            return _cleanup_roundtrip(
                key,
                result_url,
                args.deadline,
                args.control_fd,
            )
        if args.app_dir is None or args.release is None or args.expected_source_sha is None:
            raise LivenessFailure("arguments_invalid")
        identity = validate_release_identity(
            args.app_dir,
            args.release,
            args.expected_source_sha,
        )
        checks["release_identity"] = "passed"
        payload, status = _run(identity)
    except LivenessFailure:
        payload = _new_result("failed", checks, "not_run")
        status = 1
    except BaseException:
        payload = _new_result("failed", checks, "not_run")
        status = 1
    _emit_result(payload)
    return status


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BACKLOG_NAMES",
    "CHECK_NAMES",
    "DEFAULT_QUEUE",
    "LivenessFailure",
    "MAX_SECONDS",
    "RunIdentity",
    "TASK_ID_PATTERN",
    "_new_task_id",
    "run_liveness",
    "validate_release_identity",
    "validate_worker_execution",
]
