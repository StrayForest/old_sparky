#!/usr/bin/env python3
"""Create or verify the artifact-bound platform Python wheelhouse manifest."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
from email.policy import compat32
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import zipfile


MANIFEST_NAME = "WHEELHOUSE.sha256"
WHEEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.+!-]+\.whl$")
PIN_PATTERN = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[([A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*)\])?"
    r"==([A-Za-z0-9][A-Za-z0-9_.+!-]*)$"
)
MAX_WHEEL_BYTES = 256 * 1024 * 1024
MAX_WHEELHOUSE_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_WHEELS = 2048


class WheelhouseError(RuntimeError):
    """An artifact wheelhouse violated its immutable input contract."""


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _safe_regular_file(
    path: Path,
    *,
    label: str,
    expected_mode: int | None,
    max_bytes: int,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise WheelhouseError(f"{label} is unavailable") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or mode & 0o022
        or (expected_mode is not None and mode != expected_mode)
        or metadata.st_size > max_bytes
    ):
        raise WheelhouseError(f"{label} metadata is unsafe")
    return metadata


def _read_pins(path: Path, *, label: str, expected_mode: int | None) -> dict[str, str]:
    _safe_regular_file(
        path,
        label=label,
        expected_mode=expected_mode,
        max_bytes=MAX_MANIFEST_BYTES,
    )
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WheelhouseError(f"{label} is invalid") from exc
    pins: dict[str, str] = {}
    rendered: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            if label in {"Python freeze", "Python lock"} and line:
                raise WheelhouseError(f"{label} must contain only exact package pins")
            continue
        pin_line = line
        hash_parts = line.split(" --hash=")
        if len(hash_parts) > 1:
            if label != "Python lock":
                raise WheelhouseError(f"{label} must not contain package hashes")
            pin_line = hash_parts[0]
            if any(
                re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
                for value in hash_parts[1:]
            ):
                raise WheelhouseError("Python lock contains an invalid package hash")
        elif label == "Python lock":
            raise WheelhouseError("Python lock must hash every package pin")
        match = PIN_PATTERN.fullmatch(pin_line)
        if match is None:
            raise WheelhouseError(f"{label} must contain only exact package pins")
        name = _normalized_name(match.group(1))
        if name in pins:
            raise WheelhouseError(f"{label} contains duplicate packages")
        pins[name] = match.group(3)
        rendered.append(line)
    if not pins:
        raise WheelhouseError(f"{label} contains no package pins")
    if label in {"Python freeze", "Python lock"} and rendered != sorted(rendered):
        raise WheelhouseError(f"{label} is not canonically sorted")
    return pins


def _require_pip_pin(pins: dict[str, str], *, label: str) -> None:
    if "pip" not in pins:
        raise WheelhouseError(f"{label} must contain an exact pip pin")


def _validate_requirements(path: Path) -> dict[str, str]:
    requirements = _read_pins(
        path,
        label="Python requirements",
        expected_mode=None,
    )
    _require_pip_pin(requirements, label="Python requirements")
    return requirements


def _validate_freeze(path: Path) -> dict[str, str]:
    freeze = _read_pins(path, label="Python freeze", expected_mode=0o444)
    _require_pip_pin(freeze, label="Python freeze")
    return freeze


def _validate_lock(path: Path) -> dict[str, str]:
    lock = _read_pins(path, label="Python lock", expected_mode=0o644)
    _require_pip_pin(lock, label="Python lock")
    return lock


def _validate_locked_freeze(lock_path: Path, freeze_path: Path) -> dict[str, str]:
    lock = _validate_lock(lock_path)
    freeze = _validate_freeze(freeze_path)
    if lock != freeze:
        raise WheelhouseError("Python freeze does not exactly match the tracked lock")
    return freeze


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise WheelhouseError("wheelhouse changed while hashing") from exc
    return digest.hexdigest()


def _wheel_files(wheelhouse: Path, *, expected_mode: int | None) -> list[Path]:
    try:
        metadata = wheelhouse.lstat()
        entries = sorted(wheelhouse.iterdir(), key=lambda path: path.name)
        resolved = wheelhouse.resolve(strict=True)
    except OSError as exc:
        raise WheelhouseError("wheelhouse is unavailable") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or resolved != wheelhouse.absolute()
        or mode & 0o022
        or (expected_mode is not None and mode != expected_mode)
    ):
        raise WheelhouseError("wheelhouse directory metadata is unsafe")
    wheels: list[Path] = []
    total_size = 0
    for entry in entries:
        if entry.name == MANIFEST_NAME:
            continue
        if WHEEL_NAME_PATTERN.fullmatch(entry.name) is None:
            raise WheelhouseError("wheelhouse contains an unexpected entry")
        item = _safe_regular_file(
            entry,
            label="wheelhouse entry",
            expected_mode=0o444 if expected_mode is not None else None,
            max_bytes=MAX_WHEEL_BYTES,
        )
        total_size += item.st_size
        if total_size > MAX_WHEELHOUSE_BYTES:
            raise WheelhouseError("wheelhouse exceeds its size bound")
        wheels.append(entry)
        if len(wheels) > MAX_WHEELS:
            raise WheelhouseError("wheelhouse exceeds its file-count bound")
    if not wheels:
        raise WheelhouseError("wheelhouse is empty")
    return wheels


def _wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as wheel:
            metadata_names = [
                name
                for name in wheel.namelist()
                if re.fullmatch(r"[^/]+\.dist-info/METADATA", name) is not None
            ]
            if len(metadata_names) != 1:
                raise WheelhouseError("wheel metadata layout is invalid")
            info = wheel.getinfo(metadata_names[0])
            if info.file_size > MAX_METADATA_BYTES or info.flag_bits & 0x1:
                raise WheelhouseError("wheel metadata is unsafe")
            with wheel.open(info) as metadata_file:
                raw = metadata_file.read(MAX_METADATA_BYTES + 1)
            if len(raw) > MAX_METADATA_BYTES:
                raise WheelhouseError("wheel metadata is too large")
    except WheelhouseError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        raise WheelhouseError("wheel archive is invalid") from exc
    message = BytesParser(policy=compat32).parsebytes(raw, headersonly=True)
    names = message.get_all("Name", [])
    versions = message.get_all("Version", [])
    if len(names) != 1 or len(versions) != 1:
        raise WheelhouseError("wheel metadata identity is invalid")
    name = names[0].strip()
    version = versions[0].strip()
    if PIN_PATTERN.fullmatch(f"{name}=={version}") is None:
        raise WheelhouseError("wheel metadata identity is invalid")
    return _normalized_name(name), version


def _validate_resolved_set(wheels: list[Path], freeze: dict[str, str]) -> None:
    wheel_packages: dict[str, str] = {}
    for wheel in wheels:
        name, version = _wheel_identity(wheel)
        if name in wheel_packages:
            raise WheelhouseError("wheelhouse contains duplicate package wheels")
        wheel_packages[name] = version
    if wheel_packages != freeze:
        raise WheelhouseError("wheelhouse does not exactly match the Python freeze")


def _validate_requirement_set(
    requirements: dict[str, str], freeze: dict[str, str]
) -> None:
    if any(freeze.get(name) != version for name, version in requirements.items()):
        raise WheelhouseError("Python freeze does not satisfy the exact requirements")


def create_manifest(
    wheelhouse: Path,
    requirements: Path,
    lock_path: Path,
    freeze_path: Path,
) -> None:
    requirement_pins = _validate_requirements(requirements)
    freeze = _validate_locked_freeze(lock_path, freeze_path)
    _validate_requirement_set(requirement_pins, freeze)
    manifest = wheelhouse / MANIFEST_NAME
    if manifest.exists() or manifest.is_symlink():
        raise WheelhouseError("wheelhouse manifest already exists")
    wheels = _wheel_files(wheelhouse, expected_mode=None)
    _validate_resolved_set(wheels, freeze)
    records = [f"{_sha256(wheel)}  {wheel.name}\n" for wheel in wheels]
    for wheel in wheels:
        os.chmod(wheel, 0o444)  # nosec B103
    descriptor = os.open(
        manifest,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o444,
    )
    try:
        raw = "".join(records).encode("ascii")
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise WheelhouseError("wheelhouse manifest could not be written")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(manifest, 0o444)  # nosec B103
    os.chmod(wheelhouse, 0o555)  # nosec B103


def verify_manifest(
    wheelhouse: Path,
    requirements: Path,
    lock_path: Path,
    freeze_path: Path,
) -> None:
    requirement_pins = _validate_requirements(requirements)
    freeze = _validate_locked_freeze(lock_path, freeze_path)
    _validate_requirement_set(requirement_pins, freeze)
    wheels = _wheel_files(wheelhouse, expected_mode=0o555)
    _validate_resolved_set(wheels, freeze)
    manifest = wheelhouse / MANIFEST_NAME
    _safe_regular_file(
        manifest,
        label="wheelhouse manifest",
        expected_mode=0o444,
        max_bytes=MAX_MANIFEST_BYTES,
    )
    try:
        raw = manifest.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise WheelhouseError("wheelhouse manifest is invalid") from exc
    expected: dict[str, str] = {}
    for line in raw.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.+!-]+\.whl)", line)
        if match is None or match.group(2) in expected:
            raise WheelhouseError("wheelhouse manifest format is invalid")
        expected[match.group(2)] = match.group(1)
    if list(expected) != sorted(expected) or set(expected) != {
        wheel.name for wheel in wheels
    }:
        raise WheelhouseError("wheelhouse manifest file set is incomplete")
    for wheel in wheels:
        if _sha256(wheel) != expected[wheel.name]:
            raise WheelhouseError("wheelhouse checksum mismatch")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the release Python wheelhouse."
    )
    parser.add_argument("command", choices=("create", "verify"))
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--freeze", required=True, type=Path)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise WheelhouseError("wheelhouse validation requires root")
        if args.command == "create":
            create_manifest(
                args.wheelhouse,
                args.requirements,
                args.lock,
                args.freeze,
            )
        else:
            verify_manifest(
                args.wheelhouse,
                args.requirements,
                args.lock,
                args.freeze,
            )
        return 0
    except WheelhouseError as exc:
        print(f"Release wheelhouse refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
