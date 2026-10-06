"""Run the fixed retained-report recovery while keeping bounded stderr private."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import BinaryIO


RUNTIME_ROOT = Path("/opt/oldsparky/platform")
RUN_ROOT_BASE = RUNTIME_ROOT / "shared" / "production-retained-matrix"
MAX_CAPTURE_BYTES = 65_536
MAX_STDIN_BYTES = 4096
TRUNCATION_MARKER = b"\n[stderr capture truncated at 64 KiB]\n"
RUN_ID_RE = re.compile(r"[1-9][0-9]{0,31}\Z")
MODES = frozenset({"read-mix", "write-burst", "external-vote"})
RECOVERY_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "LC_CTYPE": "C.UTF-8",
}
EMAIL_LOCAL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._%+\-]{0,62}[A-Za-z0-9])?\Z")
EMAIL_DOMAIN_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\Z")


class CaptureError(RuntimeError):
    """A fixed failure class; details must remain private."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CaptureError("duplicate_input_key")
        result[key] = value
    return result


def parse_request(raw: bytes) -> dict[str, str]:
    if len(raw) > MAX_STDIN_BYTES:
        raise CaptureError("input_too_large")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError("input_invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "load_run_id",
        "cleanup_run_id",
        "control_email",
        "mode",
    }:
        raise CaptureError("input_schema_invalid")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise CaptureError("input_schema_invalid")
    load_run_id = value["load_run_id"]
    cleanup_run_id = value["cleanup_run_id"]
    control_email = value["control_email"]
    mode = value["mode"]
    if not isinstance(load_run_id, str) or RUN_ID_RE.fullmatch(load_run_id) is None:
        raise CaptureError("load_run_id_invalid")
    if (
        not isinstance(cleanup_run_id, str)
        or RUN_ID_RE.fullmatch(cleanup_run_id) is None
    ):
        raise CaptureError("cleanup_run_id_invalid")
    if not isinstance(control_email, str) or not _valid_control_email(control_email):
        raise CaptureError("control_input_invalid")
    if not isinstance(mode, str) or mode not in MODES:
        raise CaptureError("mode_invalid")
    return {
        "load_run_id": load_run_id,
        "cleanup_run_id": cleanup_run_id,
        "control_email": control_email,
        "mode": mode,
    }


def _valid_control_email(value: str) -> bool:
    if not value.isascii() or not 3 <= len(value) <= 254 or value != value.strip():
        return False
    if any(ord(character) < 0x21 or ord(character) == 0x7F for character in value):
        return False
    if value.count("@") != 1:
        return False
    local, domain = value.split("@", 1)
    if not 1 <= len(local) <= 64 or EMAIL_LOCAL_RE.fullmatch(local) is None:
        return False
    if not 1 <= len(domain) <= 253 or "." not in domain:
        return False
    labels = domain.split(".")
    return all(EMAIL_DOMAIN_LABEL_RE.fullmatch(label) is not None for label in labels)


def _open_parent_directory(path: Path) -> int:
    """Open a fixed absolute directory through no-follow dirfds."""
    if not path.is_absolute():
        raise CaptureError("invalid_root_path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            metadata = os.fstat(next_descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_gid != 0
                or metadata.st_mode & 0o022
            ):
                os.close(next_descriptor)
                raise CaptureError("root_path_metadata_invalid")
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _root_descriptor(load_run_id: str) -> tuple[int, os.stat_result, str]:
    if RUN_ID_RE.fullmatch(load_run_id) is None:
        raise CaptureError("load_run_id_invalid")
    parent_fd = _open_parent_directory(RUN_ROOT_BASE)
    root_name = f"gha-{load_run_id}"
    try:
        root_fd = os.open(
            root_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except BaseException:
        os.close(parent_fd)
        raise
    metadata = os.fstat(root_fd)
    named = os.stat(root_name, dir_fd=parent_fd, follow_symlinks=False)
    os.close(parent_fd)
    if (
        metadata.st_dev != named.st_dev
        or metadata.st_ino != named.st_ino
        or not os.path.samestat(metadata, named)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & 0o7777 != 0o700
    ):
        os.close(root_fd)
        raise CaptureError("run_root_metadata_invalid")
    return root_fd, metadata, root_name


def _verify_root_binding(
    root_fd: int, root_metadata: os.stat_result, root_name: str
) -> None:
    parent_fd = _open_parent_directory(RUN_ROOT_BASE)
    try:
        named = os.stat(root_name, dir_fd=parent_fd, follow_symlinks=False)
        current = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(named.st_mode)
            or not os.path.samestat(root_metadata, current)
            or not os.path.samestat(root_metadata, named)
            or current.st_uid != 0
            or current.st_gid != 0
            or current.st_mode & 0o7777 != 0o700
        ):
            raise CaptureError("run_root_binding_changed")
    finally:
        os.close(parent_fd)


def _write_bounded(stream: BinaryIO, output: BinaryIO) -> bool:
    retained = 0
    truncated = False
    content_limit = MAX_CAPTURE_BYTES - len(TRUNCATION_MARKER)
    while True:
        chunk = stream.read(4096)
        if not chunk:
            break
        remaining = content_limit - retained
        if remaining > 0:
            kept = chunk[:remaining]
            output.write(kept)
            retained += len(kept)
        if len(chunk) > max(remaining, 0):
            truncated = True
    if truncated:
        output.write(TRUNCATION_MARKER)
    return truncated


def capture_recovery_stderr(
    *, load_run_id: str, cleanup_run_id: str, control_email: str, mode: str
) -> int:
    """Run the fixed recovery command and retain at most 64 KiB of stderr."""
    if RUN_ID_RE.fullmatch(cleanup_run_id) is None:
        raise CaptureError("cleanup_run_id_invalid")
    if mode not in MODES:
        raise CaptureError("mode_invalid")
    if not _valid_control_email(control_email):
        raise CaptureError("control_input_invalid")

    root_fd, root_metadata, root_name = _root_descriptor(load_run_id)
    capture_name = f"cleanup-recovery-{load_run_id}-{cleanup_run_id}.stderr"
    descriptor = -1
    process: subprocess.Popen[bytes] | None = None
    try:
        descriptor = os.open(
            capture_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
        created = os.fstat(descriptor)
        if (
            not os.path.samestat(
                created,
                os.stat(capture_name, dir_fd=root_fd, follow_symlinks=False),
            )
            or created.st_dev != root_metadata.st_dev
            or created.st_uid != 0
            or created.st_gid != 0
            or created.st_mode & 0o7777 != 0o600
            or created.st_nlink != 1
        ):
            raise CaptureError("capture_metadata_invalid")
        _verify_root_binding(root_fd, root_metadata, root_name)

        tools_dir = RUNTIME_ROOT / "current" / "tools"
        platform_root = RUNTIME_ROOT / "current"
        command = [
            "/usr/bin/python3.12",
            "-I",
            "-B",
            str(tools_dir / "platform_safe_env_exec.py"),
            "exec",
            "--pythonpath",
            str(platform_root),
            "--",
            str(RUNTIME_ROOT / "shared" / "venv" / "bin" / "python"),
            str(tools_dir / "platform_recover_retained_report.py"),
            "--run-root",
            str(RUN_ROOT_BASE / root_name),
            "--load-run-id",
            load_run_id,
            "--control-email-stdin",
            "--mode",
            mode,
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd="/",
            env=RECOVERY_ENV,
            close_fds=True,
        )
        assert process.stdin is not None and process.stderr is not None
        process.stdin.write(control_email.encode("ascii") + b"\n")
        process.stdin.close()
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            _write_bounded(process.stderr, output)
            output.flush()
            os.fsync(output.fileno())
        child_status = process.wait()
        captured = os.stat(capture_name, dir_fd=root_fd, follow_symlinks=False)
        if (
            captured.st_dev != created.st_dev
            or captured.st_ino != created.st_ino
            or captured.st_uid != 0
            or captured.st_gid != 0
            or captured.st_mode & 0o7777 != 0o600
            or captured.st_nlink != 1
            or captured.st_size > MAX_CAPTURE_BYTES
        ):
            raise CaptureError("capture_changed")
        _verify_root_binding(root_fd, root_metadata, root_name)
        if child_status < 0:
            return min(255, 128 + -child_status)
        return child_status
    except BaseException:
        if process is not None and process.poll() is None:
            process.wait()
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(root_fd)


def main() -> int:
    try:
        if len(sys.argv) != 1:
            return 125
        request = parse_request(sys.stdin.buffer.read(MAX_STDIN_BYTES + 1))
        return capture_recovery_stderr(
            load_run_id=request["load_run_id"],
            cleanup_run_id=request["cleanup_run_id"],
            control_email=request["control_email"],
            mode=request["mode"],
        )
    except BaseException:
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
