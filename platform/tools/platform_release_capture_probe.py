#!/usr/bin/env python3
"""Read-only diagnosis of the systemd receipt capture preconditions."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
from typing import Any


APP_DIR = Path("/opt/oldsparky/platform")
SHARED_DIR = APP_DIR / "shared"
CURRENT_LINK = APP_DIR / "current"
TRANSACTION = SHARED_DIR / ".release-operation.json"
SYSTEMD_RECEIPT = SHARED_DIR / ".release-systemd-state.json"
SYSTEMD_MODULE = CURRENT_LINK / "tools/platform_release_systemd_state.py"
SHARED_PYTHON = SHARED_DIR / "venv/bin/python"
MAX_STATE_BYTES = 64 * 1024
MAX_PRIVATE_RETAINED_BYTES = 16 * 1024
MAX_PRIVATE_TOTAL_VALUE = 2**63 - 1
MAX_MODULE_BYTES = 2 * 1024 * 1024
MAX_HEALTH_REPORT_BYTES = 4096
MAX_HEALTH_JOURNAL_BYTES = 96 * 1024
MAX_HEALTH_PROPERTY_BYTES = 64
MAX_HEALTH_PROPERTY_VALUE_CHARS = 64
MAX_HEALTH_JOB_ROWS = 8
MAX_HEALTH_CGROUP_BYTES = 4 * 1024
MAX_HEALTH_CGROUP_PIDS = 32
MAX_HEALTH_JOURNAL_ROWS = 4
MAX_HEALTH_JOURNAL_MESSAGE_CHARS = 256
MAX_HEALTH_JOURNAL_MESSAGE_JSON_BYTES = 384
MAX_HEALTH_CHECKER_REPORT_JSON_BYTES = 2048
DIAGNOSTIC_TARGET_SOURCE_SHA = "9ceaf09ab7ba4bf5c5e636865106910eaa034c8d"
DIAGNOSTIC_HEALTH_MONITOR_SHA256 = "2ab138fc827e954acb19ff800395704c67239779165df4e516502ba3749dbb0a"
DIAGNOSTIC_DISK_POLICY_SHA256 = "55b2a91129826d372f977057fe04470650af3af84d6a58b11059e863f40d88f2"
HEALTH_MONITOR_UNIT = "deadlock-health-monitor.service"
HEALTH_MONITOR_TIMER = "deadlock-health-monitor.timer"
HEALTH_CHECK_NAMES = (
    "service:deadlock-api", "service:deadlock-worker", "service:deadlock-web",
    "service:nginx", "api_ready", "disk", "memory", "backup", "certificate",
)
HEALTH_SERVICE_PROPERTIES = (
    "Id", "FragmentPath", "LoadState", "ActiveState", "SubState", "Result",
    "ExecMainCode", "ExecMainStatus", "MainPID", "ControlPID", "ControlGroup",
    "UnitFileState", "ExecMainStartTimestamp", "ExecMainExitTimestamp",
)
HEALTH_TIMER_PROPERTIES = (
    "Id", "FragmentPath", "LoadState", "ActiveState", "SubState", "Result",
    "LastTriggerUSec", "NextElapseUSecRealtime", "UnitFileState",
)
HEALTH_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|token|secret|authorization|cookie|api[_-]?key)"
    r"(\s*[:=]\s*|\s+)([^\s,;]+)"
)
HEALTH_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
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
        safe_fields: dict[str, object] | None = None,
    ) -> None:
        super().__init__(check)
        self.check = check
        self.error_class = error_class
        self.systemd_observation = systemd_observation
        self.safe_fields = safe_fields or {}


class ProbeDeadlineExceeded(Exception):
    pass


class HealthCommandFailure(Exception):
    def __init__(self, returncode: int) -> None:
        super().__init__("read-only health command returned nonzero")
        self.returncode = returncode


_SYSTEMD_STATE_VALUES = frozenset(
    ("active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance", "unknown")
)
_SYSTEMD_ENABLED_VALUES = frozenset(("enabled", "disabled", "static", "indirect", "masked", "generated", "transient", "unknown"))
_DIAGNOSTIC_PHASES = frozenset((
    "none", "module", "systemctl_path", "transaction", "receipt", "binding",
    "unit_active", "unit_enabled", "verify",
))
_DIAGNOSTIC_CHECKS = frozenset((
    "none", "diagnostic_receipt_absent", "systemctl_path", "transaction_read",
    "systemd_module_identity", "module_metadata", "module_open", "module_identity", "module_size", "module_changed", "module_import",
    "existing_receipt_transaction", "runtime_target_binding", "current_binding",
    "previous_binding", "existing_receipt_validate", "release_metadata",
    "helper_digests", "diagnostic_target_binding", "unit_active", "unit_enabled",
    "diagnostic_receipt_units", "unit_snapshot", "systemd_verify",
))
_DIAGNOSTIC_CLASSES = frozenset((
    "none", "predicate", "oserror", "StateError", "ValueError", "TypeError",
    "TimeoutExpired", "timeout", "exception",
))
_DIAGNOSTIC_REASONS = frozenset((
    "none", "binding", "unsupported_state", "status_mismatch", "command_error",
    "malformed_output", "timeout", "receipt_state_invalid", "verify_mismatch",
    "verification_error",
))


def _safe_systemd_value(value: str, command: str) -> str:
    if not value:
        return "empty"
    known = _SYSTEMD_STATE_VALUES if command == "is-active" else _SYSTEMD_ENABLED_VALUES
    return value if value in known else "other"


def _diagnostic_systemd_snapshot(
    module: Any,
    *,
    deadline: float,
    evidence: dict[str, object],
) -> tuple[list[dict[str, object]], bool]:
    """Collect bounded observations through the pinned helper's read-only CLI."""
    units = getattr(module, "OWNED_UNITS", ())
    if not isinstance(units, (tuple, list)) or len(units) != 13:
        evidence.update(failure_phase="binding", failure_check="diagnostic_receipt_units", failure_class="StateError", failure_reason="binding")
        return [], False
    observed: list[dict[str, object]] = []
    for unit in units:
        row: dict[str, object] = {"unit": unit, "active": "unavailable", "enabled": "unavailable"}
        for query, field in (("is-active", "active"), ("is-enabled", "enabled")):
            if evidence["failure_reason"] == "none":
                evidence["failure_phase"] = "unit_active" if field == "active" else "unit_enabled"
                evidence["failure_check"] = "unit_active" if field == "active" else "unit_enabled"
                evidence["failure_unit"] = unit
                evidence["failure_query"] = field
                evidence["failure_rc"] = "unavailable"
            if time.monotonic() >= deadline:
                if evidence["failure_reason"] == "none":
                    evidence.update(
                        failure_phase="unit_active" if field == "active" else "unit_enabled",
                        failure_check="unit_active" if field == "active" else "unit_enabled",
                        failure_class="timeout",
                        failure_reason="timeout",
                        failure_unit=unit,
                        failure_query=field,
                        failure_rc="unavailable",
                    )
                observed.append(row)
                return observed, False
            try:
                returncode, value = module._run_systemctl(query, unit)
            except ProbeDeadlineExceeded:
                raise
            except Exception as exc:
                cause = exc.__cause__
                if evidence["failure_reason"] == "none":
                    malformed = (
                        type(exc).__name__ == "StateError"
                        and str(exc) == "systemd command returned multiple states"
                    )
                    evidence.update(
                        failure_class=("TimeoutExpired" if isinstance(cause, subprocess.TimeoutExpired) else "oserror" if isinstance(cause, OSError) else "StateError" if type(exc).__name__ == "StateError" else "exception"),
                        failure_reason=(
                            "timeout" if isinstance(cause, subprocess.TimeoutExpired)
                            else "malformed_output" if malformed
                            else "command_error"
                        ),
                        failure_unit=unit,
                        failure_query=field,
                        failure_rc="unavailable",
                    )
                observed.append(row)
                evidence["observed_units"] = list(observed)
                return observed, False
            state = _safe_systemd_value(value, query)
            row[field] = state
            supported = {"active", "inactive"} if field == "active" else {"enabled", "disabled", "static"}
            expected_rc = (0 if state == "active" else 3) if field == "active" else (0 if state in {"enabled", "static"} else 1)
            if evidence["failure_reason"] == "none":
                if state not in supported:
                    evidence.update(failure_class="StateError", failure_reason="unsupported_state", failure_unit=unit, failure_query=field, failure_rc=returncode)
                elif returncode != expected_rc:
                    evidence.update(failure_class="StateError", failure_reason="status_mismatch", failure_unit=unit, failure_query=field, failure_rc=returncode)
                else:
                    evidence.update(failure_unit="none", failure_query="none", failure_rc="unavailable")
            evidence["observed_units"] = [*observed, row]
        observed.append(row)
    return observed, True


def _diagnostic_systemd_snapshot_with_deadline(
    module: Any,
    *,
    deadline: float,
    evidence: dict[str, object],
) -> tuple[list[dict[str, object]], bool]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        evidence.update(failure_phase="unit_active", failure_check="unit_active", failure_class="timeout", failure_reason="timeout", failure_rc="unavailable")
        return [], False
    previous_alarm_handler = signal.getsignal(signal.SIGALRM)

    def deadline_expired(_signum: int, _frame: Any) -> None:
        raise ProbeDeadlineExceeded

    signal.signal(signal.SIGALRM, deadline_expired)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        return _diagnostic_systemd_snapshot(module, deadline=deadline, evidence=evidence)
    except ProbeDeadlineExceeded:
        if evidence["failure_reason"] == "none":
            evidence.update(
                failure_phase="unit_active",
                failure_check="unit_active",
                failure_class="timeout",
                failure_reason="timeout",
                failure_rc="unavailable",
            )
        rows = evidence.get("observed_units")
        return (rows if isinstance(rows, list) else []), False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm_handler)


def _health_monitor_initial_evidence() -> dict[str, object]:
    return {
        "schema": 1,
        "collection_completed": False,
        "started_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "finished_at_utc": None,
        "target_source_sha": DIAGNOSTIC_TARGET_SOURCE_SHA,
        "service": {"status": "unavailable", "returncodes": {}, "properties": {}, "failed_property": None},
        "timer": {"status": "unavailable", "returncodes": {}, "properties": {}, "failed_property": None},
        "jobs": {"status": "unavailable", "returncode": "unavailable", "count": 0, "rows": [], "truncated": False},
        "cgroup": {"status": "unavailable", "control_group": "unavailable", "count": 0, "pids": [], "truncated": False},
        "journal": {"status": "unavailable", "returncode": "unavailable", "total_entries": 0, "entries": [], "truncated": False},
        "checker": {"status": "unavailable", "returncode": "unavailable", "report": None},
        "failure_phase": "none",
        "failure_check": "none",
        "failure_class": "none",
    }


def _finalize_skipped_health_monitor(evidence: dict[str, object]) -> None:
    health = evidence.get("health_monitor")
    if not isinstance(health, dict) or health.get("finished_at_utc") is not None:
        return
    if health.get("failure_phase") == "none":
        # A failed outer binding means the target checker was deliberately not
        # run. Record that as unavailable without attributing a host condition.
        health.update(
            failure_phase="binding",
            failure_check="target_release",
            failure_class="exception",
        )
    health["finished_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")


def _health_failure(evidence: dict[str, object], phase: str, check: str, error: BaseException | None = None) -> None:
    if evidence["failure_phase"] != "none":
        return
    class_name = type(error).__name__ if error is not None else "StateError"
    if class_name == "ProbeDeadlineExceeded":
        class_name = "timeout"
    if class_name not in {"OSError", "StateError", "ValueError", "TimeoutExpired", "timeout", "JSONDecodeError"}:
        class_name = "exception"
    evidence.update(failure_phase=phase, failure_check=check, failure_class=class_name)


def _health_command(
    argv: list[str], *, deadline: float, maximum: int, timeout_seconds: float = 15.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProbeDeadlineExceeded
    timeout_seconds = min(timeout_seconds, remaining)
    command_deadline = time.monotonic() + timeout_seconds
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env or {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
        close_fds=True,
        bufsize=0,
    )
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    if any(stream is None for stream in streams.values()):
        _terminate_health_child(process)
        raise OSError("health command streams are unavailable")
    limits = {"stdout": maximum, "stderr": 4096}
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    try:
        for name, stream in streams.items():
            assert stream is not None
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ, name)
        while selector.get_map() or process.poll() is None:
            remaining = min(deadline, command_deadline) - time.monotonic()
            if remaining <= 0:
                _terminate_health_child(process)
                if command_deadline <= deadline:
                    raise subprocess.TimeoutExpired(argv, timeout_seconds)
                raise ProbeDeadlineExceeded
            for key, _ in selector.select(min(remaining, 0.1)):
                try:
                    chunk = os.read(key.fd, 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                name = key.data
                if len(captured[name]) + len(chunk) > limits[name]:
                    _terminate_health_child(process)
                    raise ValueError("bounded health command output exceeded")
                captured[name].extend(chunk)
        returncode = process.wait(timeout=0)
        return subprocess.CompletedProcess(
            argv, returncode, stdout=bytes(captured["stdout"]), stderr=bytes(captured["stderr"]),
        )
    except BaseException:
        if process.poll() is None:
            _terminate_health_child(process)
        raise
    finally:
        selector.close()
        for stream in streams.values():
            if stream is not None:
                stream.close()


def _terminate_health_child(process: subprocess.Popen[bytes]) -> None:
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        # The non-foreground outer GNU timeout owns this process group and is
        # the final hard cap; do not claim cleanup or continue until wait has
        # reaped the direct child.
        process.wait()
    if process.poll() is None:
        raise OSError("health command child was not reaped")


def _health_unit_properties(
    module: Any, unit: str, fields: tuple[str, ...], *, deadline: float,
    evidence: dict[str, object],
) -> None:
    values = evidence["properties"]
    returncodes = evidence["returncodes"]
    assert isinstance(values, dict) and isinstance(returncodes, dict)
    for field in fields:
        evidence["failed_property"] = field
        if time.monotonic() >= deadline:
            raise ProbeDeadlineExceeded
        status, value = module._run_systemctl(
            "show", unit, f"--property={field}", "--value",
        )
        if type(status) is not int or not 0 <= status <= 255:
            raise ValueError("systemd property return code is invalid")
        returncodes[field] = status
        if status != 0 or not value.isascii() or len(value.encode("utf-8", "strict")) > MAX_HEALTH_PROPERTY_BYTES or len(value) > MAX_HEALTH_PROPERTY_VALUE_CHARS or any(ord(char) < 32 for char in value):
            raise ValueError("systemd property query failed or malformed")
        values[field] = value
        evidence["failed_property"] = None


def _health_jobs(module: Any, *, deadline: float) -> dict[str, object]:
    result = _health_command(
        [module.SYSTEMCTL_PATH, "list-jobs", "--no-pager", "--all"],
        deadline=deadline,
        maximum=32 * 1024,
    )
    if result.returncode != 0:
        raise HealthCommandFailure(result.returncode)
    try:
        output = result.stdout.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ValueError("systemd jobs output is malformed") from exc
    if result.stdout == b"No jobs running.\n":
        return {"status": "passed", "returncode": result.returncode, "count": 0, "rows": [], "truncated": False}
    lines = output.splitlines()
    if not lines or lines[0].split() != ["JOB", "UNIT", "TYPE", "STATE"]:
        raise ValueError("systemd jobs header is malformed")
    rows: list[dict[str, str]] = []
    expected_count: int | None = None
    for line in lines[1:]:
        fields = line.split()
        if len(fields) == 3 and fields[1] in {"job", "jobs"} and fields[2] == "listed." and fields[0].isdecimal():
            if expected_count is not None:
                raise ValueError("systemd jobs summary is duplicated")
            expected_count = int(fields[0])
            continue
        if (
            len(fields) != 4 or not fields[0].isdecimal()
            or re.fullmatch(r"[A-Za-z0-9_.@:-]{1,128}", fields[1]) is None
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fields[2]) is None
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fields[3]) is None
        ):
            raise ValueError("systemd jobs row is malformed")
        if len(rows) < MAX_HEALTH_JOB_ROWS:
            rows.append({"unit": fields[1], "type": fields[2], "state": fields[3]})
    if expected_count is None or expected_count != sum(1 for line in lines[1:] if len(line.split()) == 4):
        raise ValueError("systemd jobs summary is malformed")
    return {
        "status": "passed", "returncode": result.returncode,
        "count": expected_count, "rows": rows, "truncated": expected_count > len(rows),
    }


def _health_cgroup(control_group: str) -> dict[str, object]:
    if (
        not isinstance(control_group, str) or not control_group.startswith("/")
        or ".." in Path(control_group).parts or "\x00" in control_group
    ):
        raise ValueError("health monitor cgroup identity is invalid")
    root = Path("/sys/fs/cgroup")
    directory = root / control_group.lstrip("/")
    directory.resolve(strict=True).relative_to(root.resolve(strict=True))
    descriptor = os.open(directory / "cgroup.procs", os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        raw = os.read(descriptor, MAX_HEALTH_CGROUP_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_HEALTH_CGROUP_BYTES:
        raise ValueError("health monitor cgroup output exceeded bound")
    text = raw.decode("ascii", "strict")
    pids: list[int] = []
    total = 0
    for item in text.splitlines():
        if not item.isdecimal() or len(item) > 10:
            raise ValueError("health monitor cgroup output is malformed")
        total += 1
        if len(pids) < MAX_HEALTH_CGROUP_PIDS:
            pids.append(int(item))
    return {
        "status": "passed", "control_group": control_group[:256],
        "count": total, "pids": pids, "truncated": total > len(pids),
    }


def _health_journal(module: Any, *, deadline: float) -> dict[str, object]:
    result = _health_command(
        [
            "/usr/bin/journalctl", "--unit", HEALTH_MONITOR_UNIT,
            "--output=json", "--quiet", "--no-pager", "--lines=100",
        ],
        deadline=deadline,
        maximum=MAX_HEALTH_JOURNAL_BYTES,
    )
    if result.returncode != 0:
        raise HealthCommandFailure(result.returncode)
    entries: list[dict[str, str]] = []
    entry_truncated: list[bool] = []
    try:
        lines = result.stdout.splitlines()
        if len(lines) > 100:
            raise ValueError("health monitor journal row count exceeded")
        for line in lines:
            item = json.loads(line.decode("utf-8", "strict"))
            if type(item) is not dict:
                raise ValueError("health monitor journal row is malformed")
            message = str(item.get("MESSAGE", ""))
            message = HEALTH_BEARER_PATTERN.sub("Bearer [REDACTED]", HEALTH_SECRET_PATTERN.sub(r"\1\2[REDACTED]", message))
            retained_message = ""
            for character in message[:MAX_HEALTH_JOURNAL_MESSAGE_CHARS]:
                candidate = retained_message + character
                encoded_candidate = json.dumps(candidate, ensure_ascii=True, separators=(",", ":")).encode("ascii")
                if len(encoded_candidate) > MAX_HEALTH_JOURNAL_MESSAGE_JSON_BYTES:
                    break
                retained_message = candidate
            entries.append({
                "timestamp": str(item.get("__REALTIME_TIMESTAMP", ""))[:24],
                "priority": str(item.get("PRIORITY", ""))[:2],
                "message": retained_message,
            })
            entry_truncated.append(retained_message != message)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("health monitor journal output is malformed") from exc
    total = len(entries)
    retained = entries[-MAX_HEALTH_JOURNAL_ROWS:]
    retained_truncated = entry_truncated[-MAX_HEALTH_JOURNAL_ROWS:]
    return {
        "status": "passed", "returncode": result.returncode,
        "total_entries": total, "entries": retained,
        "truncated": total > len(retained) or any(retained_truncated),
    }


def _health_checker_report(target: Path, *, deadline: float) -> dict[str, object]:
    checker = target / "tools/platform_health_monitor.py"
    disk_policy = target / "tools/platform_disk_policy.py"
    checker_bytes, _checker_metadata = _safe_module_bytes(checker)
    disk_bytes, _disk_metadata = _safe_module_bytes(disk_policy)
    if hashlib.sha256(checker_bytes).hexdigest() != DIAGNOSTIC_HEALTH_MONITOR_SHA256:
        raise ProbeFailure("health_checker_identity")
    if hashlib.sha256(disk_bytes).hexdigest() != DIAGNOSTIC_DISK_POLICY_SHA256:
        raise ProbeFailure("health_disk_policy_identity")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProbeDeadlineExceeded
    try:
        result = _health_command(
            [
                str(SHARED_PYTHON), "-B", str(checker),
                "--disk-min-free-gib", "5", "--disk-max-used-percent", "85",
            ],
            deadline=deadline,
            maximum=MAX_HEALTH_REPORT_BYTES,
            timeout_seconds=90,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired:
        raise
    if len(result.stdout) > MAX_HEALTH_REPORT_BYTES or result.returncode not in {0, 1}:
        raise ValueError("health monitor report exceeded its contract")
    try:
        report = json.loads(result.stdout.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("health monitor report is malformed") from exc
    if (
        type(report) is not dict or set(report) != {"event", "timestamp", "ok", "failed", "checks"}
        or report.get("event") != "platform_health_monitor"
        or not isinstance(report.get("timestamp"), str) or len(report["timestamp"]) > 64
        or type(report.get("ok")) is not bool
        or type(report.get("failed")) is not list
        or type(report.get("checks")) is not list
        or [row.get("name") if type(row) is dict else None for row in report["checks"]] != list(HEALTH_CHECK_NAMES)
        or report["failed"] != [row["name"] for row in report["checks"] if type(row) is dict and not row.get("ok")]
        or result.returncode != (0 if report["ok"] else 1)
        or report["ok"] != (not report["failed"])
    ):
        raise ValueError("health monitor report fields are invalid")
    for row in report["checks"]:
        if type(row) is not dict or set(row) != {"name", "ok", "detail"} or type(row.get("name")) is not str or type(row.get("ok")) is not bool or type(row.get("detail")) is not dict:
            raise ValueError("health monitor check report is invalid")
    if len(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")) > MAX_HEALTH_CHECKER_REPORT_JSON_BYTES:
        raise ValueError("health monitor report detail exceeded bound")
    return {"status": "reported", "returncode": result.returncode, "report": report}


def _collect_health_monitor_evidence(
    module: Any, target: Path | None, *, deadline: float,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    evidence = evidence if evidence is not None else _health_monitor_initial_evidence()
    if (
        target is None or target.parent != APP_DIR / "releases"
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}", target.name) is None
    ):
        _health_failure(evidence, "binding", "target_release")
        evidence["finished_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
        return evidence
    unit_rows = (
        ("service", HEALTH_MONITOR_UNIT, HEALTH_SERVICE_PROPERTIES),
        ("timer", HEALTH_MONITOR_TIMER, HEALTH_TIMER_PROPERTIES),
    )
    for label, unit, fields in unit_rows:
        unit_evidence = evidence[label]
        assert isinstance(unit_evidence, dict)
        try:
            _health_unit_properties(module, unit, fields, deadline=deadline, evidence=unit_evidence)
            unit_evidence["status"] = "passed"
        except ProbeDeadlineExceeded as exc:
            _health_failure(evidence, "properties", label, exc)
            break
        except Exception as exc:
            _health_failure(evidence, "properties", label, exc)
            unit_evidence["status"] = "unavailable"
            break
    service = evidence["service"]
    control_group = service.get("properties", {}).get("ControlGroup", "") if isinstance(service, dict) else ""
    if isinstance(control_group, str) and control_group:
        try:
            evidence["cgroup"] = _health_cgroup(control_group)
        except ProbeDeadlineExceeded:
            raise
        except Exception as exc:
            _health_failure(evidence, "cgroup", "health_cgroup", exc)
    else:
        _health_failure(evidence, "cgroup", "health_cgroup")
    try:
        evidence["jobs"] = _health_jobs(module, deadline=deadline)
    except HealthCommandFailure as exc:
        _health_failure(evidence, "jobs", "health_jobs", ValueError("systemd jobs query failed"))
        jobs = evidence["jobs"]
        assert isinstance(jobs, dict)
        jobs["returncode"] = exc.returncode
    except ProbeDeadlineExceeded:
        raise
    except Exception as exc:
        _health_failure(evidence, "jobs", "health_jobs", exc)
    try:
        evidence["journal"] = _health_journal(module, deadline=deadline)
    except HealthCommandFailure as exc:
        _health_failure(evidence, "journal", "health_journal", ValueError("health monitor journal query failed"))
        journal = evidence["journal"]
        assert isinstance(journal, dict)
        journal["returncode"] = exc.returncode
    except ProbeDeadlineExceeded:
        raise
    except Exception as exc:
        _health_failure(evidence, "journal", "health_journal", exc)
    try:
        evidence["checker"] = _health_checker_report(target, deadline=deadline)
    except ProbeDeadlineExceeded:
        raise
    except Exception as exc:
        _health_failure(evidence, "checker", "health_checker", exc)
    evidence["collection_completed"] = all(
        isinstance(evidence[key], dict) and evidence[key].get("status") in {"passed", "reported"}
        for key in ("service", "timer", "jobs", "cgroup", "journal", "checker")
    )
    evidence["finished_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    return evidence


def _collect_health_monitor_evidence_with_deadline(
    module: Any, target: Path | None, *, deadline: float,
) -> dict[str, object]:
    evidence = _health_monitor_initial_evidence()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _health_failure(evidence, "deadline", "health_monitor", ProbeDeadlineExceeded())
        evidence["finished_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
        return evidence
    previous_alarm_handler = signal.getsignal(signal.SIGALRM)

    def deadline_expired(_signum: int, _frame: Any) -> None:
        raise ProbeDeadlineExceeded

    signal.signal(signal.SIGALRM, deadline_expired)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        return _collect_health_monitor_evidence(module, target, deadline=deadline, evidence=evidence)
    except ProbeDeadlineExceeded as exc:
        _health_failure(evidence, "deadline", "health_monitor", exc)
        evidence["finished_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
        return evidence
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm_handler)


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


def _load_pinned_runtime_installer(path: Path, expected_sha256: str) -> Any:
    source, metadata = _safe_module_bytes(path)
    digest = hashlib.sha256(source).hexdigest()
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None or digest != expected_sha256:
        raise ProbeFailure("runtime_installer_identity")
    module = types.ModuleType("_release_liveqa_runtime_installer_pinned")
    module.__file__ = str(path)
    module.__verified_sha256__ = digest
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    except Exception as exc:
        safe_class = type(exc).__name__
        raise ProbeFailure(
            "runtime_installer_import",
            safe_class if safe_class in {"ImportError", "OSError", "SyntaxError"} else "exception",
        ) from None
    after, opened = _safe_module_bytes(path)
    if (
        hashlib.sha256(after).hexdigest() != digest
        or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
    ):
        raise ProbeFailure("runtime_installer_changed")
    return module


def _expected_liveqa_payload_tree(
    module: Any,
    *,
    release: Path,
    source_sha: str,
    runtime_provider: dict[str, object] | None,
    deadline: float,
    runtime_manifest: dict[str, object] | None = None,
) -> tuple[str, dict[str, str]]:
    """Recompute install's deterministic payload tree from its pinned inputs."""

    files: dict[str, str] = {}
    directories: set[str] = {"platform", "platform/tools", "runtime"}
    total_bytes = 0

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise ProbeFailure("runtime_expected_tree", "timeout")

    def add_file(relative: str, source: Path, *, sandbox: bool = False) -> None:
        nonlocal total_bytes
        check_deadline()
        metadata = module._regular(source, allow_sandbox=sandbox)
        total_bytes += metadata.st_size
        if len(files) >= module.MAX_FILES or total_bytes > module.MAX_RUNTIME_BYTES:
            raise ProbeFailure("runtime_expected_tree", "bound")
        files[relative] = (
            module._sandbox_digest(source)
            if sandbox
            else module._digest_regular(source)
        )
        parent = PurePosixPath(relative).parent
        while str(parent) != ".":
            directories.add(parent.as_posix())
            parent = parent.parent

    def add_tree(source: Path, destination: str) -> None:
        check_deadline()
        module._validate_source_tree(source.parent, source.name)
        directories.add(destination)
        cache_policy = getattr(module, "SOURCE_TREE_CACHE_POLICY", None)
        if cache_policy is not None and cache_policy != "paired-python-bytecode-v1":
            raise ProbeFailure("runtime_expected_tree", "cache_policy")
        source_paths = tuple(source.rglob("*"))
        source_files = {
            path.relative_to(source).as_posix(): path
            for path in source_paths
            if stat.S_ISREG(module._metadata(path).st_mode)
        }
        for path in source_paths:
            check_deadline()
            relative = path.relative_to(source).as_posix()
            target_relative = f"{destination}/{relative}"
            metadata = module._metadata(path)
            if cache_policy == "paired-python-bytecode-v1" and "__pycache__" in PurePosixPath(relative).parts:
                parts = PurePosixPath(relative).parts
                if parts.count("__pycache__") != 1:
                    raise ProbeFailure("runtime_expected_tree", "cache_policy")
                if stat.S_ISDIR(metadata.st_mode) and parts[-1] == "__pycache__":
                    continue
                paired_source = module._source_tree_cache_relative(relative)
                if (
                    paired_source is None
                    or paired_source not in source_files
                    or not stat.S_ISREG(module._metadata(source_files[paired_source]).st_mode)
                ):
                    raise ProbeFailure("runtime_expected_tree", "cache_policy")
                continue
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(target_relative)
                continue
            add_file(target_relative, path)

    if module.SHA_PATTERN.fullmatch(source_sha) is None:
        raise ProbeFailure("runtime_target_binding")
    for tool in module.TOOL_FILES:
        add_file(f"platform/tools/{tool}", release / "tools" / tool)
    for tree in module.SOURCE_TREES:
        add_tree(release / tree, f"platform/{tree}")
    for relative in module.SOURCE_FILES:
        add_file(f"platform/{relative}", release / relative)

    runtime_source = release / "liveqa-runtime"
    if runtime_manifest is None:
        runtime_manifest = module._validate_runtime_source(
            runtime_source,
            source_sha=source_sha,
        )
    runtime_version = runtime_manifest.get("version")
    if runtime_version not in {1, 2}:
        raise ProbeFailure("runtime_expected_tree", "manifest")
    for path in runtime_source.rglob("*"):
        check_deadline()
        relative = path.relative_to(runtime_source).as_posix()
        target_relative = f"runtime/{relative}"
        metadata = module._metadata(path)
        if stat.S_ISDIR(metadata.st_mode):
            directories.add(target_relative)
            continue
        add_file(
            target_relative,
            path,
            sandbox=(
                runtime_version == 1
                and relative == module.RUNTIME_SANDBOX_RELATIVE.as_posix()
            ),
        )

    if runtime_version == 2:
        if runtime_provider is None:
            raise ProbeFailure("runtime_expected_tree", "provider")
        provider_record = {**runtime_provider, "source_sha": source_sha}
        provider_raw = (
            json.dumps(
                provider_record,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
        files["runtime-provider.json"] = hashlib.sha256(provider_raw).hexdigest()
        total_bytes += len(provider_raw)
    elif runtime_provider is not None:
        raise ProbeFailure("runtime_expected_tree", "provider")

    source_raw = (source_sha + "\n").encode("ascii")
    files["source-sha"] = hashlib.sha256(source_raw).hexdigest()
    total_bytes += len(source_raw)
    if len(files) > module.MAX_FILES or total_bytes > module.MAX_RUNTIME_BYTES:
        raise ProbeFailure("runtime_expected_tree", "bound")

    digest = hashlib.sha256()
    for relative in sorted(files.keys() | directories, key=module._runtime_path_order_key):
        check_deadline()
        digest.update(relative.encode("utf-8") + b"\0")
        if relative in directories:
            digest.update(b"d\0")
        else:
            digest.update(b"f\0" + bytes.fromhex(files[relative]))
    return digest.hexdigest(), files


def _private_runtime_detail(component: str, detail: dict[str, object]) -> None:
    raw = json.dumps(detail, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    stream = tempfile.TemporaryFile(mode="w+b")
    try:
        stream.write(raw)
        stream.flush()
        _emit_private_runtime_context(
            stream,
            component=component,
            returncode=1,
            total_bytes=len(raw),
            position=len(raw),
            retained_bytes=len(raw),
        )
    finally:
        stream.close()


def _private_runtime_report(
    component: str,
    detail: dict[str, object],
    *,
    returncode: int,
) -> None:
    raw = json.dumps(detail, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(raw) > MAX_PRIVATE_RETAINED_BYTES:
        raise ProbeFailure("private_report_size")
    stream = tempfile.TemporaryFile(mode="w+b")
    try:
        metadata = os.fstat(stream.fileno())
        if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != _private_report_owner_uid():
            raise ProbeFailure("private_report_metadata")
        stream.write(raw)
        stream.flush()
        _emit_private_runtime_context(
            stream,
            component=component,
            returncode=returncode,
            total_bytes=len(raw),
            position=len(raw),
            retained_bytes=len(raw),
        )
    finally:
        stream.close()


def _unit_snapshot_with_deadline(
    module: Any,
    *,
    deadline: float,
    result: dict[str, object],
) -> list[dict[str, object]]:
    remaining_seconds = deadline - time.monotonic()
    if remaining_seconds <= 0:
        raise ProbeFailure("unit_snapshot", "timeout")
    previous_alarm_handler = signal.getsignal(signal.SIGALRM)

    def deadline_expired(_signum: int, _frame: Any) -> None:
        raise ProbeDeadlineExceeded

    signal.signal(signal.SIGALRM, deadline_expired)
    signal.setitimer(signal.ITIMER_REAL, remaining_seconds)
    try:
        return _run("unit_snapshot", lambda: module._unit_snapshot(None))
    except ProbeDeadlineExceeded:
        if _private_capture_requested():
            _private_runtime_detail(
                "systemd_unit_snapshot",
                {"schema": 1, "reason": "read_only_unit_snapshot_deadline"},
            )
        raise ProbeFailure("unit_snapshot", "timeout") from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm_handler)


def _compare_expected_payload(
    expected_digest: str,
    expected_files: dict[str, str],
    manifest: dict[str, object],
) -> tuple[bool, dict[str, object]]:
    actual_files = manifest.get("files")
    if not isinstance(actual_files, dict):
        raise ProbeFailure("runtime_expected_tree", "manifest")
    missing = sorted(set(expected_files) - set(actual_files))
    extra = sorted(set(actual_files) - set(expected_files))
    changed = sorted(
        relative
        for relative in expected_files.keys() & actual_files.keys()
        if expected_files[relative] != actual_files[relative]
    )
    actual_digest = manifest.get("payload_tree_sha256")
    matches = not missing and not extra and not changed and actual_digest == expected_digest
    evidence = {
        "runtime_payload_verify": "passed",
        "runtime_payload_verify_returncode": 0,
        "runtime_expected_tree": "passed" if matches else "failed",
        "runtime_expected_tree_returncode": 0 if matches else 1,
    }
    if matches:
        return True, evidence
    detail: dict[str, object] = {
        "schema": 1,
        "reason": "expected_tree_mismatch",
        "expected_tree_sha256": expected_digest,
        "actual_tree_sha256": actual_digest if isinstance(actual_digest, str) else "unavailable",
        "expected_file_count": len(expected_files),
        "actual_file_count": len(actual_files),
        "missing_count": len(missing),
        "extra_count": len(extra),
        "extra": [item[:256] for item in extra[:8]],
        "changed_count": len(changed),
        "changed": [item[:256] for item in changed[:8]],
    }
    # Preserve the complete missing set when it fits the existing encrypted
    # component budget. Never imply that a partial path list is exhaustive.
    retained: list[str] = []
    for relative in missing[:64]:
        if len(relative) > 512:
            break
        candidate = [*retained, relative]
        detail["missing"] = candidate
        if len(json.dumps(detail, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")) > 15 * 1024:
            detail["missing"] = retained
            break
        retained = candidate
    detail["missing"] = retained
    detail["missing_paths_truncated"] = len(retained) != len(missing)
    evidence["private_detail"] = detail
    return False, evidence


def _all_missing_python_bytecode(
    missing: list[str],
    expected_files: dict[str, str],
    *,
    source_sha: str,
) -> bool:
    """Prove every missing expected file is paired bytecode in the bound tree."""
    if re.fullmatch(r"[0-9a-f]{40,64}", source_sha) is None:
        return False
    if not missing or len(missing) > 64:
        return False
    for relative in missing:
        path = PurePosixPath(relative)
        parts = path.parts
        if "__pycache__" not in parts or not relative.endswith(".pyc"):
            return False
        cache_index = parts.index("__pycache__")
        if cache_index == 0 or cache_index != len(parts) - 2:
            return False
        filename = parts[-1]
        stem = filename[:-4]
        tagged = False
        for marker in (".cpython-", ".pypy-"):
            if marker in stem:
                stem = stem.split(marker, 1)[0]
                tagged = True
                break
        if not tagged and (stem.endswith(".opt-1") or stem.endswith(".opt-2")):
            stem = stem.rsplit(".opt-", 1)[0]
        source_name = "__init__.py" if stem == "__init__" else f"{stem}.py"
        source_relative = PurePosixPath(*parts[:cache_index], source_name).as_posix()
        digest = expected_files.get(source_relative)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return False
    return True


def _runtime_payload_integrity(
    target: Path,
    *,
    source_sha: str,
    expected_helper_sha256: str,
    deadline: float,
) -> tuple[dict[str, object], str | None, dict[str, object] | None]:
    helper_path = target / "tools/platform_live_qa_runtime_install.py"
    installer = _load_pinned_runtime_installer(helper_path, expected_helper_sha256)
    release, active_source, release_slug = installer._active_release(APP_DIR)
    if release != target or active_source != source_sha:
        raise ProbeFailure("runtime_target_binding")
    manifest = installer._read_manifest()
    if (
        manifest.get("source_sha") != source_sha
        or manifest.get("release_slug") != release_slug
    ):
        raise ProbeFailure("runtime_target_binding")
    runtime_manifest = installer._validate_runtime_source(
        target / "liveqa-runtime",
        source_sha=source_sha,
    )
    provider: dict[str, object] | None = None
    if runtime_manifest.get("version") == 2:
        provider = installer._runtime_provider_payload(
            APP_DIR,
            expected_manifest=runtime_manifest,
        )
    else:
        # For v1, verify() is the canonical read-only path that binds the
        # active manifest, payload tree and all trusted entrypoints.
        manifest = installer.verify(APP_DIR, source_sha)
    if time.monotonic() >= deadline:
        raise ProbeFailure("runtime_expected_tree", "timeout")
    expected_digest, expected_files = _expected_liveqa_payload_tree(
        installer,
        release=target,
        source_sha=source_sha,
        runtime_provider=provider,
        runtime_manifest=runtime_manifest,
        deadline=deadline,
    )
    matches, evidence = _compare_expected_payload(
        expected_digest,
        expected_files,
        manifest,
    )
    if not matches:
        detail = evidence.pop("private_detail")
        if (
            isinstance(detail, dict)
            and detail.get("missing_count", 0) > 0
            and detail.get("missing_paths_truncated") is False
        ):
            missing = detail.get("missing")
            if isinstance(missing, list) and len(missing) == detail.get("missing_count"):
                detail["all_missing_python_bytecode"] = _all_missing_python_bytecode(
                    missing,
                    expected_files,
                    source_sha=source_sha,
                )
                detail["missing_paths_source_sha"] = source_sha
        return evidence, "runtime_expected_tree", detail
    return evidence, None, None


def _verify_release_source(release: Path, module: Any) -> str:
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
    return source_sha


def _read_stable_state(path: Path, check: str) -> dict[str, object]:
    """Read one small root-owned receipt without following or trusting a path race."""
    try:
        before = path.lstat()
    except OSError:
        raise ProbeFailure(check, "oserror") from None
    trusted_uid, trusted_gid = _trusted_state_owner()
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != trusted_uid
        or before.st_gid != trusted_gid
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size > MAX_STATE_BYTES
    ):
        raise ProbeFailure(check)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise ProbeFailure(check, "oserror") from None
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_uid, opened.st_gid, opened.st_nlink, opened.st_mode) != (
            before.st_dev, before.st_ino, before.st_size, before.st_uid, before.st_gid, before.st_nlink, before.st_mode
        ):
            raise ProbeFailure(check)
        raw = os.read(descriptor, MAX_STATE_BYTES + 1)
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or len(raw) > MAX_STATE_BYTES
            or (after.st_dev, after.st_ino, after.st_size, after.st_uid, after.st_gid, after.st_nlink, after.st_mode, after.st_mtime_ns, after.st_ctime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_uid, opened.st_gid, opened.st_nlink, opened.st_mode, opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise ProbeFailure(check)
    finally:
        os.close(descriptor)
    try:
        record = json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object)
    except Exception:
        raise ProbeFailure(check) from None
    if not isinstance(record, dict):
        raise ProbeFailure(check)
    return record


def _trusted_state_owner() -> tuple[int, int]:
    return 0, 0


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _receipt_runtime_target(transaction: dict[str, object]) -> tuple[str, str | None, str]:
    operation = transaction.get("operation")
    phase = transaction.get("phase")
    current = transaction.get("current_before")
    previous = transaction.get("previous_before")
    if not isinstance(operation, str) or operation not in {"install", "rollback"} or not isinstance(phase, str):
        raise ProbeFailure("existing_receipt_transaction")
    if not isinstance(current, str) or not isinstance(previous, str) or not current or not previous:
        raise ProbeFailure("existing_receipt_transaction")
    # The canonical recovery script also invokes the signed runtime restore for
    # install transactions with both before-pointers, including
    # recovery-restored.  The companion receipt remains operation=rollback and
    # is joined by operation id and pointer identities, not this transaction
    # operation field.
    if operation == "install":
        return "current_before", current, "current_before"
    if phase in {"rollback-runtime-pending", "services-restarted", "smoke-passed", "filesystem-restored-runtime-pending"}:
        return "current_before", current, "current_before"
    if phase == "rollback-cache-purged":
        return "previous_before", previous, "previous_before"
    if phase == "rollback-runtime-applied":
        return "none", None, "previous_before"
    if phase == "recovery-restored":
        return "none", None, "current_before"
    raise ProbeFailure("existing_receipt_phase")


def _bind_release_pointer(link: Path, expected: str, app_metadata: os.stat_result, check: str) -> Path:
    try:
        pointer = link.lstat()
        target = link.resolve(strict=True)
    except OSError:
        raise ProbeFailure(check, "oserror") from None
    if (
        not stat.S_ISLNK(pointer.st_mode)
        or pointer.st_uid != app_metadata.st_uid
        or pointer.st_gid != app_metadata.st_gid
        or target != Path(expected)
    ):
        raise ProbeFailure(check)
    return target


def _private_capture_requested() -> bool:
    return (
        os.environ.get("PLATFORM_RUNTIME_PRIVATE_CAPTURE") == "1"
        and os.environ.get("PLATFORM_RUNTIME_PRIVATE_ROUTE") == "capture_probe"
    )


def _private_report_owner_uid() -> int:
    return os.geteuid()


def _emit_private_runtime_context(
    stream: Any,
    *,
    route: str = "capture_probe",
    component: str,
    returncode: int | str,
    total_bytes: int | str,
    position: int,
    retained_bytes: int,
) -> None:
    if retained_bytes < MAX_PRIVATE_RETAINED_BYTES:
        stream.seek(0)
        retained = stream.read(retained_bytes)
    else:
        stream.seek(position)
        retained = stream.read(MAX_PRIVATE_RETAINED_BYTES - position)
        stream.seek(0)
        retained += stream.read(position)
    record = {
        "schema": 1,
        "route": route,
        "component": component,
        "guard_id": None,
        "returncode": returncode if type(returncode) is int and 0 <= returncode <= 255 else "unavailable",
        "stream": "stderr" if route == "capture_probe" else "combined",
        "output_b64": base64.b64encode(retained).decode("ascii"),
        "total_bytes": total_bytes if type(total_bytes) is int and 0 <= total_bytes <= MAX_PRIVATE_TOTAL_VALUE else "unavailable",
        "truncated": total_bytes == "unavailable" or (type(total_bytes) is int and total_bytes > len(retained)),
        "retained": "tail",
    }
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    sys.stderr.buffer.write(("RUNTIME_PRIVATE_CONTEXT " + payload + "\n").encode("ascii"))
    sys.stderr.buffer.flush()


def _append_private_tail(stream: Any, position: int, retained_bytes: int, chunk: bytes) -> tuple[int, int]:
    if len(chunk) >= MAX_PRIVATE_RETAINED_BYTES:
        stream.seek(0)
        stream.write(chunk[-MAX_PRIVATE_RETAINED_BYTES:])
        return 0, MAX_PRIVATE_RETAINED_BYTES
    first = min(len(chunk), MAX_PRIVATE_RETAINED_BYTES - position)
    stream.seek(position)
    stream.write(chunk[:first])
    if first < len(chunk):
        stream.seek(0)
        stream.write(chunk[first:])
    return (position + len(chunk)) % MAX_PRIVATE_RETAINED_BYTES, min(
        MAX_PRIVATE_RETAINED_BYTES, retained_bytes + len(chunk)
    )


def _run_private_preflight(
    argv: list[str],
    env: dict[str, str],
    *,
    timeout_seconds: int = 60,
) -> tuple[int | str, int, Any, int, int, bool]:
    stream = tempfile.TemporaryFile(mode="w+b")
    metadata = os.fstat(stream.fileno())
    if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid():
        stream.close()
        raise OSError("private capture temp metadata is unsafe")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=0,
        )
    except Exception:
        stream.close()
        raise
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stderr, selectors.EVENT_READ)
    position = 0
    retained_bytes = 0
    total_bytes = 0
    timed_out = False
    deadline = time.monotonic() + timeout_seconds
    drain_deadline: float | None = None
    try:
        while selector.get_map():
            now = time.monotonic()
            if not timed_out and now >= deadline:
                timed_out = True
                drain_deadline = now + 1
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            if timed_out and drain_deadline is not None and now >= drain_deadline:
                break
            timeout = min(0.25, max(0.0, (drain_deadline if timed_out else deadline) - now))
            ready = selector.select(timeout)
            if not ready:
                if timed_out:
                    break
                continue
            for key, _ in ready:
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total_bytes += len(chunk)
                position, retained_bytes = _append_private_tail(stream, position, retained_bytes, chunk)
        if timed_out:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            stream.flush()
            return "unavailable", "unavailable", stream, position, retained_bytes, True
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        stream.flush()
        return returncode, total_bytes, stream, position, retained_bytes, False
    except Exception:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        stream.close()
        raise
    finally:
        selector.close()
        process.stderr.close()


def _receipt_snapshot_and_preflight(
    module: Any,
    transaction: tuple[object, ...],
    *,
    deadline: float,
    diagnostic_evidence: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object], str, str | None]:
    readonly_deadline = deadline
    (operation_id, operation, current_before, previous_before, _current_identity, _previous_identity, _active_overrides) = transaction
    transaction_record = _read_stable_state(TRANSACTION, "transaction_read")
    if (
        transaction_record.get("operation_id") != operation_id
        or transaction_record.get("operation") != operation
        or transaction_record.get("current_before") != current_before
        or transaction_record.get("previous_before") != previous_before
    ):
        raise ProbeFailure("existing_receipt_transaction")
    target_label, target_path, validation_label = _receipt_runtime_target(transaction_record)
    validation_path = transaction_record.get(validation_label)
    if not isinstance(validation_path, str) or not validation_path:
        raise ProbeFailure("existing_receipt_transaction")
    validation_release = Path(validation_path)
    target = Path(target_path) if target_path is not None else None

    def collect_health_monitor() -> None:
        if diagnostic_evidence is not None:
            diagnostic_evidence["health_monitor"] = _collect_health_monitor_evidence(
                module, target, deadline=readonly_deadline,
            )

    try:
        app_metadata = APP_DIR.lstat()
    except OSError:
        raise ProbeFailure("current_binding", "oserror") from None
    releases = APP_DIR / "releases"
    if validation_release.parent != releases or (target is not None and target.parent != releases):
        raise ProbeFailure("runtime_target_binding")
    _bind_release_pointer(CURRENT_LINK, str(current_before), app_metadata, "current_binding")
    _bind_release_pointer(APP_DIR / "previous", str(previous_before), app_metadata, "previous_binding")
    record = _run(
        "existing_receipt_validate",
        lambda: module._read_receipt(SYSTEMD_RECEIPT),
    )
    try:
        _run(
            "existing_receipt_validate",
            lambda: module.validate(
                SYSTEMD_RECEIPT,
                app_dir=APP_DIR,
                transaction=TRANSACTION,
                helper_release=validation_release,
            ),
        )
    except ProbeFailure:
        if diagnostic_evidence is not None:
            diagnostic_evidence["validate_status"] = "failed"
        raise
    if diagnostic_evidence is not None:
        diagnostic_evidence["validate_status"] = "passed"
        diagnostic_evidence["failure_phase"] = "binding"
        diagnostic_evidence["failure_check"] = "none"
    if (
        record.get("operation") != "rollback"
        or record.get("operation_id") != operation_id
        or record.get("current_before") != current_before
        or record.get("previous_before") != previous_before
        or record.get("app_dir") != str(APP_DIR)
    ):
        raise ProbeFailure("existing_receipt_transaction")
    manifests = record.get("helper_digests")
    if not isinstance(manifests, dict) or validation_label not in manifests:
        raise ProbeFailure("runtime_target_binding")
    manifest = manifests[validation_label]
    if not isinstance(manifest, dict):
        raise ProbeFailure("runtime_target_binding")
    source_sha = _run("release_metadata", lambda: _verify_release_source(validation_release, module))
    if diagnostic_evidence is not None:
        phase = transaction_record.get("phase")
        rollback_runtime_phase = operation == "rollback" and phase in {
            "rollback-runtime-pending",
            "services-restarted",
            "smoke-passed",
            "filesystem-restored-runtime-pending",
        }
        restored_install_phase = operation == "install" and phase == "recovery-restored"
        if (
            not (rollback_runtime_phase or restored_install_phase)
            or target_label != "current_before"
            or source_sha != DIAGNOSTIC_TARGET_SOURCE_SHA
        ):
            raise ProbeFailure("diagnostic_target_binding")
    actual_helpers = _run("helper_digests", lambda: module._helper_digests(validation_release))
    nginx_digest = actual_helpers.get("tools/platform_install_nginx.py") if isinstance(actual_helpers, dict) else None
    if not isinstance(nginx_digest, str) or manifest.get("tools/platform_install_nginx.py") != nginx_digest:
        raise ProbeFailure("runtime_target_binding")
    state_helper_relative = "tools/platform_release_systemd_state.py"
    state_helper_digest = (
        actual_helpers.get(state_helper_relative)
        if isinstance(actual_helpers, dict)
        else None
    )
    if diagnostic_evidence is not None and (
        not isinstance(state_helper_digest, str)
        or state_helper_digest != module.__verified_sha256__
        or manifest.get(state_helper_relative) != state_helper_digest
    ):
        raise ProbeFailure("diagnostic_target_binding")
    runtime_installer_relative = "tools/platform_live_qa_runtime_install.py"
    runtime_installer_digest = (
        actual_helpers.get(runtime_installer_relative)
        if isinstance(actual_helpers, dict)
        else None
    )
    if target is not None and (
        validation_release != target
        or not isinstance(runtime_installer_digest, str)
        or manifest.get(runtime_installer_relative) != runtime_installer_digest
    ):
        raise ProbeFailure("runtime_target_binding")
    result: dict[str, object] = {
        "capture_predicate": "existing_receipt_valid",
        "receipt_present": True,
        "current_bound": True,
        "previous_bound": True,
        "runtime_target": target.name if target is not None else None,
        "runtime_target_source": source_sha if target is not None else None,
        "runtime_target_helper_sha256": nginx_digest if target is not None else None,
        "runtime_payload_verify": "not_applicable",
        "runtime_payload_verify_returncode": "unavailable",
        "runtime_expected_tree": "not_applicable",
        "runtime_expected_tree_returncode": "unavailable",
        "nginx_preflight": "not_applicable",
        "nginx_preflight_returncode": "unavailable",
    }
    units = getattr(module, "OWNED_UNITS", ())
    if diagnostic_evidence is not None:
        expected_units = record.get("units")
        if (
            not isinstance(expected_units, list)
            or len(expected_units) != 13
            or any(
                not isinstance(item, dict)
                or set(item) != {"name", "active", "enabled"}
                or item.get("name") != unit
                or item.get("active") not in _SYSTEMD_STATE_VALUES
                or item.get("enabled") not in _SYSTEMD_ENABLED_VALUES
                for unit, item in zip(units, expected_units, strict=True)
            )
        ):
            raise ProbeFailure("diagnostic_receipt_units")
        expected_snapshot = [
            {"unit": item["name"], "active": item["active"], "enabled": item["enabled"]}
            for item in expected_units
        ]
        diagnostic_evidence["failure_phase"] = "unit_active"
        diagnostic_evidence["failure_check"] = "none"
        diagnostic_evidence["failure_class"] = "none"
        diagnostic_evidence["failure_reason"] = "none"
        observed_snapshot, collection_completed = _diagnostic_systemd_snapshot_with_deadline(
            module,
            deadline=readonly_deadline,
            evidence=diagnostic_evidence,
        )
        diagnostic_evidence["expected_units"] = expected_snapshot
        diagnostic_evidence["observed_units"] = observed_snapshot
        if not collection_completed:
            diagnostic_evidence["collection_completed"] = False
            diagnostic_evidence["current_facts_status"] = "unavailable"
            diagnostic_evidence["mismatch_count"] = 0
            collect_health_monitor()
            return record, result, target_label, target_path
        diagnostic_evidence["collection_completed"] = True
        comparable = all(
            row["active"] not in {"unknown", "empty", "other", "unavailable"}
            and row["enabled"] not in {"unknown", "empty", "other", "unavailable"}
            for row in observed_snapshot
        )
        mismatch_count = sum(expected != observed for expected, observed in zip(expected_snapshot, observed_snapshot, strict=True)) if comparable else 0
        diagnostic_evidence.update(
            expected_units=expected_snapshot,
            observed_units=observed_snapshot,
            mismatch_count=mismatch_count,
            current_facts_status=("unavailable" if not comparable else "mismatch" if mismatch_count else "matched"),
        )
        if comparable and mismatch_count and diagnostic_evidence["failure_reason"] == "none":
            mismatch_index = next(
                index for index, (expected, observed) in enumerate(zip(expected_snapshot, observed_snapshot, strict=True))
                if expected != observed
            )
            expected_row = expected_snapshot[mismatch_index]
            observed_row = observed_snapshot[mismatch_index]
            query = "active" if expected_row["active"] != observed_row["active"] else "enabled"
            observed_value = observed_row[query]
            query_returncode = (
                (0 if observed_value == "active" else 3)
                if query == "active"
                else (0 if observed_value in {"enabled", "static"} else 1)
            )
            diagnostic_evidence.update(
                failure_phase="unit_active" if query == "active" else "unit_enabled",
                failure_check="unit_active" if query == "active" else "unit_enabled",
                failure_class="StateError",
                failure_reason="status_mismatch",
                failure_unit=expected_row["unit"],
                failure_query=query,
                failure_rc=query_returncode,
            )
        verify_remaining = readonly_deadline - time.monotonic()
        if verify_remaining <= 0:
            diagnostic_evidence["verify_status"] = "unavailable"
            if diagnostic_evidence["failure_reason"] == "none":
                diagnostic_evidence.update(
                    failure_phase="verify", failure_check="systemd_verify",
                    failure_class="timeout", failure_reason="timeout",
                )
            collect_health_monitor()
            return record, result, target_label, target_path
        previous_verify_alarm_handler = signal.getsignal(signal.SIGALRM)
        verify_observation: dict[str, object] = {}
        original_subprocess = getattr(module, "subprocess", None)

        def observed_subprocess_run(*args: object, **kwargs: object) -> Any:
            argv = args[0] if args else kwargs.get("args")
            if isinstance(argv, (tuple, list)) and len(argv) >= 3:
                verify_observation.update(
                    query="active" if argv[1] == "is-active" else "enabled" if argv[1] == "is-enabled" else "none",
                    unit=argv[2],
                    returncode="unavailable",
                    reason="command_error",
                    error_class="oserror",
                )
            try:
                completed = original_subprocess.run(*args, **kwargs)
            except ProbeDeadlineExceeded:
                verify_observation.update(reason="timeout", error_class="timeout")
                raise
            except Exception as exc:
                verify_observation.update(
                    reason="timeout" if isinstance(exc, subprocess.TimeoutExpired) else "command_error",
                    error_class="TimeoutExpired" if isinstance(exc, subprocess.TimeoutExpired) else "oserror" if isinstance(exc, OSError) else "exception",
                )
                raise
            output = completed.stdout if isinstance(completed.stdout, str) else ""
            normalized_output = output.strip()
            command = verify_observation.get("query")
            if "\n" in normalized_output or "\r" in normalized_output:
                verify_observation.update(returncode=completed.returncode, reason="malformed_output", error_class="StateError")
            elif command in {"active", "enabled"}:
                field_value = _safe_systemd_value(normalized_output, "is-active" if command == "active" else "is-enabled")
                supported_values = {"active", "inactive"} if command == "active" else {"enabled", "disabled", "static"}
                expected_status = (0 if field_value == "active" else 3) if command == "active" else (0 if field_value in {"enabled", "static"} else 1)
                if field_value not in supported_values:
                    verify_observation.update(returncode=completed.returncode, reason="unsupported_state", error_class="StateError")
                elif completed.returncode != expected_status:
                    verify_observation.update(returncode=completed.returncode, reason="status_mismatch", error_class="StateError")
                else:
                    verify_observation.update(returncode=completed.returncode, reason="none", error_class="none")
            return completed

        def verify_systemd_state() -> None:
            module.subprocess = types.SimpleNamespace(
                **{**vars(original_subprocess), "run": observed_subprocess_run}
            )
            try:
                try:
                    module.verify(
                        SYSTEMD_RECEIPT,
                        app_dir=APP_DIR,
                        transaction=TRANSACTION,
                        helper_release=validation_release,
                    )
                except ProbeDeadlineExceeded:
                    raise
                except Exception as exc:
                    cause = exc.__cause__
                    observed_reason = verify_observation.get("reason", "none")
                    if observed_reason == "none":
                        observed_reason = (
                            "timeout" if isinstance(exc, subprocess.TimeoutExpired) or isinstance(cause, subprocess.TimeoutExpired)
                            else "command_error" if isinstance(exc, OSError) or isinstance(cause, OSError)
                            else "verify_mismatch" if (
                                type(exc).__name__ == "StateError"
                                and verify_observation.get("query") in {"active", "enabled"}
                            )
                            else "verification_error"
                        )
                    observed_class = (
                        "TimeoutExpired" if isinstance(exc, subprocess.TimeoutExpired) or isinstance(cause, subprocess.TimeoutExpired)
                        else "oserror" if isinstance(exc, OSError) or isinstance(cause, OSError)
                        else type(exc).__name__ if type(exc).__name__ in _DIAGNOSTIC_CLASSES
                        else "exception"
                    )
                    verify_observation.update(reason=observed_reason, error_class=observed_class)
                    raise ProbeFailure("systemd_verify", observed_class) from None
            finally:
                module.subprocess = original_subprocess

        def verify_deadline_expired(_signum: int, _frame: Any) -> None:
            raise ProbeDeadlineExceeded

        signal.signal(signal.SIGALRM, verify_deadline_expired)
        signal.setitimer(signal.ITIMER_REAL, verify_remaining)
        try:
            if diagnostic_evidence["failure_reason"] == "none":
                diagnostic_evidence["failure_phase"] = "verify"
                diagnostic_evidence["failure_check"] = "systemd_verify"
            verify_systemd_state()
        except ProbeDeadlineExceeded:
            diagnostic_evidence["verify_status"] = "unavailable"
            if diagnostic_evidence["failure_reason"] == "none":
                diagnostic_evidence.update(
                    failure_phase="verify",
                    failure_check="systemd_verify",
                    failure_class="timeout", failure_reason="timeout",
                    failure_unit=verify_observation.get("unit", "none"),
                    failure_query=verify_observation.get("query", "none"),
                    failure_rc=verify_observation.get("returncode", "unavailable"),
                )
        except ProbeFailure as exc:
            diagnostic_evidence["verify_status"] = "failed"
            if diagnostic_evidence["failure_reason"] == "none":
                error_class = verify_observation.get("error_class", exc.error_class)
                if error_class not in _DIAGNOSTIC_CLASSES:
                    error_class = "exception"
                reason = verify_observation.get("reason", "none")
                if reason not in _DIAGNOSTIC_REASONS:
                    reason = "verification_error"
                if reason == "none":
                    reason = "verify_mismatch"
                diagnostic_evidence["failure_class"] = error_class
                diagnostic_evidence["failure_reason"] = reason
                diagnostic_evidence["failure_phase"] = "verify"
                diagnostic_evidence["failure_check"] = "systemd_verify"
                if reason in {"unsupported_state", "status_mismatch", "malformed_output", "command_error", "timeout"}:
                    diagnostic_evidence["failure_unit"] = verify_observation.get("unit", "none")
                    diagnostic_evidence["failure_query"] = verify_observation.get("query", "none")
                    diagnostic_evidence["failure_rc"] = verify_observation.get("returncode", "unavailable")
                else:
                    diagnostic_evidence["failure_unit"] = "none"
                    diagnostic_evidence["failure_query"] = "none"
                    diagnostic_evidence["failure_rc"] = "unavailable"
        else:
            diagnostic_evidence["verify_status"] = "passed"
            if diagnostic_evidence["failure_reason"] == "none":
                diagnostic_evidence["failure_phase"] = "none"
                diagnostic_evidence["failure_check"] = "none"
                diagnostic_evidence["failure_class"] = "none"
                diagnostic_evidence["failure_reason"] = "none"
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_verify_alarm_handler)
        collect_health_monitor()
        return record, result, target_label, target_path
    states = _unit_snapshot_with_deadline(
        module,
        deadline=readonly_deadline,
        result=result,
    )
    if (
        not isinstance(states, list)
        or not isinstance(units, (tuple, list))
        or len(units) != 13
        or len(states) != 13
        or any(not isinstance(item, dict) or item.get("name") != unit or item.get("active") not in _SYSTEMD_STATE_VALUES or item.get("enabled") not in _SYSTEMD_ENABLED_VALUES for unit, item in zip(units, states, strict=True))
    ):
        raise ProbeFailure("unit_snapshot")
    result["service_active_count"] = sum(item["active"] == "active" for item in states)
    result["service_enabled_count"] = sum(item["enabled"] == "enabled" for item in states)
    if target is None:
        result["nginx_preflight"] = "not_applicable"
        result["nginx_preflight_returncode"] = "unavailable"
        return record, result, target_label, target_path
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PLATFORM_APP_DIR": str(APP_DIR), "PYTHONDONTWRITEBYTECODE": "1"}

    integrity_status = "runtime_payload_verify"
    integrity_private_detail: dict[str, object] | None = None
    previous_alarm_handler = signal.getsignal(signal.SIGALRM)
    remaining_integrity_seconds = readonly_deadline - time.monotonic()
    if remaining_integrity_seconds <= 0:
        raise ProbeFailure("runtime_payload_verify", "child_failure", safe_fields=result)

    def integrity_deadline_expired(_signum: int, _frame: Any) -> None:
        raise ProbeDeadlineExceeded

    signal.signal(signal.SIGALRM, integrity_deadline_expired)
    signal.setitimer(signal.ITIMER_REAL, remaining_integrity_seconds)
    try:
        integrity, failed_stage, private_detail = _runtime_payload_integrity(
            target,
            source_sha=source_sha,
            expected_helper_sha256=runtime_installer_digest,
            deadline=readonly_deadline,
        )
        result.update(integrity)
        if failed_stage is not None:
            integrity_status = failed_stage
            integrity_private_detail = private_detail
            raise ProbeFailure(failed_stage, "child_failure", safe_fields=result)
    except ProbeDeadlineExceeded:
        result["runtime_payload_verify"] = "timeout"
        result["runtime_payload_verify_returncode"] = "unavailable"
        if _private_capture_requested():
            _private_runtime_detail(
                "runtime_payload_verify",
                {"schema": 1, "reason": "read_only_validation_timeout"},
            )
        raise ProbeFailure("runtime_payload_verify", "child_failure", safe_fields=result) from None
    except ProbeFailure as exc:
        failed_check = exc.check
        if failed_check == "runtime_expected_tree":
            state = "timeout" if exc.error_class == "timeout" else "failed"
            result["runtime_expected_tree"] = state
            result["runtime_expected_tree_returncode"] = "unavailable" if state == "timeout" else 1
            integrity_status = "runtime_expected_tree"
        else:
            result["runtime_payload_verify"] = "timeout" if exc.error_class == "timeout" else "failed"
            result["runtime_payload_verify_returncode"] = "unavailable" if exc.error_class == "timeout" else 1
            integrity_status = "runtime_payload_verify"
        if _private_capture_requested():
            _private_runtime_detail(
                integrity_status,
                integrity_private_detail
                or {
                    "schema": 1,
                    "reason": "read_only_validation_failed",
                    "failed_check": failed_check,
                    "error_class": exc.error_class,
                },
            )
        raise ProbeFailure(integrity_status, "child_failure", safe_fields=result) from None
    except Exception as exc:
        result["runtime_payload_verify"] = "failed"
        result["runtime_payload_verify_returncode"] = 1
        if _private_capture_requested():
            _private_runtime_detail(
                "runtime_payload_verify",
                {
                    "schema": 1,
                    "reason": "read_only_validation_exception",
                    "exception_class": type(exc).__name__[:96],
                    "message": str(exc)[:4096],
                },
            )
        raise ProbeFailure("runtime_payload_verify", "child_failure", safe_fields=result) from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm_handler)

    private_capture = _private_capture_requested()
    remaining_seconds = int(readonly_deadline - time.monotonic())
    if remaining_seconds <= 15:
        result["nginx_preflight"] = "timeout"
        result["nginx_preflight_returncode"] = "unavailable"
        raise ProbeFailure("nginx_preflight", "child_failure", safe_fields=result)
    nginx_timeout = min(60, remaining_seconds - 15)
    argv = [str(SHARED_PYTHON), str(target / "tools/platform_install_nginx.py"), "--json"]
    private_stderr = None
    private_total = 0
    private_position = 0
    private_retained = 0
    try:
        if private_capture:
            returncode, private_total, private_stderr, private_position, private_retained, timed_out = _run_private_preflight(argv, env, timeout_seconds=nginx_timeout)
            if timed_out:
                result["nginx_preflight"] = "timeout"
                result["nginx_preflight_returncode"] = "unavailable"
                raise ProbeFailure("nginx_preflight", "child_failure", safe_fields=result)
        else:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=nginx_timeout,
                check=False,
                env=env,
            )
            returncode = completed.returncode
    except subprocess.TimeoutExpired:
        result["nginx_preflight"] = "timeout"
        result["nginx_preflight_returncode"] = "unavailable"
        raise ProbeFailure("nginx_preflight", "child_failure", safe_fields=result) from None
    except OSError:
        result["nginx_preflight"] = "unavailable"
        result["nginx_preflight_returncode"] = "unavailable"
        raise ProbeFailure("nginx_preflight", "child_failure", safe_fields=result) from None
    finally:
        if private_stderr is not None:
            if private_capture and returncode != 0:
                _emit_private_runtime_context(
                    private_stderr,
                    component="nginx_preflight",
                    returncode=returncode,
                    total_bytes=private_total,
                    position=private_position,
                    retained_bytes=private_retained,
                )
            private_stderr.close()
    result["nginx_preflight"] = "passed" if returncode == 0 else "failed"
    result["nginx_preflight_returncode"] = returncode if type(returncode) is int and 0 <= returncode <= 255 else "unavailable"
    if returncode != 0:
        raise ProbeFailure("nginx_preflight", "child_failure", safe_fields=result)
    return record, result, target_label, target_path


def _run(check: str, operation: Any) -> Any:
    try:
        return operation()
    except ProbeFailure:
        raise
    except ProbeDeadlineExceeded:
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


def _diagnostic_systemd_probe(
    expected_systemd_sha256: str,
    *,
    companion_present: bool,
    deadline: float,
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "schema": 1,
        "collection_completed": False,
        "validate_status": "unavailable",
        "verify_status": "unavailable",
        "current_facts_status": "unavailable",
        "mismatch_count": 0,
        "historical_cause_proven": False,
        "expected_units": [],
        "observed_units": [],
        "failure_phase": "module",
        "failure_check": "none",
        "failure_class": "none",
        "failure_reason": "none",
        "failure_unit": "none",
        "failure_query": "none",
        "failure_rc": "unavailable",
        "health_monitor": _health_monitor_initial_evidence(),
    }
    try:
        if not companion_present:
            raise ProbeFailure("diagnostic_receipt_absent")
        module = _load_pinned_module(expected_systemd_sha256)
        evidence["failure_phase"] = "systemctl_path"
        _run("systemctl_path", lambda: module._validate_systemctl_path(module.SYSTEMCTL))
        evidence["failure_phase"] = "transaction"
        transaction = _run("transaction_read", lambda: module._read_transaction(TRANSACTION, APP_DIR))
        evidence["failure_phase"] = "receipt"
        _receipt_snapshot_and_preflight(
            module,
            transaction,
            deadline=deadline,
            diagnostic_evidence=evidence,
        )
    except Exception as exc:
        _finalize_skipped_health_monitor(evidence)
        check = exc.check if isinstance(exc, ProbeFailure) else "none"
        error_class = exc.error_class if isinstance(exc, ProbeFailure) else type(exc).__name__
        evidence["failure_check"] = check if check in _DIAGNOSTIC_CHECKS else "none"
        evidence["failure_class"] = error_class if error_class in _DIAGNOSTIC_CLASSES else "exception"
        if evidence["failure_reason"] == "none":
            evidence["failure_reason"] = (
                "timeout" if evidence["failure_class"] in {"timeout", "TimeoutExpired"}
                else "command_error" if evidence["failure_class"] in {"oserror", "OSError"}
                else "receipt_state_invalid" if check == "existing_receipt_validate"
                else "binding" if evidence["failure_phase"] in {"module", "systemctl_path", "transaction", "receipt", "binding"}
                else "verification_error"
            )
        state = "unavailable"
        public_report: dict[str, object] = {
            "schema": 1,
            "kind": "release_capture_probe",
            "status": "failed",
            "failed_check": "diagnostic_unavailable",
            "error_class": "predicate",
            "diagnostic_systemd_state": state,
            "diagnostic_systemd_state_returncode": 1,
        }
        marker_returncode = 1
    else:
        passed = (
            evidence["collection_completed"] is True
            and evidence["validate_status"] == "passed"
            and evidence["verify_status"] == "passed"
            and evidence["current_facts_status"] == "matched"
        )
        state = "passed" if passed else (
            "mismatch"
            if evidence["current_facts_status"] == "mismatch"
            else "unavailable"
        )
        public_report = {
            "schema": 1,
            "kind": "release_capture_probe",
            "status": "passed" if passed else "failed",
            "failed_check": "diagnostic_mismatch" if state == "mismatch" else "diagnostic_unavailable",
            "error_class": "predicate",
            "diagnostic_systemd_state": state,
            "diagnostic_systemd_state_returncode": 0 if passed else 1,
        }
        if passed:
            public_report.pop("failed_check")
            public_report.pop("error_class")
        marker_returncode = 0 if passed else 1
    _private_runtime_report(
        "systemd_state_probe",
        evidence,
        returncode=marker_returncode,
    )
    return public_report


def probe(
    expected_systemd_sha256: str,
    *,
    diagnostic_systemd_state: bool = False,
) -> dict[str, object]:
    readonly_deadline = time.monotonic() + 480
    if os.geteuid() != 0:
        raise ProbeFailure("root", "permission")
    if diagnostic_systemd_state and not _private_capture_requested():
        raise ProbeFailure("diagnostic_private_capture", "permission")
    try:
        companion = SYSTEMD_RECEIPT.lstat()
        companion_present = True
        if stat.S_ISLNK(companion.st_mode) or not stat.S_ISREG(companion.st_mode):
            raise ProbeFailure("companion_metadata")
    except FileNotFoundError:
        companion_present = False
    except OSError:
        raise ProbeFailure("companion_lstat", "oserror") from None

    if diagnostic_systemd_state:
        if not _private_capture_requested():
            raise ProbeFailure("diagnostic_private_capture", "permission")
        return _diagnostic_systemd_probe(
            expected_systemd_sha256,
            companion_present=companion_present,
            deadline=readonly_deadline,
        )

    module = _load_pinned_module(expected_systemd_sha256)
    if companion_present:
        _run("systemctl_path", lambda: module._validate_systemctl_path(module.SYSTEMCTL))
        transaction = _run("transaction_read", lambda: module._read_transaction(TRANSACTION, APP_DIR))
        _receipt, receipt_result, _target_label, _target_path = _receipt_snapshot_and_preflight(
            module,
            transaction,
            deadline=readonly_deadline,
        )
        return {
            "schema": 1,
            "kind": "release_capture_probe",
            "status": "passed",
            "capture_predicate": "existing_receipt_valid",
            "health": _health(),
            **receipt_result,
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
        "runtime_payload_verify": "not_applicable",
        "runtime_payload_verify_returncode": "unavailable",
    }


def main() -> int:
    arguments = sys.argv[1:]
    diagnostic_systemd_state = bool(arguments and arguments[0] == "--diagnostic-systemd-state")
    if diagnostic_systemd_state:
        arguments = arguments[1:]
    if len(arguments) != 2:
        report: dict[str, object] = {
            "schema": 1,
            "kind": "release_capture_probe",
            "status": "failed",
            "failed_check": "probe_identity",
            "error_class": "input",
        }
        if not diagnostic_systemd_state:
            report["health"] = _health()
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 1
    expected_probe_sha256, expected_systemd_sha256 = arguments
    try:
        probe_source, probe_metadata = _safe_module_bytes(Path(__file__))
        if (
            re.fullmatch(r"[0-9a-f]{64}", expected_probe_sha256) is None
            or hashlib.sha256(probe_source).hexdigest() != expected_probe_sha256
        ):
            raise ProbeFailure("probe_identity")
        if stat.S_IMODE(probe_metadata.st_mode) != 0o600:
            raise ProbeFailure("probe_identity")
        report = probe(
            expected_systemd_sha256,
            diagnostic_systemd_state=diagnostic_systemd_state,
        )
    except ProbeFailure as exc:
        report = {
            "schema": 1,
            "kind": "release_capture_probe",
            "status": "failed",
            "failed_check": exc.check,
            "error_class": exc.error_class,
        }
        if not diagnostic_systemd_state:
            report["health"] = _health()
        if exc.systemd_observation is not None:
            report["systemd_observation"] = exc.systemd_observation
        report.update(exc.safe_fields)
    except Exception:
        report = {
            "schema": 1,
            "kind": "release_capture_probe",
            "status": "failed",
            "failed_check": "internal",
            "error_class": "exception",
        }
        if not diagnostic_systemd_state:
            report["health"] = _health()
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
