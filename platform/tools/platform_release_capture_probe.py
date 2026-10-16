#!/usr/bin/env python3
"""Read-only diagnosis of the systemd receipt capture preconditions."""

from __future__ import annotations

import base64
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
        for path in source.rglob("*"):
            check_deadline()
            relative = path.relative_to(source).as_posix()
            target_relative = f"{destination}/{relative}"
            metadata = module._metadata(path)
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
    evidence["private_detail"] = {
        "schema": 1,
        "reason": "expected_tree_mismatch",
        "expected_tree_sha256": expected_digest,
        "actual_tree_sha256": actual_digest if isinstance(actual_digest, str) else "unavailable",
        "expected_file_count": len(expected_files),
        "actual_file_count": len(actual_files),
        "missing_count": len(missing),
        "missing": [item[:256] for item in missing[:8]],
        "extra_count": len(extra),
        "extra": [item[:256] for item in extra[:8]],
        "changed_count": len(changed),
        "changed": [item[:256] for item in changed[:8]],
    }
    return False, evidence


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
        return evidence, "failed", evidence.pop("private_detail")
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
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or before.st_gid != 0
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
    module: Any, transaction: tuple[object, ...], *, deadline: float
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
    _run(
        "existing_receipt_validate",
        lambda: module.validate(
            SYSTEMD_RECEIPT,
            app_dir=APP_DIR,
            transaction=TRANSACTION,
            helper_release=validation_release,
        ),
    )
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
    actual_helpers = _run("helper_digests", lambda: module._helper_digests(validation_release))
    nginx_digest = actual_helpers.get("tools/platform_install_nginx.py") if isinstance(actual_helpers, dict) else None
    if not isinstance(nginx_digest, str) or manifest.get("tools/platform_install_nginx.py") != nginx_digest:
        raise ProbeFailure("runtime_target_binding")
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
    states = _unit_snapshot_with_deadline(
        module,
        deadline=readonly_deadline,
        result=result,
    )
    units = getattr(module, "OWNED_UNITS", ())
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


def probe(expected_systemd_sha256: str) -> dict[str, object]:
    readonly_deadline = time.monotonic() + 480
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
        report.update(exc.safe_fields)
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
