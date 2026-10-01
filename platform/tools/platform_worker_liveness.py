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
import stat
import time
from typing import Any, Callable
from urllib.parse import urlsplit


MAX_SECONDS = 15.0
DEFAULT_EXPIRES_SECONDS = 10.0
WORKER_USER = "oldsparky-worker"
WORKER_RUNTIME_SERVICE = "worker"
DEFAULT_QUEUE = "deadlock-platform-default"
PING_TASK_NAME = "platform.ping"
WORKER_ENV_NAME = "worker.env"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
RELEASE_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
TASK_ID_PATTERN = re.compile(r"^platform-release-ping-[0-9a-f]{32}$")
REDIS_SCHEMES = frozenset({"redis", "rediss"})

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


def _redis_namespace(url: object, *, expected_database: str) -> None:
    if not isinstance(url, str):
        raise LivenessFailure("redis_url_invalid")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise LivenessFailure("redis_url_invalid") from exc
    if (
        parts.scheme not in REDIS_SCHEMES
        or parts.path != f"/{expected_database}"
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.hostname is None
    ):
        raise LivenessFailure("redis_namespace_mismatch")
    # Production Redis is loopback-only.  Reject names that could resolve to a
    # remote host even when they happen to use the expected logical database.
    try:
        import ipaddress

        host = ipaddress.ip_address(parts.hostname)
    except (ValueError, TypeError) as exc:
        raise LivenessFailure("redis_host_invalid") from exc
    if not host.is_loopback or "%" in parts.hostname:
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


def _cleanup_result(
    result: Any,
    task_id: str,
    result_url: str,
    deadline: float,
    *,
    clock: Callable[[], float] | None = None,
) -> bool:
    """Forget and prove absence of exactly one result key before the deadline."""

    try:
        key = _result_key(result, task_id)
        _remaining(deadline, clock)
        result.forget()
        # Importing redis is deliberately delayed until after the worker app
        # has passed all identity checks.  The key is never scanned or logged.
        import redis

        client = redis.Redis.from_url(
            result_url,
            decode_responses=False,
            socket_connect_timeout=min(1.0, _remaining(deadline, clock)),
            socket_timeout=min(1.0, _remaining(deadline, clock)),
        )
        try:
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
    except Exception as exc:
        raise LivenessFailure("result_cleanup_unproven", cleanup_unproven=True) from exc


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
    except Exception:
        # The JSON contract deliberately keeps this evidence redacted and
        # never turns a best-effort backlog read into a liveness failure.
        return


def run_liveness(
    identity: RunIdentity,
    *,
    clock: Callable[[], float] | None = None,
) -> dict[str, object]:
    """Execute the release-only roundtrip under one absolute deadline."""

    if clock is None:
        clock = time.monotonic
    checks: dict[str, str] = {}
    deadline = clock() + MAX_SECONDS
    result: Any | None = None
    task_id: str | None = None
    result_url: str | None = None
    cleanup_status = "not_run"
    status = "failed"
    try:
        validate_worker_execution(identity)
        checks["worker_uid"] = "passed"
        checks["worker_env"] = "passed"
        _load_worker_environment(identity)

        worker_module = importlib.import_module("apps.platform_worker.worker")
        app = getattr(worker_module, "celery_app", None)
        if app is None:
            raise LivenessFailure("worker_app_unavailable")
        broker_url = app.conf.broker_url
        result_url = app.conf.result_backend
        _redis_namespace(broker_url, expected_database="13")
        checks["broker_namespace"] = "passed"
        _redis_namespace(result_url, expected_database="14")
        checks["result_namespace"] = "passed"
        checks["release_identity"] = "passed"

        queue_name = _task_queue(app)
        checks["task_route"] = "passed"
        task_id = _new_task_id()
        result = app.AsyncResult(task_id)
        _remaining(deadline, clock)
        task = app.tasks[PING_TASK_NAME]
        expires = min(DEFAULT_EXPIRES_SECONDS, _remaining(deadline, clock))
        if expires <= 0:
            raise LivenessFailure("deadline_exceeded", cleanup_unproven=True)
        result = task.apply_async(
            args=(),
            kwargs={},
            task_id=task_id,
            queue=queue_name,
            routing_key=queue_name,
            retry=False,
            expires=expires,
        )
        _remaining(deadline, clock)
        value = result.get(timeout=_remaining(deadline, clock), propagate=False)
        state = result.state
        if state != "SUCCESS" or value != "pong":
            raise LivenessFailure("task_result_invalid")
        checks["task_result"] = "passed"
        _backlog_evidence(broker_url, deadline, clock=clock)
        status = "passed"
        cleanup_status = "proven"
    except LivenessFailure as exc:
        if exc.cleanup_unproven:
            cleanup_status = "unproven"
        checks.setdefault("release_identity", "not_run")
        status = "cleanup_unproven" if exc.cleanup_unproven else "failed"
    except Exception:
        status = "failed"
    finally:
        # Cleanup is deliberately handled in a second bounded path below.  It
        # is invoked even for result validation/publish failures when a task ID
        # was allocated, and never renders the caught exception.
        if task_id is not None and (result is None or result_url is None):
            # A task identity was allocated but no exact Celery result object
            # survived to the cleanup boundary.  There is no safe way to
            # derive/forget that backend key here, so fail closed.
            cleanup_status = "unproven"
        elif result is not None and task_id is not None and result_url is not None:
            try:
                cleanup_ok = _cleanup_result(
                    result,
                    task_id,
                    result_url,
                    deadline,
                    clock=clock,
                )
            except LivenessFailure:
                cleanup_ok = False
            except Exception:
                cleanup_ok = False
            if not cleanup_ok:
                cleanup_status = "unproven"
    if cleanup_status == "unproven":
        status = "cleanup_unproven"
    return _new_result(status, checks, cleanup_status)


def _run(identity: RunIdentity) -> tuple[dict[str, object], int]:
    payload = run_liveness(identity)
    status = payload["status"]
    return payload, 0 if status == "passed" else 2 if status == "cleanup_unproven" else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the release worker liveness smoke.")
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--expected-source-sha", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    checks: dict[str, str] = {}
    try:
        args = _parse_args(argv)
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
    except Exception:
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
