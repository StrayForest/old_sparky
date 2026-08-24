#!/usr/bin/env python3
"""Safely parse platform dotenv files and exec approved production commands.

The parser deliberately does not use a shell or ``python-dotenv``. Production
paths are opened without following symlinks and configuration values are always
treated as data. The fixed production exec contour remains narrowly allowlisted.
"""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import re
import shlex
import stat
import sys
from typing import Iterable


PRODUCTION_ENV_FILE = Path("/opt/oldsparky/platform/shared/.env.platform")
PRODUCTION_SHARED_DIR = PRODUCTION_ENV_FILE.parent
MAX_ENV_BYTES = 256 * 1024
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
ALLOWED_PREFIXES = ("PLATFORM_", "NEXT_PUBLIC_PLATFORM_")
PUBLIC_VALUE_NAMES = frozenset({"PLATFORM_ENVIRONMENT", "PLATFORM_WEB_ORIGIN"})
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
TRUSTED_PLATFORM_ROOT = Path("/root/old_sparky/platform")
TRUSTED_PYTHON = TRUSTED_PLATFORM_ROOT / ".venv_platform/bin/python"
TRUSTED_SYSTEM_PYTHON = Path("/usr/bin/python3.12")
TRUSTED_DB_TOOLS = frozenset(
    {
        "platform_cleanup_live_user_qa.py",
        "platform_cleanup_retained_matrix.py",
        "platform_manual_live_auth_qa.py",
        "platform_provision_live_csp_qa.py",
        "platform_recover_live_user_qa.py",
    }
)


class SafeEnvError(RuntimeError):
    """A non-sensitive platform-env boundary failure."""


def _open_component(
    name: str,
    *,
    directory_fd: int,
    directory: bool,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        return os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise SafeEnvError("environment path is unavailable or unsafe") from exc


def _production_component_owners() -> tuple[int, ...]:
    # /, /opt, /opt/oldsparky, /opt/oldsparky/platform, .../shared
    return (0, 0, 0, 0, 0)


def _validate_directory(metadata: os.stat_result, *, expected_uid: int) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise SafeEnvError("environment path ownership is unsafe")


def _validate_production_env_file(metadata: os.stat_result) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or metadata.st_size > MAX_ENV_BYTES
        or mode != 0o600
        or metadata.st_gid != 0
    ):
        raise SafeEnvError("production environment file metadata is unsafe")


# Backward-compatible private helper used by metadata regression tests.
_validate_env_file = _validate_production_env_file

def _read_env_bytes_at(path: Path, *, owners: tuple[int, ...]) -> bytes:
    """Open an absolute path one trusted component at a time."""

    parts = path.parts
    if (
        not path.is_absolute()
        or not parts
        or parts[0] != "/"
        or any(part in {"", ".", ".."} for part in parts[1:])
        or len(owners) != len(parts) - 1
    ):
        raise SafeEnvError("production environment path is invalid")
    root_fd = os.open(
        "/",
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    current_fd = root_fd
    opened: list[int] = []
    try:
        _validate_directory(os.fstat(root_fd), expected_uid=owners[0])
        for index, component in enumerate(parts[1:-1], start=1):
            next_fd = _open_component(
                component,
                directory_fd=current_fd,
                directory=True,
            )
            opened.append(next_fd)
            _validate_directory(os.fstat(next_fd), expected_uid=owners[index])
            current_fd = next_fd
        file_fd = _open_component(
            parts[-1],
            directory_fd=current_fd,
            directory=False,
        )
        opened.append(file_fd)
        before = os.fstat(file_fd)
        _validate_production_env_file(before)
        raw = _read_open_file(file_fd, before)
        return raw
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
        os.close(root_fd)


def _read_open_file(file_fd: int, before: os.stat_result) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_ENV_BYTES + 1
    while remaining:
        chunk = os.read(file_fd, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    after = os.fstat(file_fd)
    if len(raw) > MAX_ENV_BYTES or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise SafeEnvError("environment changed while reading")
    return raw


def read_production_env_bytes(path: Path = PRODUCTION_ENV_FILE) -> bytes:
    """Open the fixed production environment without following any symlink."""

    if path != PRODUCTION_ENV_FILE or path.parts != (
        "/",
        "opt",
        "oldsparky",
        "platform",
        "shared",
        ".env.platform",
    ):
        raise SafeEnvError("production environment path must be fixed")
    return _read_env_bytes_at(path, owners=_production_component_owners())


def _is_production_shared_path(path: Path) -> bool:
    if not path.is_absolute():
        return False
    try:
        path.relative_to(PRODUCTION_SHARED_DIR)
    except ValueError:
        return False
    return True


def _validate_generic_path(path: Path, metadata: os.stat_result) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_ENV_BYTES
        or mode & 0o022
    ):
        raise SafeEnvError("environment file metadata is unsafe")
    if _is_production_shared_path(path):
        if metadata.st_uid != 0:
            raise SafeEnvError("production runtime environment must be root-owned")
        current = path.parent
        while current != Path("/"):
            current_metadata = current.lstat()
            if (
                stat.S_ISLNK(current_metadata.st_mode)
                or not stat.S_ISDIR(current_metadata.st_mode)
                or current_metadata.st_uid != 0
                or stat.S_IMODE(current_metadata.st_mode) & 0o022
            ):
                raise SafeEnvError("production runtime environment path is unsafe")
            current = current.parent


def read_env_bytes(path: Path) -> bytes:
    """Read a platform dotenv file without executing it or following the final link."""

    if path == PRODUCTION_ENV_FILE:
        return read_production_env_bytes(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SafeEnvError("environment file is unavailable") from exc
    _validate_generic_path(path, metadata)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(path, flags)
    except OSError as exc:
        raise SafeEnvError("environment file is unavailable or unsafe") from exc
    try:
        opened = os.fstat(file_fd)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise SafeEnvError("environment file changed during validation")
        _validate_generic_path(path, opened)
        return _read_open_file(file_fd, opened)
    finally:
        os.close(file_fd)


def _parse_value(value: str, *, line_number: int) -> str:
    lexer = shlex.shlex(value, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise SafeEnvError(f"invalid dotenv quoting on line {line_number}") from exc
    if len(tokens) == 0 and value == "":
        return ""
    if len(tokens) != 1:
        raise SafeEnvError(f"dotenv value on line {line_number} is ambiguous")
    parsed = tokens[0]
    if "\x00" in parsed or "\n" in parsed or "\r" in parsed:
        raise SafeEnvError(f"dotenv value on line {line_number} is unsafe")
    return parsed


def parse_dotenv(raw: bytes) -> dict[str, str]:
    if len(raw) > MAX_ENV_BYTES:
        raise SafeEnvError("environment exceeds its size limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SafeEnvError("environment must be UTF-8") from exc
    if "\x00" in text or "\u0085" in text or "\u2028" in text or "\u2029" in text:
        raise SafeEnvError("environment contains an unsafe line separator")
    if "\r" in text.replace("\r\n", ""):
        raise SafeEnvError("environment contains a bare carriage return")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or "=" not in line:
            raise SafeEnvError(f"invalid dotenv assignment on line {line_number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if (
            not KEY_PATTERN.fullmatch(key)
            or not key.startswith(ALLOWED_PREFIXES)
            or key in values
        ):
            raise SafeEnvError(f"invalid or duplicate dotenv key on line {line_number}")
        values[key] = _parse_value(raw_value.strip(), line_number=line_number)
    if not values:
        raise SafeEnvError("environment is empty")
    return values


def load_env_file(path: Path) -> dict[str, str]:
    return parse_dotenv(read_env_bytes(path))


def clean_child_environment(
    values: dict[str, str], *, pythonpath: Path
) -> dict[str, str]:
    if pythonpath != TRUSTED_PLATFORM_ROOT:
        raise SafeEnvError("PYTHONPATH must be the fixed root-controlled checkout")
    child = dict(values)
    child.update(
        {
            "LANG": "C.UTF-8",
            "HOME": "/nonexistent",
            "PATH": SAFE_PATH,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(pythonpath),
        }
    )
    return child


def validate_trusted_command(command: list[str], *, pythonpath: Path) -> None:
    if len(command) < 2 or Path(command[0]) != TRUSTED_PYTHON:
        raise SafeEnvError(
            "clean exec requires the fixed root-controlled Python runtime"
        )
    script = Path(command[1])
    if (
        not script.is_absolute()
        or script.parent != TRUSTED_PLATFORM_ROOT / "tools"
        or script.name not in TRUSTED_DB_TOOLS
    ):
        raise SafeEnvError("clean exec target is not an approved live QA DB tool")
    for path in (TRUSTED_PLATFORM_ROOT, TRUSTED_PYTHON, script):
        try:
            resolved = path.resolve(strict=True)
            metadata = path.lstat()
        except OSError as exc:
            raise SafeEnvError("clean exec target is unavailable") from exc
        if metadata.st_uid != 0 or (
            path != TRUSTED_PYTHON and stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise SafeEnvError("clean exec target ownership is unsafe")
        if path == TRUSTED_PYTHON:
            if resolved != TRUSTED_SYSTEM_PYTHON or not resolved.is_file():
                raise SafeEnvError("clean exec Python target is unsafe")
        elif path == script and (
            stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
        ):
            raise SafeEnvError("clean exec script target is unsafe")
    validate_trusted_runtime()


def _validate_root_owned_tree(root: Path) -> None:
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise SafeEnvError("root-controlled Python import tree is unavailable") from exc
    for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in [*names, *filenames]:
            target = directory_path / name
            try:
                metadata = target.lstat()
            except OSError as exc:
                raise SafeEnvError(
                    "root-controlled Python import tree changed"
                ) from exc
            if metadata.st_uid != 0:
                raise SafeEnvError("root-controlled Python import tree owner is unsafe")
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    resolved = target.resolve(strict=True)
                except OSError as exc:
                    raise SafeEnvError(
                        "root-controlled Python symlink is invalid"
                    ) from exc
                if resolved != TRUSTED_SYSTEM_PYTHON:
                    try:
                        resolved.relative_to(resolved_root)
                    except ValueError as exc:
                        raise SafeEnvError(
                            "root-controlled Python symlink escapes"
                        ) from exc
                elif not (
                    root == TRUSTED_PLATFORM_ROOT / ".venv_platform"
                    and target.parent == root / "bin"
                    and target.name in {"python", "python3", "python3.12"}
                ):
                    raise SafeEnvError(
                        "root-controlled Python symlink target is unsafe"
                    )
            elif stat.S_ISDIR(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) & 0o022:
                    raise SafeEnvError("root-controlled Python directory is writable")
            elif stat.S_ISREG(metadata.st_mode):
                if (
                    metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                    or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID)
                ):
                    raise SafeEnvError(
                        "root-controlled Python file is writable or linked"
                    )
            else:
                raise SafeEnvError(
                    "root-controlled Python import tree has a special file"
                )


def validate_trusted_runtime() -> None:
    for directory in (
        Path("/root"),
        Path("/root/old_sparky"),
        TRUSTED_PLATFORM_ROOT,
    ):
        metadata = directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise SafeEnvError("root-controlled checkout path is unsafe")
    for system_path in (Path("/usr"), Path("/usr/bin"), TRUSTED_SYSTEM_PYTHON):
        metadata = system_path.lstat()
        if (
            (
                system_path == TRUSTED_SYSTEM_PYTHON
                and not stat.S_ISREG(metadata.st_mode)
            )
            or (
                system_path != TRUSTED_SYSTEM_PYTHON
                and not stat.S_ISDIR(metadata.st_mode)
            )
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or (system_path == TRUSTED_SYSTEM_PYTHON and metadata.st_nlink != 1)
            or (
                system_path == TRUSTED_SYSTEM_PYTHON
                and metadata.st_mode & (stat.S_ISUID | stat.S_ISGID)
            )
        ):
            raise SafeEnvError("trusted system Python path is unsafe")
    _validate_root_owned_tree(TRUSTED_PLATFORM_ROOT / ".venv_platform")
    _validate_root_owned_tree(TRUSTED_PLATFORM_ROOT / "python_packages")
    for name in TRUSTED_DB_TOOLS:
        target = TRUSTED_PLATFORM_ROOT / "tools" / name
        metadata = target.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID)
        ):
            raise SafeEnvError("approved live QA DB tool metadata is unsafe")


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely read platform dotenv data and exec approved commands."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    value = commands.add_parser("print-public-value")
    value.add_argument("name", choices=sorted(PUBLIC_VALUE_NAMES))
    commands.add_parser("validate-runtime")
    export_values = commands.add_parser("export-b64")
    export_values.add_argument("--path", required=True, type=Path)
    execute = commands.add_parser("exec")
    execute.add_argument("--pythonpath", required=True, type=Path)
    execute.add_argument("argv", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def _emit_base64_assignments(values: dict[str, str]) -> None:
    for key in sorted(values):
        encoded = base64.b64encode(values[key].encode("utf-8")).decode("ascii")
        print(f"{key}\t{encoded}")


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "export-b64":
            _emit_base64_assignments(load_env_file(args.path))
            return 0
        if os.geteuid() != 0:
            raise SafeEnvError("safe production environment access requires root")
        if args.command == "validate-runtime":
            validate_trusted_runtime()
            return 0
        values = parse_dotenv(read_production_env_bytes())
        if args.command == "print-public-value":
            value = values.get(args.name)
            if value is None or "\n" in value or "\r" in value:
                raise SafeEnvError("required public production setting is unavailable")
            print(value)
            return 0
        command = list(args.argv)
        if command and command[0] == "--":
            command = command[1:]
        validate_trusted_command(command, pythonpath=args.pythonpath)
        # Executable and script are the exact root-controlled allowlist above.
        os.execve(  # nosec B606
            command[0],
            command,
            clean_child_environment(values, pythonpath=args.pythonpath),
        )
    except SafeEnvError as exc:
        print(f"Safe platform environment refused: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Safe platform environment operation failed.", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
