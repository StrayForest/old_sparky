#!/usr/bin/env python3
"""Read-only diagnosis of the systemd receipt capture preconditions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import types
from typing import Any


APP_DIR = Path("/opt/oldsparky/platform")
SHARED_DIR = APP_DIR / "shared"
CURRENT_LINK = APP_DIR / "current"
TRANSACTION = SHARED_DIR / ".release-operation.json"
SYSTEMD_RECEIPT = SHARED_DIR / ".release-systemd-state.json"
SYSTEMD_MODULE = CURRENT_LINK / "tools/platform_release_systemd_state.py"
MAX_MODULE_BYTES = 2 * 1024 * 1024
HEALTH_URLS = (
    ("api_live", "http://127.0.0.1:8010/api/v1/health/live"),
    ("api_ready", "http://127.0.0.1:8010/api/v1/health/ready"),
    ("web_local", "http://127.0.0.1:3000/"),
    ("public", "https://old-sparky.com/"),
)


class ProbeFailure(Exception):
    def __init__(
        self,
        check: str,
        error_class: str = "predicate",
        systemd_observation: dict[str, object] | None = None,
    ) -> None:
        super().__init__(check)
        self.check = check
        self.error_class = error_class
        self.systemd_observation = systemd_observation


_SYSTEMD_STATE_VALUES = frozenset(
    ("active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance", "unknown")
)
_SYSTEMD_ENABLED_VALUES = frozenset(("enabled", "disabled", "static", "indirect", "masked", "generated", "transient", "unknown"))


def _safe_systemd_value(value: str, command: str) -> str:
    if not value:
        return "empty"
    known = _SYSTEMD_STATE_VALUES if command == "is-active" else _SYSTEMD_ENABLED_VALUES
    return value if value in known else "other"


def _systemd_failure_observation(
    command: str,
    unit: str,
    result: tuple[int, str] | None,
    error: BaseException | None,
) -> dict[str, object]:
    if result is not None:
        status, value = result
        state = _safe_systemd_value(value, command)
        supported = {"active", "inactive"} if command == "is-active" else {"enabled", "disabled", "static"}
        return {
            "command": command,
            "unit": unit,
            "result": (
                "unsupported_state"
                if state not in supported
                else "status_mismatch"
                if (command == "is-active" and ((state == "active" and status != 0) or (state == "inactive" and status != 3)))
                or (command == "is-enabled" and ((state in {"enabled", "static"} and status != 0) or (state == "disabled" and status != 1)))
                else "state_error"
            ),
            "returncode": status if type(status) is int and 0 <= status <= 255 else "unavailable",
            "state": state,
        }
    error_text = str(error) if error is not None else ""
    result_class = (
        "malformed_output"
        if type(error).__name__ == "StateError" and error_text == "systemd command returned multiple states"
        else "command_error"
    )
    return {
        "command": command,
        "unit": unit,
        "result": result_class,
        "returncode": "unavailable",
        "state": "multiple" if result_class == "malformed_output" else "command_error",
    }


def _safe_module_bytes(path: Path) -> tuple[bytes, os.stat_result]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ProbeFailure("module_metadata", "oserror") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or before.st_gid != 0
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o022
        or before.st_size > MAX_MODULE_BYTES
    ):
        raise ProbeFailure("module_metadata")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ProbeFailure("module_open", "oserror") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_uid, opened.st_gid, opened.st_nlink, opened.st_mode) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_uid,
            before.st_gid,
            before.st_nlink,
            before.st_mode,
        ):
            raise ProbeFailure("module_identity")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, min(65536, MAX_MODULE_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_MODULE_BYTES:
                raise ProbeFailure("module_size")
        after = os.fstat(fd)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
            after.st_mode,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_uid,
            opened.st_gid,
            opened.st_nlink,
            opened.st_mode,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ):
            raise ProbeFailure("module_changed")
        return b"".join(chunks), opened
    finally:
        os.close(fd)


def _load_pinned_module(expected_sha256: str) -> Any:
    source, metadata = _safe_module_bytes(SYSTEMD_MODULE)
    # The installed release tree is root-owned and immutable. Pin the exact
    # module bytes across import and reject a changed path after loading.
    digest = hashlib.sha256(source).hexdigest()
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None or digest != expected_sha256:
        raise ProbeFailure("systemd_module_identity")
    module = types.ModuleType("_release_systemd_state_pinned")
    module.__file__ = str(SYSTEMD_MODULE)
    module.__verified_sha256__ = digest
    try:
        exec(compile(source, str(SYSTEMD_MODULE), "exec"), module.__dict__)
    except Exception as exc:
        raise ProbeFailure("module_import", type(exc).__name__ if type(exc).__name__ in {"ImportError", "OSError", "SyntaxError"} else "exception") from None
    after, opened = _safe_module_bytes(SYSTEMD_MODULE)
    if (
        hashlib.sha256(after).hexdigest() != digest
        or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
    ):
        raise ProbeFailure("module_changed")
    return module


def _verify_release_source(release: Path, module: Any) -> None:
    path = release / "RELEASE.json"
    try:
        before = path.lstat()
        release_stat = release.lstat()
    except OSError:
        raise ProbeFailure("release_metadata", "oserror") from None
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != release_stat.st_uid
        or before.st_gid != release_stat.st_gid
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o022
        or before.st_size > 64 * 1024
    ):
        raise ProbeFailure("release_metadata")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise ProbeFailure("release_metadata", "oserror") from None
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_uid, opened.st_gid, opened.st_nlink, opened.st_mode) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_uid,
            before.st_gid,
            before.st_nlink,
            before.st_mode,
        ):
            raise ProbeFailure("release_metadata")
        raw = os.read(descriptor, 64 * 1024 + 1)
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_uid, after.st_gid, after.st_nlink, after.st_mode, after.st_mtime_ns, after.st_ctime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_uid, opened.st_gid, opened.st_nlink, opened.st_mode, opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise ProbeFailure("release_metadata")
    finally:
        os.close(descriptor)
    try:
        record = json.loads(raw.decode("ascii"), object_pairs_hook=module._strict_object)
    except Exception:
        raise ProbeFailure("release_metadata") from None
    source_sha = record.get("source_git_commit") if isinstance(record, dict) else None
    if not isinstance(source_sha, str) or re.fullmatch(r"[0-9a-f]{40,64}", source_sha) is None:
        raise ProbeFailure("release_metadata")


def _run(check: str, operation: Any) -> Any:
    try:
        return operation()
    except ProbeFailure:
        raise
    except OSError:
        raise ProbeFailure(check, "oserror") from None
    except Exception as exc:
        kind = type(exc).__name__
        safe_kind = kind if kind in {"StateError", "ValueError", "TypeError", "JSONDecodeError", "TimeoutExpired"} else "exception"
        raise ProbeFailure(check, safe_kind) from None


def _health() -> dict[str, bool]:
    result: dict[str, bool] = {}
    for label, url in HEALTH_URLS:
        try:
            completed = subprocess.run(
                ["/usr/bin/curl", "--disable", "--silent", "--show-error", "--max-time", "5", "--output", "/dev/null", "--write-out", "%{http_code}", url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=7,
                check=False,
                text=True,
                encoding="ascii",
                errors="ignore",
                env={"PATH": "/usr/bin:/bin"},
            )
            result[label] = completed.returncode == 0 and completed.stdout == "200"
        except (OSError, subprocess.SubprocessError):
            result[label] = False
    return result


def probe(expected_systemd_sha256: str) -> dict[str, object]:
    if os.geteuid() != 0:
        raise ProbeFailure("root", "permission")
    try:
        companion = SYSTEMD_RECEIPT.lstat()
        companion_present = True
        if stat.S_ISLNK(companion.st_mode) or not stat.S_ISREG(companion.st_mode):
            raise ProbeFailure("companion_metadata")
    except FileNotFoundError:
        companion_present = False
    except OSError:
        raise ProbeFailure("companion_lstat", "oserror") from None

    module = _load_pinned_module(expected_systemd_sha256)
    if companion_present:
        _run(
            "existing_receipt_validate",
            lambda: module.validate(SYSTEMD_RECEIPT, app_dir=APP_DIR, transaction=TRANSACTION),
        )
        return {
            "schema": 1,
            "kind": "release_capture_probe",
            "status": "passed",
            "capture_predicate": "existing_receipt_valid",
            "health": _health(),
            "receipt_present": True,
            "current_bound": None,
            "previous_bound": None,
            "service_active_count": None,
            "service_enabled_count": None,
        }

    _run("systemctl_path", lambda: module._validate_systemctl_path(module.SYSTEMCTL))
    transaction = _run("transaction_read", lambda: module._read_transaction(TRANSACTION, APP_DIR))
    (_operation_id, operation, current_before, previous_before, _current_identity, _previous_identity, active_overrides) = transaction
    if operation != "install":
        raise ProbeFailure("install_operation")
    if not isinstance(active_overrides, dict) or not active_overrides:
        raise ProbeFailure("active_overrides")

    current_release = Path(current_before)
    if not current_release.is_absolute():
        raise ProbeFailure("current_binding")
    try:
        current_pointer = CURRENT_LINK.lstat()
        current_target = CURRENT_LINK.resolve(strict=True)
        app_metadata = APP_DIR.lstat()
    except OSError:
        raise ProbeFailure("current_binding", "oserror") from None
    if (
        stat.S_ISLNK(current_pointer.st_mode) is False
        or current_pointer.st_uid != app_metadata.st_uid
        or current_pointer.st_gid != app_metadata.st_gid
        or current_target != current_release
    ):
        raise ProbeFailure("current_binding")
    previous_link = APP_DIR / "previous"
    if previous_before:
        try:
            previous_pointer = previous_link.lstat()
            previous_target = previous_link.resolve(strict=True)
        except OSError:
            raise ProbeFailure("previous_binding", "oserror") from None
        if (
            not stat.S_ISLNK(previous_pointer.st_mode)
            or previous_pointer.st_uid != app_metadata.st_uid
            or previous_pointer.st_gid != app_metadata.st_gid
            or previous_target != Path(previous_before)
        ):
            raise ProbeFailure("previous_binding")
    elif os.path.lexists(previous_link):
        raise ProbeFailure("previous_binding")
    _run("release_metadata", lambda: _verify_release_source(current_release, module))
    helper_manifest = _run("helper_digests", lambda: module._helper_digests(current_release))
    if helper_manifest.get("tools/platform_release_systemd_state.py") != module.__verified_sha256__:
        raise ProbeFailure("helper_release_binding")

    # Recheck the no-follow marker after potentially expensive immutable reads.
    try:
        SYSTEMD_RECEIPT.lstat()
    except FileNotFoundError:
        companion_still_absent = True
    except OSError:
        raise ProbeFailure("companion_recheck", "oserror") from None
    else:
        companion_still_absent = False
    if not companion_still_absent:
        raise ProbeFailure("companion_changed")

    capture_context = _run(
        "capture_context",
        lambda: module._release_context(APP_DIR, current_before, previous_before),
    )
    if capture_context is None:
        raise ProbeFailure("capture_context")
    try:
        SYSTEMD_RECEIPT.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise ProbeFailure("receipt_absence", "oserror") from None
    else:
        raise ProbeFailure("receipt_absence")

    failed_query: list[str] = []
    systemd_query: dict[str, object] = {}
    original_active = module._read_active
    original_enabled = module._read_enabled
    original_run_systemctl = getattr(module, "_run_systemctl", None)

    def read_active(unit: str) -> str:
        failed_query[:] = ["active", unit]
        return original_active(unit)

    def read_enabled(unit: str) -> str:
        failed_query[:] = ["enabled", unit]
        return original_enabled(unit)

    def run_systemctl(*args: str) -> tuple[int, str]:
        if (
            len(args) == 2
            and args[0] in {"is-active", "is-enabled"}
            and args[1] in module.OWNED_UNITS
        ):
            try:
                result = original_run_systemctl(*args)
            except Exception as exc:
                systemd_query.clear()
                systemd_query.update(command=args[0], unit=args[1], error=exc)
                raise
            systemd_query.clear()
            systemd_query.update(command=args[0], unit=args[1], result=result)
            return result
        return original_run_systemctl(*args)

    module._read_active = read_active
    module._read_enabled = read_enabled
    if callable(original_run_systemctl):
        module._run_systemctl = run_systemctl
    try:
        unit_snapshot = _run("unit_snapshot", lambda: module._unit_snapshot(active_overrides))
    except ProbeFailure as exc:
        if failed_query:
            exc.check = "unit_" + failed_query[0] + "_" + failed_query[1].replace(".", "_")
            if systemd_query.get("command") == ("is-active" if failed_query[0] == "active" else "is-enabled") and systemd_query.get("unit") == failed_query[1]:
                result = systemd_query.get("result")
                error = systemd_query.get("error")
                exc.systemd_observation = _systemd_failure_observation(
                    str(systemd_query["command"]),
                    str(systemd_query["unit"]),
                    result if isinstance(result, tuple) and len(result) == 2 else None,
                    error if isinstance(error, BaseException) else None,
                )
        raise
    finally:
        module._read_active = original_active
        module._read_enabled = original_enabled
        if callable(original_run_systemctl):
            module._run_systemctl = original_run_systemctl
    capture_result = "prewrite_predicates_passed"
    active_count = sum(item["active"] == "active" for item in unit_snapshot)
    enabled_count = sum(item["enabled"] == "enabled" for item in unit_snapshot)

    return {
        "schema": 1,
        "kind": "release_capture_probe",
        "status": "passed",
        "capture_predicate": capture_result,
        "health": _health(),
        "receipt_present": False,
        "current_bound": True,
        "previous_bound": bool(previous_before),
        "service_active_count": active_count,
        "service_enabled_count": enabled_count,
    }


def main() -> int:
    if len(sys.argv) != 3:
        report: dict[str, object] = {
            "schema": 1,
            "kind": "release_capture_probe",
            "status": "failed",
            "failed_check": "probe_identity",
            "error_class": "input",
            "health": _health(),
        }
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 1
    expected_probe_sha256, expected_systemd_sha256 = sys.argv[1:]
    try:
        probe_source, probe_metadata = _safe_module_bytes(Path(__file__))
        if (
            re.fullmatch(r"[0-9a-f]{64}", expected_probe_sha256) is None
            or hashlib.sha256(probe_source).hexdigest() != expected_probe_sha256
        ):
            raise ProbeFailure("probe_identity")
        if stat.S_IMODE(probe_metadata.st_mode) != 0o600:
            raise ProbeFailure("probe_identity")
        report = probe(expected_systemd_sha256)
    except ProbeFailure as exc:
        report = {
            "schema": 1,
            "kind": "release_capture_probe",
            "status": "failed",
            "failed_check": exc.check,
            "error_class": exc.error_class,
            "health": _health(),
        }
        if exc.systemd_observation is not None:
            report["systemd_observation"] = exc.systemd_observation
    except Exception:
        report = {
            "schema": 1,
            "kind": "release_capture_probe",
            "status": "failed",
            "failed_check": "internal",
            "error_class": "exception",
            "health": _health(),
        }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
