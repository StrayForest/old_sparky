#!/usr/bin/env python3
"""Bind a cold release venv to measured builder size and disk headroom."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import zipfile


SCHEMA = 1
OVERHEAD_BYTES = 128 * 1024 * 1024
MAX_MEASURED_BYTES = 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ENTRIES = 200_000
INPUTS = (
    "requirements-platform.txt",
    "requirements-platform.lock.txt",
    "requirements-platform.freeze.txt",
    "wheelhouse/WHEELHOUSE.sha256",
)
EVIDENCE_KEYS = frozenset(
    {
        "schema", "builder_allocated_bytes", "wheelhouse_expanded_bytes",
        "host_overhead_bytes", "inputs_sha256",
    }
)


class CapacityError(ValueError):
    pass


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
        value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns,
    )


def _regular_bytes(path: Path) -> bytes:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o022 or info.st_size <= 0
                or info.st_size > 64 * 1024 * 1024):
            raise CapacityError("input_metadata")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if _identity(opened) != _identity(info):
                raise CapacityError("input_changed")
            chunks: list[bytes] = []
            remaining = opened.st_size + 1
            while remaining and (chunk := os.read(fd, min(1024 * 1024, remaining))):
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
            named = path.lstat()
            if _identity(opened) != _identity(after) or _identity(opened) != _identity(named):
                raise CapacityError("input_changed")
            raw = b"".join(chunks)
            if len(raw) != opened.st_size:
                raise CapacityError("input_changed")
            return raw
        finally:
            os.close(fd)
    except OSError as exc:
        raise CapacityError("input_metadata") from exc


def input_digest(release: Path) -> str:
    digest = hashlib.sha256(b"oldsparky-python-venv-inputs-v1\0")
    for relative in INPUTS:
        name = relative.encode("ascii")
        raw = _regular_bytes(release / relative)
        digest.update(len(name).to_bytes(2, "big"))
        digest.update(name)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def wheelhouse_expanded_bytes(release: Path) -> int:
    wheelhouse = release / "wheelhouse"
    try:
        directory = wheelhouse.lstat()
        if (not stat.S_ISDIR(directory.st_mode)
                or stat.S_IMODE(directory.st_mode) & 0o022):
            raise CapacityError("wheelhouse_metadata")
        manifest = _regular_bytes(wheelhouse / "WHEELHOUSE.sha256")
        if len(manifest) > 1024 * 1024:
            raise CapacityError("wheelhouse_metadata")
        expected = {}
        for line in manifest.decode("ascii").splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.+!-]+\.whl)", line)
            if match is None or match[2] in expected:
                raise CapacityError("wheelhouse_metadata")
            expected[match[2]] = match[1]
        entries = sorted(wheelhouse.iterdir(), key=lambda item: item.name)
        wheels = [entry for entry in entries if entry.name != "WHEELHOUSE.sha256"]
        if (not 1 <= len(wheels) <= 2048
                or {wheel.name for wheel in wheels} != set(expected)):
            raise CapacityError("wheelhouse_metadata")
    except (OSError, UnicodeError) as exc:
        raise CapacityError("wheelhouse_metadata") from exc
    total = 0
    entry_count = 0
    archive_bytes = 0
    for wheel in wheels:
        try:
            info = wheel.lstat()
            archive_bytes += info.st_size
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) & 0o022
                    or not 1 <= info.st_size <= 256 * 1024 * 1024
                    or archive_bytes > 1024 * 1024 * 1024):
                raise CapacityError("wheelhouse_metadata")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(wheel, flags), "rb") as source:
                if _identity(info) != _identity(os.fstat(source.fileno())):
                    raise CapacityError("wheelhouse_changed")
                digest = hashlib.sha256()
                remaining = info.st_size + 1
                while remaining and (chunk := source.read(min(1024 * 1024, remaining))):
                    digest.update(chunk)
                    remaining -= len(chunk)
                if source.tell() != info.st_size or digest.hexdigest() != expected[wheel.name]:
                    raise CapacityError("wheelhouse_binding")
                source.seek(0)
                with zipfile.ZipFile(source) as archive:
                    zip_entries = archive.infolist()
                    if not zip_entries:
                        raise CapacityError("wheelhouse_metadata")
                    for entry in zip_entries:
                        entry_count += 1
                        if entry_count > MAX_ENTRIES:
                            raise CapacityError("wheelhouse_entries")
                        if not 0 <= entry.file_size <= MAX_EXPANDED_BYTES:
                            raise CapacityError("wheelhouse_size")
                        total += entry.file_size
                        if total > MAX_EXPANDED_BYTES:
                            raise CapacityError("wheelhouse_size")
                if (_identity(info) != _identity(os.fstat(source.fileno()))
                        or _identity(info) != _identity(wheel.lstat())):
                    raise CapacityError("wheelhouse_changed")
        except (OSError, zipfile.BadZipFile) as exc:
            raise CapacityError("wheelhouse_metadata") from exc
    return total


def allocated_bytes(root: Path, *, maximum: int = MAX_MEASURED_BYTES) -> int:
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise CapacityError("venv_metadata") from exc
    if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
            or root_info.st_nlink < 2):
        raise CapacityError("venv_metadata")
    total = root_info.st_blocks * 512
    if total > maximum:
        raise CapacityError("venv_size")
    entries = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise CapacityError("venv_metadata") from exc
        for child in children:
            entries += 1
            if entries > MAX_ENTRIES:
                raise CapacityError("venv_size")
            path = Path(child.path)
            try:
                info = path.lstat()
            except OSError as exc:
                raise CapacityError("venv_metadata") from exc
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise CapacityError("venv_metadata")
            elif not stat.S_ISLNK(info.st_mode):
                raise CapacityError("venv_metadata")
            total += info.st_blocks * 512
            if total > maximum:
                raise CapacityError("venv_size")
    return total


def measure(release: Path, venv: Path) -> dict[str, int | str]:
    return {
        "schema": SCHEMA,
        "builder_allocated_bytes": allocated_bytes(venv),
        "wheelhouse_expanded_bytes": wheelhouse_expanded_bytes(release),
        "host_overhead_bytes": OVERHEAD_BYTES,
        "inputs_sha256": input_digest(release),
    }


def verify_evidence(release: Path) -> dict[str, int | str]:
    try:
        raw = _regular_bytes(release / "RELEASE.json")
        payload = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapacityError("release_metadata") from exc
    if not isinstance(payload, dict):
        raise CapacityError("release_metadata")
    evidence = payload.get("python_venv_install_evidence")
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS:
        raise CapacityError("evidence_schema")
    if (type(evidence.get("schema")) is not int or evidence["schema"] != SCHEMA
            or type(evidence.get("builder_allocated_bytes")) is not int
            or not 1 <= evidence["builder_allocated_bytes"] <= MAX_MEASURED_BYTES
            or type(evidence.get("wheelhouse_expanded_bytes")) is not int
            or not 1 <= evidence["wheelhouse_expanded_bytes"] <= MAX_EXPANDED_BYTES
            or type(evidence.get("host_overhead_bytes")) is not int
            or evidence["host_overhead_bytes"] != OVERHEAD_BYTES
            or not isinstance(evidence.get("inputs_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence["inputs_sha256"]) is None):
        raise CapacityError("evidence_schema")
    if (evidence["inputs_sha256"] != input_digest(release)
            or evidence["wheelhouse_expanded_bytes"] != wheelhouse_expanded_bytes(release)):
        raise CapacityError("evidence_binding")
    return evidence


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CapacityError("release_metadata")
        result[key] = value
    return result


def _main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    measure_parser = sub.add_parser("measure")
    measure_parser.add_argument("--release", type=Path, required=True)
    measure_parser.add_argument("--venv", type=Path, required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--release", type=Path, required=True)
    size_parser = sub.add_parser("size")
    size_parser.add_argument("--venv", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "measure":
            result = measure(args.release, args.venv)
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        elif args.command == "verify":
            result = verify_evidence(args.release)
            print(
                f"{result['builder_allocated_bytes']} "
                f"{result['wheelhouse_expanded_bytes']} "
                f"{result['host_overhead_bytes']}"
            )
        else:
            print(allocated_bytes(args.venv, maximum=MAX_MEASURED_BYTES + OVERHEAD_BYTES))
    except (CapacityError, OSError, ValueError, json.JSONDecodeError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
