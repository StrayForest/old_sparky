#!/usr/bin/env python3
"""Bounded source-bound repair for verified LiveQA bytecode-only drift."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import errno
import hashlib
import importlib._bootstrap_external
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import time
from typing import Iterator

sys.dont_write_bytecode = True

APP_DIR = Path("/opt/oldsparky/platform")
LIVEQA_ROOT = Path("/root/.oldsparky/liveqa")
PLAN_ROOT = LIVEQA_ROOT / "payload-repair-plans"
QUARANTINE_ROOT = LIVEQA_ROOT / "payload-repair-quarantine"
TOOL_ROOT = Path("/root/.oldsparky/liveqa/payload-repair-tools")
RELEASE_LOCK_PATH = Path("/run/lock/oldsparky-platform-release.lock")
LIVEQA_SYSTEMD_UNIT = "oldsparky-liveqa-browser.service"
RUNTIME_INSTALLER_RELATIVE = Path("tools/platform_live_qa_runtime_install.py")

SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
PYTHON_CACHE_TAG = "cpython-312"
CACHE_DIR_RELATIVE = "platform/tools/__pycache__"
EXTRA_FILE_RELATIVES = (
    "platform/tools/__pycache__/platform_live_qa_guard.cpython-312.pyc",
    "platform/tools/__pycache__/platform_live_qa_machine_lock.cpython-312.pyc",
)
PLAN_SCHEMA = 1
MAX_MANIFEST_BYTES = 256 * 1024
MAX_PLAN_BYTES = 32 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_PAYLOAD_FILES = 200_000
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024 * 1024


class RepairError(RuntimeError):
    """A closed, non-sensitive refusal from the exact-payload repair."""

    ALLOWED_REASONS = frozenset(
        {
            "source_mismatch",
            "runner_mismatch",
            "lock_unavailable",
            "pending_operation",
            "qa_busy",
            "pointer_mismatch",
            "manifest_mismatch",
            "unexpected_tree",
            "extra_identity_mismatch",
            "plan_unavailable",
            "plan_mismatch",
            "quarantine_exists",
            "validation_failed",
            "rollback_failed",
            "source_bundle_mismatch",
            "receipt_write_failed",
        }
    )

    def __init__(self, reason: str) -> None:
        if reason not in self.ALLOWED_REASONS:
            reason = "unexpected_tree"
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class RepairPaths:
    """Fixed production roots, injectable only by owner contract tests."""

    app_dir: Path = APP_DIR
    liveqa_root: Path = LIVEQA_ROOT
    plan_root: Path = PLAN_ROOT
    quarantine_root: Path = QUARANTINE_ROOT
    tool_root: Path = TOOL_ROOT


DEFAULT_PATHS = RepairPaths()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RepairError("manifest_mismatch")
        result[key] = value
    return result


def _metadata(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise RepairError("pointer_mismatch") from exc


def _check_dir(path: Path, *, exact_mode: int | None = None) -> os.stat_result:
    metadata = _metadata(path)
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink < 2
        or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        or mode & 0o022
        or (exact_mode is not None and mode != exact_mode)
    ):
        raise RepairError("pointer_mismatch")
    return metadata


def _open_regular(path: Path, *, maximum: int = MAX_FILE_BYTES) -> tuple[int, os.stat_result]:
    named = _metadata(path)
    if (
        not stat.S_ISREG(named.st_mode)
        or named.st_uid != 0
        or named.st_gid != 0
        or named.st_nlink != 1
        or stat.S_IMODE(named.st_mode) & 0o022
        or named.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        or named.st_size < 0
        or named.st_size > maximum
    ):
        raise RepairError("manifest_mismatch")
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(fd)
    except OSError as exc:
        raise RepairError("manifest_mismatch") from exc
    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
        named.st_dev,
        named.st_ino,
        named.st_size,
        named.st_mtime_ns,
    ):
        os.close(fd)
        raise RepairError("manifest_mismatch")
    return fd, opened


def _read_regular(path: Path, *, maximum: int = MAX_FILE_BYTES) -> tuple[bytes, os.stat_result]:
    fd, metadata = _open_regular(path, maximum=maximum)
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise RepairError("manifest_mismatch")
            chunks.append(chunk)
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ) or total != metadata.st_size:
            raise RepairError("manifest_mismatch")
    finally:
        os.close(fd)
    return b"".join(chunks), metadata


def _digest_file(path: Path) -> tuple[str, os.stat_result, bytes]:
    data, metadata = _read_regular(path)
    return hashlib.sha256(data).hexdigest(), metadata, data


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _identity(metadata: os.stat_result) -> dict[str, int]:
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size": metadata.st_size,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "nlink": metadata.st_nlink,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
    }


def _sha_arg(value: str, *, reason: str) -> None:
    if not SOURCE_SHA_RE.fullmatch(value):
        raise RepairError(reason)


def _release_record(app_dir: Path, pointer_name: str) -> dict[str, object]:
    releases = app_dir / "releases"
    _check_dir(app_dir)
    _check_dir(releases)
    pointer = app_dir / pointer_name
    pointer_metadata = _metadata(pointer)
    if (
        not stat.S_ISLNK(pointer_metadata.st_mode)
        or pointer_metadata.st_uid != 0
        or pointer_metadata.st_gid != 0
        or pointer_metadata.st_nlink != 1
    ):
        raise RepairError("pointer_mismatch")
    try:
        target = pointer.resolve(strict=True)
    except OSError as exc:
        raise RepairError("pointer_mismatch") from exc
    if target.parent != releases or not SLUG_RE.fullmatch(target.name):
        raise RepairError("pointer_mismatch")
    release_directory = _check_dir(target)
    raw, release_metadata = _read_regular(target / "RELEASE.json", maximum=16 * 1024)
    try:
        metadata = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RepairError("pointer_mismatch") from exc
    source = metadata.get("source_git_commit") if isinstance(metadata, dict) else None
    slug = metadata.get("release_slug") if isinstance(metadata, dict) else None
    if not isinstance(source, str) or not SOURCE_SHA_RE.fullmatch(source):
        raise RepairError("pointer_mismatch")
    if slug != target.name:
        raise RepairError("pointer_mismatch")
    return {
        "slug": target.name,
        "source_sha": source,
        "pointer_identity": _identity(pointer_metadata),
        "release_identity": _identity(release_directory),
        "release_json_identity": _identity(release_metadata),
        "release_json_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _load_active_manifest(paths: RepairPaths, expected_source_sha: str) -> tuple[dict[str, object], bytes]:
    root = paths.liveqa_root
    _check_dir(root, exact_mode=0o700)
    _check_dir(root / "releases", exact_mode=0o755)
    active_pointer = root / "active"
    pointer_metadata = _metadata(active_pointer)
    if (
        not stat.S_ISLNK(pointer_metadata.st_mode)
        or pointer_metadata.st_uid != 0
        or pointer_metadata.st_gid != 0
        or pointer_metadata.st_nlink != 1
    ):
        raise RepairError("pointer_mismatch")
    payload_path = root / "releases" / expected_source_sha
    try:
        resolved = active_pointer.resolve(strict=True)
    except OSError as exc:
        raise RepairError("pointer_mismatch") from exc
    if resolved != payload_path:
        raise RepairError("pointer_mismatch")
    _check_dir(payload_path, exact_mode=0o555)
    raw, _ = _read_regular(root / "active-manifest.json", maximum=MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RepairError("manifest_mismatch") from exc
    expected_keys = {
        "version",
        "source_sha",
        "release_slug",
        "payload",
        "payload_tree_sha256",
        "files",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != expected_keys
        or manifest.get("version") != 1
        or manifest.get("source_sha") != expected_source_sha
        or not isinstance(manifest.get("release_slug"), str)
        or not SLUG_RE.fullmatch(manifest["release_slug"])
        or manifest.get("payload") != str(payload_path)
        or not isinstance(manifest.get("payload_tree_sha256"), str)
        or not SHA256_RE.fullmatch(manifest["payload_tree_sha256"])
        or not isinstance(manifest.get("files"), dict)
        or not manifest["files"]
    ):
        raise RepairError("manifest_mismatch")
    for relative, digest in manifest["files"].items():
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
        ):
            raise RepairError("manifest_mismatch")
    current = _release_record(paths.app_dir, "current")
    previous = _release_record(paths.app_dir, "previous")
    if (
        current["source_sha"] != expected_source_sha
        or current["slug"] != manifest["release_slug"]
    ):
        raise RepairError("source_mismatch")
    if previous["source_sha"] == expected_source_sha:
        raise RepairError("pointer_mismatch")
    return manifest, raw


def _expected_links(payload: Path, files: dict[str, str]) -> dict[str, str]:
    provider_path = payload / "runtime-provider.json"
    if "runtime-provider.json" not in files:
        return {}
    raw, _ = _read_regular(provider_path, maximum=MAX_MANIFEST_BYTES)
    try:
        provider = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RepairError("manifest_mismatch") from exc
    if not isinstance(provider, dict) or provider.get("version") != 2:
        return {}
    if provider.get("provider_sha") != payload.name or provider.get("source_sha") != payload.name:
        return {}
    links = provider.get("engine_links")
    if not isinstance(links, dict):
        raise RepairError("manifest_mismatch")
    result: dict[str, str] = {}
    for relative, target in links.items():
        if (
            not isinstance(relative, str)
            or relative.startswith("/")
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not isinstance(target, str)
            or not target
        ):
            raise RepairError("manifest_mismatch")
        result[f"provider-runtime/{relative}"] = target
    return result


def _scan_payload(payload: Path, files_manifest: dict[str, str]) -> tuple[
    dict[str, str], dict[str, str], set[str], dict[str, str]
]:
    actual_files: dict[str, str] = {}
    actual_links: dict[str, str] = {}
    directories: set[str] = set()
    expected_links = _expected_links(payload, files_manifest)
    entry_count = 0
    total_bytes = 0

    def visit(directory: Path, prefix: str) -> None:
        nonlocal entry_count, total_bytes
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise RepairError("unexpected_tree") from exc
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            path = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RepairError("unexpected_tree") from exc
            entry_count += 1
            if entry_count > MAX_PAYLOAD_FILES:
                raise RepairError("unexpected_tree")
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path)
                if (
                    relative not in expected_links
                    or target != expected_links[relative]
                    or not relative.startswith("provider-runtime/browsers/")
                    or metadata.st_uid != 0
                    or metadata.st_gid != 0
                    or metadata.st_nlink != 1
                    or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
                ):
                    raise RepairError("unexpected_tree")
                actual_links[relative] = target
            elif stat.S_ISDIR(metadata.st_mode):
                mode = stat.S_IMODE(metadata.st_mode)
                if (
                    metadata.st_uid != 0
                    or metadata.st_gid != 0
                    or metadata.st_nlink < 2
                    or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
                    or mode & 0o022
                ):
                    raise RepairError("unexpected_tree")
                directories.add(relative)
                visit(path, relative)
            elif stat.S_ISREG(metadata.st_mode):
                total_bytes += metadata.st_size
                if total_bytes > MAX_PAYLOAD_BYTES:
                    raise RepairError("unexpected_tree")
                fd, opened = _open_regular(path)
                digest = hashlib.sha256()
                try:
                    while True:
                        chunk = os.read(fd, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                    after = os.fstat(fd)
                    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                        opened.st_dev,
                        opened.st_ino,
                        opened.st_size,
                        opened.st_mtime_ns,
                    ):
                        raise RepairError("unexpected_tree")
                finally:
                    os.close(fd)
                actual_files[relative] = digest.hexdigest()
            else:
                raise RepairError("unexpected_tree")

    visit(payload, "")
    return actual_files, actual_links, directories, expected_links


def _expected_directories(files: dict[str, str], links: set[str]) -> set[str]:
    result: set[str] = set()
    for relative in {*files, *links}:
        parts = PurePosixPath(relative).parts
        for size in range(1, len(parts)):
            result.add(PurePosixPath(*parts[:size]).as_posix())
    return result


def _manifest_tree_digest(
    files: dict[str, str],
    links: dict[str, str],
    directories: set[str],
) -> str:
    """Rebuild the canonical digest from files, links and observed directories."""

    required_directories = _expected_directories(files, set(links))
    if not required_directories.issubset(directories):
        raise RepairError("manifest_mismatch")
    if set(files) & set(links) or (set(files) | set(links)) & directories:
        raise RepairError("manifest_mismatch")
    digest = hashlib.sha256()
    for relative in sorted(
        (*files.keys(), *links.keys(), *directories),
        key=lambda value: PurePosixPath(value).parts,
    ):
        encoded = relative.encode("utf-8")
        if relative in links:
            digest.update(encoded + b"\0l\0" + links[relative].encode("utf-8") + b"\0")
        elif relative in directories:
            digest.update(encoded + b"\0d\0")
        else:
            digest.update(encoded + b"\0f\0" + bytes.fromhex(files[relative]))
    return digest.hexdigest()


def _pyc_is_for_source(payload: Path, relative: str, data: bytes) -> bool:
    if sys.implementation.name != "cpython" or sys.implementation.cache_tag != PYTHON_CACHE_TAG:
        return False
    parts = PurePosixPath(relative).parts
    source_relative = PurePosixPath(
        *parts[:-2],
        parts[-1].removesuffix(f".{PYTHON_CACHE_TAG}.pyc") + ".py",
    ).as_posix()
    source_path = payload / source_relative
    try:
        source, source_metadata = _read_regular(source_path)
        code = compile(
            source,
            str(source_path),
            "exec",
            dont_inherit=True,
            optimize=sys.flags.optimize,
        )
        expected = importlib._bootstrap_external._code_to_timestamp_pyc(
            code,
            int(source_metadata.st_mtime),
            source_metadata.st_size,
        )
    except (OSError, SyntaxError, ValueError, TypeError):
        return False
    return data == expected


def _extra_records(
    payload: Path,
    actual_files: dict[str, str],
    expected_files: dict[str, str],
) -> tuple[list[dict[str, object]], dict[str, int]]:
    extras = sorted(set(actual_files) - set(expected_files))
    missing = set(expected_files) - set(actual_files)
    changed = {path for path in set(expected_files) & set(actual_files) if expected_files[path] != actual_files[path]}
    if missing or changed or tuple(extras) != tuple(sorted(EXTRA_FILE_RELATIVES)):
        raise RepairError("unexpected_tree")
    cache_path = payload / CACHE_DIR_RELATIVE
    cache_metadata = _metadata(cache_path)
    if (
        not stat.S_ISDIR(cache_metadata.st_mode)
        or cache_metadata.st_uid != 0
        or cache_metadata.st_gid != 0
        or stat.S_IMODE(cache_metadata.st_mode) != 0o755
        or cache_metadata.st_nlink != 2
        or cache_metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
    ):
        raise RepairError("extra_identity_mismatch")
    contents = sorted(entry.name for entry in os.scandir(cache_path))
    expected_names = sorted(PurePosixPath(path).name for path in EXTRA_FILE_RELATIVES)
    if contents != expected_names:
        raise RepairError("unexpected_tree")
    records: list[dict[str, object]] = []
    for relative in sorted(EXTRA_FILE_RELATIVES):
        path = payload / relative
        digest, metadata, data = _digest_file(path)
        if (
            digest != actual_files[relative]
            or stat.S_IMODE(metadata.st_mode) != 0o644
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
            or not _pyc_is_for_source(payload, relative, data)
        ):
            raise RepairError("extra_identity_mismatch")
        records.append({"relative_path": relative, **_identity(metadata), "sha256": digest})
    return records, _identity(cache_metadata)


def _verify_tree_delta(paths: RepairPaths, manifest: dict[str, object]) -> tuple[list[dict[str, object]], dict[str, int]]:
    payload = Path(str(manifest["payload"]))
    expected_files = manifest["files"]
    assert isinstance(expected_files, dict)
    actual_files, actual_links, directories, expected_links = _scan_payload(payload, expected_files)
    if actual_links != expected_links:
        raise RepairError("unexpected_tree")
    if CACHE_DIR_RELATIVE not in directories:
        raise RepairError("unexpected_tree")
    manifest_directories = directories - {CACHE_DIR_RELATIVE}
    if _manifest_tree_digest(expected_files, expected_links, manifest_directories) != manifest.get(
        "payload_tree_sha256"
    ):
        raise RepairError("manifest_mismatch")
    extra_records, cache_identity = _extra_records(payload, actual_files, expected_files)
    return extra_records, cache_identity


def _ensure_private_dir(path: Path) -> os.stat_result:
    if not os.path.lexists(path):
        try:
            path.mkdir(mode=0o700)
            os.chown(path, 0, 0)
        except FileExistsError:
            pass
        except OSError as exc:
            raise RepairError("plan_unavailable") from exc
    return _check_dir(path, exact_mode=0o700)


def _bundle_root(paths: RepairPaths, runner_source_sha: str) -> Path:
    # Production runs the helper from stdin under Python -I, so __file__ and
    # cwd cannot select a trusted source directory.  Tests pass a private
    # direct bundle root explicitly.
    if paths.tool_root == TOOL_ROOT:
        return TOOL_ROOT / runner_source_sha
    return paths.tool_root


def _verify_source_bundle(paths: RepairPaths, runner_source_sha: str) -> dict[str, object]:
    _sha_arg(runner_source_sha, reason="runner_mismatch")
    tool_root = _bundle_root(paths, runner_source_sha)
    _check_dir(tool_root, exact_mode=0o700)
    bundle_path = tool_root / "repair-bundle.json"
    raw, _ = _read_regular(bundle_path, maximum=4096)
    try:
        bundle = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RepairError("source_bundle_mismatch") from exc
    expected = {
        "schema",
        "runner_source_sha",
        "repair_tool_sha256",
        "machine_lock_sha256",
        "guard_sha256",
    }
    if (
        not isinstance(bundle, dict)
        or set(bundle) != expected
        or bundle.get("schema") != 1
        or bundle.get("runner_source_sha") != runner_source_sha
        or not isinstance(bundle.get("repair_tool_sha256"), str)
        or not SHA256_RE.fullmatch(bundle["repair_tool_sha256"])
        or not isinstance(bundle.get("machine_lock_sha256"), str)
        or not SHA256_RE.fullmatch(bundle["machine_lock_sha256"])
        or not isinstance(bundle.get("guard_sha256"), str)
        or not SHA256_RE.fullmatch(bundle["guard_sha256"])
    ):
        raise RepairError("source_bundle_mismatch")
    for name, expected_sha in (
        ("platform_live_qa_payload_bytecode_repair.py", bundle["repair_tool_sha256"]),
        ("platform_live_qa_machine_lock.py", bundle["machine_lock_sha256"]),
        ("platform_live_qa_guard.py", bundle["guard_sha256"]),
    ):
        digest, metadata, _ = _digest_file(tool_root / name)
        if digest != expected_sha or metadata.st_mode & 0o022:
            raise RepairError("source_bundle_mismatch")
    entries = sorted(entry.name for entry in os.scandir(tool_root))
    if entries != sorted(
        (
            "repair-bundle.json",
            "platform_live_qa_payload_bytecode_repair.py",
            "platform_live_qa_machine_lock.py",
            "platform_live_qa_guard.py",
        )
    ):
        raise RepairError("source_bundle_mismatch")
    return bundle


def _require_release_lock() -> None:
    try:
        lock_root = RELEASE_LOCK_PATH.parent.lstat()
        lock_metadata = RELEASE_LOCK_PATH.lstat()
        if (
            not stat.S_ISDIR(lock_root.st_mode)
            or lock_root.st_uid != 0
            or stat.S_ISLNK(lock_metadata.st_mode)
            or not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != 0
            or lock_metadata.st_gid != 0
            or lock_metadata.st_nlink != 1
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
        ):
            raise RepairError("lock_unavailable")
        device = f"{os.major(lock_metadata.st_dev):x}:{os.minor(lock_metadata.st_dev):x}"
        inode = str(lock_metadata.st_ino)
        lock_lines = Path("/proc/locks").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise RepairError("lock_unavailable") from exc
    pid = os.getpid()
    while pid > 1:
        try:
            executable = Path(f"/proc/{pid}/exe").resolve()
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            parent_pid = int(fields[1])
        except (OSError, UnicodeError, ValueError, IndexError) as exc:
            raise RepairError("lock_unavailable") from exc
        if executable == Path("/usr/bin/flock"):
            for line in lock_lines:
                parts = line.split()
                if len(parts) < 6 or parts[1:5] != ["FLOCK", "ADVISORY", "WRITE", str(pid)]:
                    continue
                lock_device = parts[5].split(":")
                if len(lock_device) != 3:
                    continue
                current_device = f"{int(lock_device[0], 16):x}:{int(lock_device[1], 16):x}"
                if current_device == device and lock_device[2].lstrip("0") == inode.lstrip("0"):
                    return
        if parent_pid == pid:
            break
        pid = parent_pid
    raise RepairError("lock_unavailable")


def _load_verified_guard(paths: RepairPaths, runner_source_sha: str) -> dict[str, object]:
    """Execute the verified guard source, with its exact verified lock owner."""

    _verify_source_bundle(paths, runner_source_sha)
    tool_root = _bundle_root(paths, runner_source_sha)
    lock_path = tool_root / "platform_live_qa_machine_lock.py"
    guard_path = tool_root / "platform_live_qa_guard.py"
    lock_source, _ = _read_regular(lock_path, maximum=64 * 1024)
    guard_source, _ = _read_regular(guard_path, maximum=2 * 1024 * 1024)
    lock_module = type(sys)("platform_live_qa_machine_lock")
    lock_module.__file__ = str(lock_path)
    namespace: dict[str, object] = {"__name__": "_oldsparky_verified_liveqa_guard", "__file__": str(guard_path)}
    previous_lock_module = sys.modules.get("platform_live_qa_machine_lock")
    sys.modules["platform_live_qa_machine_lock"] = lock_module
    try:
        exec(compile(lock_source, str(lock_path), "exec", dont_inherit=True), lock_module.__dict__)
        exec(compile(guard_source, str(guard_path), "exec", dont_inherit=True), namespace)
    except BaseException as exc:
        raise RepairError("source_bundle_mismatch") from exc
    finally:
        if previous_lock_module is None:
            sys.modules.pop("platform_live_qa_machine_lock", None)
        else:
            sys.modules["platform_live_qa_machine_lock"] = previous_lock_module
    return namespace


def _open_machine_lock(paths: RepairPaths, runner_source_sha: str) -> int:
    """Acquire the canonical lock owner loaded from verified runner source."""

    namespace = _load_verified_guard(paths, runner_source_sha)
    module = namespace.get("_machine_lock_module")
    opener = getattr(module, "open_machine_lock", None)
    if not callable(opener):
        raise RepairError("source_bundle_mismatch")
    try:
        descriptor = opener()
    except BaseException as exc:
        raise RepairError("qa_busy") from exc
    if type(descriptor) is not int or descriptor < 0:
        raise RepairError("qa_busy")
    return descriptor


@contextmanager
def _acquire_locks(paths: RepairPaths, runner_source_sha: str) -> Iterator[None]:
    """Acquire the canonical release lock first, then the canonical QA lock."""

    _require_release_lock()
    descriptor = _open_machine_lock(paths, runner_source_sha)
    try:
        yield
    finally:
        os.close(descriptor)


def _assert_no_pending_operations(paths: RepairPaths) -> None:
    shared = paths.app_dir / "shared"
    _check_dir(shared)
    for name in (".release-operation.json", ".release-quiesce.json", ".release-systemd-state.json"):
        if os.path.lexists(shared / name):
            raise RepairError("pending_operation")


def _assert_no_active_qa(paths: RepairPaths, runner_source_sha: str) -> None:
    try:
        namespace = _load_verified_guard(paths, runner_source_sha)
        idle = namespace.get("assert_liveqa_idle")
        collected = namespace.get("_assert_runtime_unit_collected")
        if not callable(idle) or not callable(collected):
            raise RepairError("source_bundle_mismatch")
        idle()
        collected()
    except RepairError:
        raise
    except BaseException as exc:
        raise RepairError("qa_busy") from exc


def _plan_location(paths: RepairPaths, plan_sha256: str) -> Path:
    if not SHA256_RE.fullmatch(plan_sha256):
        raise RepairError("plan_mismatch")
    return paths.plan_root / f"{plan_sha256}.json"


def _unlink_just_created(path: Path, expected: os.stat_result) -> None:
    """Remove one partial file only when it is still our original inode."""

    parent_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (expected.st_dev, expected.st_ino):
            raise RepairError("plan_unavailable")
        os.unlink(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _write_exclusive(path: Path, raw: bytes, *, mode: int = 0o600) -> os.stat_result | None:
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
    except FileExistsError:
        existing, metadata = _read_regular(path, maximum=MAX_PLAN_BYTES)
        if existing != raw or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != mode:
            raise RepairError("plan_mismatch")
        return None
    except OSError as exc:
        raise RepairError("plan_unavailable") from exc
    opened = os.fstat(fd)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise RepairError("plan_unavailable")
            view = view[written:]
        os.fchown(fd, 0, 0)
        os.fchmod(fd, mode)
        os.fsync(fd)
        written_metadata = os.fstat(fd)
    except BaseException as exc:
        try:
            _unlink_just_created(path, opened)
        except BaseException as cleanup_exc:
            raise RepairError("plan_unavailable") from cleanup_exc
        raise RepairError("plan_unavailable") from exc
    finally:
        os.close(fd)
    try:
        _fsync_dir(path.parent)
    except OSError as exc:
        try:
            _unlink_just_created(path, opened)
        except BaseException as cleanup_exc:
            raise RepairError("plan_unavailable") from cleanup_exc
        raise RepairError("plan_unavailable") from exc
    return written_metadata


def _publish_initial_receipt(
    path: Path,
    value: dict[str, object],
    expected_parent_identity: dict[str, object],
) -> None:
    """Publish the first intent atomically; never expose a partial receipt."""

    parent = path.parent
    parent_before = _check_dir(parent, exact_mode=0o700)
    if not _same_directory_identity(parent_before, expected_parent_identity):
        raise RepairError("receipt_write_failed")
    raw = _canonical_json(value)
    temp = parent / f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    temp_metadata = _write_exclusive(temp, raw)
    if temp_metadata is None:
        raise RepairError("receipt_write_failed")
    temp_raw, actual = _read_regular(temp, maximum=MAX_PLAN_BYTES)
    if (
        temp_raw != raw
        or _identity(actual) != _identity(temp_metadata)
        or actual.st_uid != 0
        or actual.st_gid != 0
        or stat.S_IMODE(actual.st_mode) != 0o600
    ):
        raise RepairError("receipt_write_failed")
    parent_fd = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened_parent = os.fstat(parent_fd)
        current_temp = os.stat(temp.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not _same_directory_identity(opened_parent, expected_parent_identity)
            or not _same_identity(current_temp, _identity(temp_metadata))
        ):
            raise RepairError("receipt_write_failed")
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if renameat2(
            parent_fd,
            os.fsencode(temp.name),
            parent_fd,
            os.fsencode(path.name),
            1,  # RENAME_NOREPLACE
        ) != 0:
            raise RepairError("receipt_write_failed")
        os.fsync(parent_fd)
    except OSError as exc:
        raise RepairError("receipt_write_failed") from exc
    finally:
        os.close(parent_fd)


def _remove_unstarted_quarantine(
    quarantine: Path,
    expected_parent_identity: dict[str, object],
    expected_device: int,
) -> None:
    """Discard only an untouched, plan-named quarantine before any move."""

    parent_fd = os.open(
        quarantine.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    target_fd: int | None = None
    try:
        parent = os.fstat(parent_fd)
        if not _same_directory_identity(parent, expected_parent_identity):
            raise RepairError("quarantine_exists")
        target_fd = os.open(
            quarantine.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        target = os.fstat(target_fd)
        if (
            target.st_dev != expected_device
            or target.st_uid != 0
            or target.st_gid != 0
            or stat.S_IMODE(target.st_mode) != 0o700
            or target.st_nlink != 2
        ):
            raise RepairError("quarantine_exists")
        names = sorted(os.listdir(target_fd))
        if names:
            if len(names) != 1 or not RECEIPT_TEMP_RE.fullmatch(names[0]):
                raise RepairError("quarantine_exists")
            temp_path = quarantine / names[0]
            temp_raw, temp_stat = _read_regular(temp_path, maximum=MAX_PLAN_BYTES)
            quarantine_identity = _identity(target)
            plan_sha256 = quarantine.name
            expected_initial = _canonical_json(
                {
                    "schema": 1,
                    "plan_sha256": plan_sha256,
                    "phase": "prepared",
                    "quarantine_identity": quarantine_identity,
                }
            )
            if (
                not stat.S_ISREG(temp_stat.st_mode)
                or temp_stat.st_uid != 0
                or temp_stat.st_gid != 0
                or temp_stat.st_nlink != 1
                or stat.S_IMODE(temp_stat.st_mode) != 0o600
                or temp_stat.st_size > MAX_PLAN_BYTES
                or not SHA256_RE.fullmatch(plan_sha256)
                or not expected_initial.startswith(temp_raw)
            ):
                raise RepairError("quarantine_exists")
            os.unlink(names[0], dir_fd=target_fd)
            os.fsync(target_fd)
        os.rmdir(quarantine.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as exc:
        raise RepairError("quarantine_exists") from exc
    finally:
        if target_fd is not None:
            os.close(target_fd)
        os.close(parent_fd)


def prepare_repair_plan(
    expected_source_sha: str,
    runner_source_sha: str,
    paths: RepairPaths = DEFAULT_PATHS,
) -> dict[str, object]:
    _sha_arg(expected_source_sha, reason="source_mismatch")
    _sha_arg(runner_source_sha, reason="runner_mismatch")
    _verify_source_bundle(paths, runner_source_sha)
    with _acquire_locks(paths, runner_source_sha):
        _assert_no_pending_operations(paths)
        _assert_no_active_qa(paths, runner_source_sha)
        current = _release_record(paths.app_dir, "current")
        previous = _release_record(paths.app_dir, "previous")
        manifest, raw_manifest = _load_active_manifest(paths, expected_source_sha)
        extra_records, cache_identity = _verify_tree_delta(paths, manifest)
        _ensure_private_dir(paths.plan_root)
        body: dict[str, object] = {
            "schema": PLAN_SCHEMA,
            "runner_source_sha": runner_source_sha,
            "active_source_sha": expected_source_sha,
            "current_release": current,
            "previous_release": previous,
            "active_manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
            "payload_tree_sha256": manifest["payload_tree_sha256"],
            "cache_dir": {"relative_path": CACHE_DIR_RELATIVE, **cache_identity},
            "extra_files": extra_records,
        }
        plan_sha256 = hashlib.sha256(_canonical_json(body)).hexdigest()
        envelope = {"plan": body, "plan_sha256": plan_sha256}
        _write_exclusive(_plan_location(paths, plan_sha256), _canonical_json(envelope))
        return {"status": "prepared", "plan_sha256": plan_sha256, "extra_file_count": 2}


def _load_plan(paths: RepairPaths, plan_sha256: str) -> dict[str, object]:
    raw, metadata = _read_regular(_plan_location(paths, plan_sha256), maximum=MAX_PLAN_BYTES)
    if metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RepairError("plan_unavailable")
    try:
        envelope = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RepairError("plan_mismatch") from exc
    if not isinstance(envelope, dict) or set(envelope) != {"plan", "plan_sha256"}:
        raise RepairError("plan_mismatch")
    body = envelope["plan"]
    if not isinstance(body, dict) or hashlib.sha256(_canonical_json(body)).hexdigest() != plan_sha256:
        raise RepairError("plan_mismatch")
    if envelope["plan_sha256"] != plan_sha256:
        raise RepairError("plan_mismatch")
    plan_keys = {
        "schema",
        "runner_source_sha",
        "active_source_sha",
        "current_release",
        "previous_release",
        "active_manifest_sha256",
        "payload_tree_sha256",
        "cache_dir",
        "extra_files",
    }
    if (
        set(body) != plan_keys
        or body.get("schema") != PLAN_SCHEMA
        or not isinstance(body.get("runner_source_sha"), str)
        or not SOURCE_SHA_RE.fullmatch(body["runner_source_sha"])
        or not isinstance(body.get("active_source_sha"), str)
        or not SOURCE_SHA_RE.fullmatch(body["active_source_sha"])
        or not isinstance(body.get("active_manifest_sha256"), str)
        or not SHA256_RE.fullmatch(body["active_manifest_sha256"])
        or not isinstance(body.get("payload_tree_sha256"), str)
        or not SHA256_RE.fullmatch(body["payload_tree_sha256"])
    ):
        raise RepairError("plan_mismatch")
    identity_keys = {
        "device", "inode", "size", "mode", "uid", "gid", "nlink", "mtime_ns", "ctime_ns"
    }

    def valid_identity(value: object) -> bool:
        return (
            isinstance(value, dict)
            and set(value) == identity_keys
            and all(type(item) is int and item >= 0 for item in value.values())
        )

    def valid_release(value: object) -> bool:
        return (
            isinstance(value, dict)
            and set(value)
            == {
                "slug",
                "source_sha",
                "pointer_identity",
                "release_identity",
                "release_json_identity",
                "release_json_sha256",
            }
            and isinstance(value.get("slug"), str)
            and SLUG_RE.fullmatch(value["slug"])
            and isinstance(value.get("source_sha"), str)
            and SOURCE_SHA_RE.fullmatch(value["source_sha"])
            and valid_identity(value.get("pointer_identity"))
            and valid_identity(value.get("release_identity"))
            and valid_identity(value.get("release_json_identity"))
            and isinstance(value.get("release_json_sha256"), str)
            and SHA256_RE.fullmatch(value["release_json_sha256"])
        )

    cache_dir = body.get("cache_dir")
    extra_files = body.get("extra_files")
    if (
        not valid_release(body.get("current_release"))
        or not valid_release(body.get("previous_release"))
        or not isinstance(cache_dir, dict)
        or set(cache_dir) != {"relative_path", *identity_keys}
        or cache_dir.get("relative_path") != CACHE_DIR_RELATIVE
        or not valid_identity({key: cache_dir.get(key) for key in identity_keys})
        or not isinstance(extra_files, list)
        or len(extra_files) != 2
    ):
        raise RepairError("plan_mismatch")
    records: list[dict[str, object]] = []
    for record in extra_files:
        if (
            not isinstance(record, dict)
            or set(record) != {"relative_path", "sha256", *identity_keys}
            or record.get("relative_path") not in EXTRA_FILE_RELATIVES
            or not isinstance(record.get("sha256"), str)
            or not SHA256_RE.fullmatch(record["sha256"])
            or not valid_identity({key: record.get(key) for key in identity_keys})
        ):
            raise RepairError("plan_mismatch")
        records.append(record)
    if [record["relative_path"] for record in records] != sorted(EXTRA_FILE_RELATIVES):
        raise RepairError("plan_mismatch")
    return body


def _same_identity(actual: os.stat_result, expected: dict[str, object]) -> bool:
    return _identity(actual) == {key: expected.get(key) for key in _identity(actual)}


def _same_directory_identity(actual: os.stat_result, expected: dict[str, object]) -> bool:
    current = _identity(actual)
    return all(
        current.get(key) == expected.get(key)
        for key in ("device", "inode", "mode", "uid", "gid")
    )


def _rename_noreplace(
    source_parent: Path,
    source_name: str,
    destination_parent: Path,
    destination_name: str,
    expected_source_identity: dict[str, object],
    *,
    failure_reason: str,
    conflict_reason: str = "quarantine_exists",
) -> None:
    """Rename a directory by dirfd without following paths or replacing target."""

    if (
        not source_name
        or source_name in {".", ".."}
        or "/" in source_name
        or not destination_name
        or destination_name in {".", ".."}
        or "/" in destination_name
    ):
        raise RepairError(failure_reason)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_parent_meta = _check_dir(source_parent)
    destination_parent_meta = _check_dir(destination_parent)
    if source_parent_meta.st_dev != destination_parent_meta.st_dev:
        raise RepairError(failure_reason)
    source_fd = os.open(source_parent, flags | getattr(os, "O_CLOEXEC", 0))
    destination_fd = os.open(destination_parent, flags | getattr(os, "O_CLOEXEC", 0))
    try:
        source_opened = os.fstat(source_fd)
        destination_opened = os.fstat(destination_fd)
        if (
            (source_opened.st_dev, source_opened.st_ino)
            != (source_parent_meta.st_dev, source_parent_meta.st_ino)
            or (destination_opened.st_dev, destination_opened.st_ino)
            != (destination_parent_meta.st_dev, destination_parent_meta.st_ino)
            or source_opened.st_dev != destination_opened.st_dev
        ):
            raise RepairError(failure_reason)
        source_stat = os.stat(source_name, dir_fd=source_fd, follow_symlinks=False)
        if not stat.S_ISDIR(source_stat.st_mode) or not _same_identity(
            source_stat, expected_source_identity
        ):
            raise RepairError(failure_reason)
        try:
            renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        except AttributeError as exc:
            raise RepairError(failure_reason) from exc
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            source_fd,
            os.fsencode(source_name),
            destination_fd,
            os.fsencode(destination_name),
            1,  # RENAME_NOREPLACE
        )
        if result != 0:
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise RepairError(conflict_reason)
            raise RepairError(failure_reason)
    except OSError as exc:
        raise RepairError(failure_reason) from exc
    finally:
        os.close(destination_fd)
        os.close(source_fd)


def _remove_created_empty_dir(
    path: Path,
    expected_identity: dict[str, object],
    expected_parent_identity: dict[str, object],
) -> None:
    parent_before = _check_dir(path.parent, exact_mode=0o700)
    if not _same_directory_identity(parent_before, expected_parent_identity):
        raise RepairError("quarantine_exists")
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    target_fd: int | None = None
    try:
        parent_opened = os.fstat(parent_fd)
        if not _same_directory_identity(parent_opened, expected_parent_identity):
            raise RepairError("quarantine_exists")
        target_fd = os.open(
            path.name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(target_fd)
        is_empty = not os.listdir(target_fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not _same_directory_identity(opened, expected_identity)
            or opened.st_nlink != 2
            or not is_empty
        ):
            raise RepairError("quarantine_exists")
        os.rmdir(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as exc:
        raise RepairError("quarantine_exists") from exc
    finally:
        if target_fd is not None:
            os.close(target_fd)
        os.close(parent_fd)


def _write_receipt(
    path: Path,
    value: dict[str, object],
    expected_parent_identity: dict[str, object],
) -> None:
    expected_keys = {"schema", "plan_sha256", "phase", "quarantine_identity"}
    phases = {"prepared", "quarantined", "validated", "rolled_back"}
    identity_keys = {"device", "inode", "size", "mode", "uid", "gid", "nlink", "mtime_ns", "ctime_ns"}
    if (
        set(value) != expected_keys
        or value.get("schema") != 1
        or not isinstance(value.get("plan_sha256"), str)
        or not SHA256_RE.fullmatch(value["plan_sha256"])
        or value.get("phase") not in phases
        or not isinstance(value.get("quarantine_identity"), dict)
        or set(value["quarantine_identity"]) != identity_keys
        or not _same_directory_identity(
            _metadata(path.parent), value["quarantine_identity"]
        )
    ):
        raise RepairError("receipt_write_failed")
    parent_before = _check_dir(path.parent, exact_mode=0o700)
    if not _same_directory_identity(parent_before, expected_parent_identity):
        raise RepairError("receipt_write_failed")
    previous_raw, previous_metadata = _read_regular(path, maximum=MAX_PLAN_BYTES)
    if (
        previous_metadata.st_gid != 0
        or stat.S_IMODE(previous_metadata.st_mode) != 0o600
    ):
        raise RepairError("receipt_write_failed")
    try:
        previous = json.loads(previous_raw.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RepairError("receipt_write_failed") from exc
    phase_transitions = {
        "prepared": {"quarantined", "rolled_back"},
        "quarantined": {"validated", "rolled_back"},
    }
    if (
        not isinstance(previous, dict)
        or set(previous) != expected_keys
        or previous.get("schema") != 1
        or previous.get("plan_sha256") != value.get("plan_sha256")
        or previous.get("quarantine_identity") != value.get("quarantine_identity")
        or value.get("phase") not in phase_transitions.get(previous.get("phase"), set())
    ):
        raise RepairError("receipt_write_failed")
    raw = _canonical_json(value)
    temp = path.parent / f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    temp_metadata: os.stat_result | None = None
    try:
        temp_metadata = _write_exclusive(temp, raw)
        if temp_metadata is None:
            raise RepairError("receipt_write_failed")
        temp_raw, temp_actual = _read_regular(temp, maximum=MAX_PLAN_BYTES)
        if (
            temp_raw != raw
            or _identity(temp_actual) != _identity(temp_metadata)
            or temp_actual.st_gid != 0
            or stat.S_IMODE(temp_actual.st_mode) != 0o600
        ):
            raise RepairError("receipt_write_failed")
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened_parent = os.fstat(parent_fd)
            current_receipt = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                (opened_parent.st_dev, opened_parent.st_ino)
                != (parent_before.st_dev, parent_before.st_ino)
                or not _same_directory_identity(opened_parent, expected_parent_identity)
                or not _same_identity(current_receipt, _identity(previous_metadata))
            ):
                raise RepairError("receipt_write_failed")
            os.replace(temp.name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except RepairError as exc:
        if temp_metadata is not None and os.path.lexists(temp):
            try:
                _unlink_just_created(temp, temp_metadata)
            except BaseException:
                pass
        raise RepairError("receipt_write_failed") from exc
    except OSError as exc:
        if temp_metadata is not None and os.path.lexists(temp):
            try:
                _unlink_just_created(temp, temp_metadata)
            except BaseException:
                pass
        raise RepairError("receipt_write_failed") from exc


RECEIPT_KEYS = frozenset({"schema", "plan_sha256", "phase", "quarantine_identity"})
IDENTITY_KEYS = frozenset(
    {"device", "inode", "size", "mode", "uid", "gid", "nlink", "mtime_ns", "ctime_ns"}
)
RECEIPT_TEMP_RE = re.compile(r"^\.intent\.json\.[1-9][0-9]{0,19}\.[1-9][0-9]{0,19}\.tmp$")


def _read_quarantine_state(
    paths: RepairPaths,
    plan_sha256: str,
) -> tuple[dict[str, object], str, bool]:
    quarantine = paths.quarantine_root / plan_sha256
    quarantine_root = _check_dir(paths.quarantine_root, exact_mode=0o700)
    quarantine_metadata = _check_dir(quarantine, exact_mode=0o700)
    if quarantine_metadata.st_dev != quarantine_root.st_dev:
        raise RepairError("quarantine_exists")
    entries = sorted(entry.name for entry in os.scandir(quarantine))
    has_cache = "bytecode-cache" in entries
    receipt_temps = [name for name in entries if RECEIPT_TEMP_RE.fullmatch(name)]
    expected_entries = {"intent.json"}
    if has_cache:
        expected_entries.add("bytecode-cache")
    if len(receipt_temps) > 1 or set(entries) - set(receipt_temps) != expected_entries:
        raise RepairError("quarantine_exists")
    raw, receipt_metadata = _read_regular(quarantine / "intent.json", maximum=MAX_PLAN_BYTES)
    if (
        receipt_metadata.st_uid != 0
        or receipt_metadata.st_gid != 0
        or stat.S_IMODE(receipt_metadata.st_mode) != 0o600
    ):
        raise RepairError("quarantine_exists")
    try:
        receipt = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RepairError("quarantine_exists") from exc
    if (
        not isinstance(receipt, dict)
        or set(receipt) != RECEIPT_KEYS
        or receipt.get("schema") != 1
        or receipt.get("plan_sha256") != plan_sha256
        or receipt.get("phase") not in {"prepared", "quarantined", "validated", "rolled_back"}
    ):
        raise RepairError("quarantine_exists")
    identity = receipt.get("quarantine_identity")
    if (
        not isinstance(identity, dict)
        or set(identity) != IDENTITY_KEYS
        or any(type(value) is not int or value < 0 for value in identity.values())
        or not _same_directory_identity(quarantine_metadata, identity)
        or quarantine_metadata.st_nlink != (3 if has_cache else 2)
    ):
        raise RepairError("quarantine_exists")
    phase = receipt["phase"]
    if (phase == "validated" and not has_cache) or (phase == "rolled_back" and has_cache):
        raise RepairError("quarantine_exists")
    if receipt_temps:
        if phase == "validated":
            raise RepairError("quarantine_exists")
        stale_temp = quarantine / receipt_temps[0]
        stale_raw, stale_metadata = _read_regular(stale_temp, maximum=MAX_PLAN_BYTES)
        if (
            stale_metadata.st_uid != 0
            or stale_metadata.st_gid != 0
            or stat.S_IMODE(stale_metadata.st_mode) != 0o600
        ):
            raise RepairError("quarantine_exists")
        try:
            stale_value = json.loads(stale_raw.decode("ascii"), object_pairs_hook=_unique_object)
        except (UnicodeError, json.JSONDecodeError, RepairError):
            stale_value = None
        valid_next_phases = {
            "prepared": {"quarantined", "rolled_back"},
            "quarantined": {"validated", "rolled_back"},
        }
        permitted_temp_values = [
            _canonical_json(
                {
                    "schema": 1,
                    "plan_sha256": plan_sha256,
                    "phase": next_phase,
                    "quarantine_identity": identity,
                }
            )
            for next_phase in valid_next_phases.get(phase, set())
        ]
        parsed_allowed = (
            isinstance(stale_value, dict)
            and set(stale_value) == RECEIPT_KEYS
            and stale_value.get("schema") == 1
            and stale_value.get("plan_sha256") == plan_sha256
            and stale_value.get("quarantine_identity") == identity
            and stale_value.get("phase") in valid_next_phases.get(phase, set())
        )
        partial_allowed = any(
            candidate.startswith(stale_raw) for candidate in permitted_temp_values
        )
        if not (parsed_allowed or partial_allowed):
            raise RepairError("quarantine_exists")
        _unlink_just_created(stale_temp, stale_metadata)
    return identity, phase, has_cache


def _verify_quarantined_cache(
    payload: Path,
    cache_path: Path,
    plan: dict[str, object],
) -> dict[str, object]:
    metadata = _metadata(cache_path)
    expected_cache = plan.get("cache_dir")
    if (
        not isinstance(expected_cache, dict)
        or expected_cache.get("relative_path") != CACHE_DIR_RELATIVE
        or not _same_directory_identity(metadata, expected_cache)
        or metadata.st_nlink != 2
    ):
        raise RepairError("extra_identity_mismatch")
    extras = plan.get("extra_files")
    if not isinstance(extras, list) or len(extras) != 2:
        raise RepairError("plan_mismatch")
    expected_names: list[str] = []
    for record in extras:
        if not isinstance(record, dict):
            raise RepairError("plan_mismatch")
        relative = record.get("relative_path")
        if relative not in EXTRA_FILE_RELATIVES:
            raise RepairError("plan_mismatch")
        relative_path = PurePosixPath(relative)
        name = relative_path.name
        expected_names.append(name)
        digest, file_metadata, data = _digest_file(cache_path / name)
        expected_identity = {
            key: record.get(key)
            for key in IDENTITY_KEYS
            if key != "device" and key != "inode"
        }
        expected_identity["device"] = record.get("device")
        expected_identity["inode"] = record.get("inode")
        if (
            _identity(file_metadata) != expected_identity
            or digest != record.get("sha256")
            or not _pyc_is_for_source(payload, relative, data)
        ):
            raise RepairError("extra_identity_mismatch")
    if sorted(entry.name for entry in os.scandir(cache_path)) != sorted(expected_names):
        raise RepairError("unexpected_tree")
    return _identity(metadata)


def _validate_active_payload(paths: RepairPaths, expected_source_sha: str) -> None:
    installer = paths.app_dir / "current" / RUNTIME_INSTALLER_RELATIVE
    result = subprocess.run(
        [
            "/usr/bin/python3.12",
            "-I",
            "-B",
            str(installer),
            "verify",
            "--app-dir",
            str(paths.app_dir),
            "--target-sha",
            expected_source_sha,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=180,
    )
    expected = f"LIVE_QA_RUNTIME_VERIFY status=passed source_sha={expected_source_sha}\n".encode()
    if result.returncode != 0 or result.stdout != expected:
        raise RepairError("validation_failed")


def apply_repair_plan(
    plan_sha256: str,
    expected_source_sha: str,
    runner_source_sha: str,
    paths: RepairPaths = DEFAULT_PATHS,
) -> dict[str, object]:
    _sha_arg(expected_source_sha, reason="source_mismatch")
    _sha_arg(runner_source_sha, reason="runner_mismatch")
    _verify_source_bundle(paths, runner_source_sha)
    _ensure_private_dir(paths.plan_root)
    plan = _load_plan(paths, plan_sha256)
    if plan.get("runner_source_sha") != runner_source_sha or plan.get("active_source_sha") != expected_source_sha:
        raise RepairError("plan_mismatch")
    with _acquire_locks(paths, runner_source_sha):
        _assert_no_pending_operations(paths)
        _assert_no_active_qa(paths, runner_source_sha)
        current = _release_record(paths.app_dir, "current")
        previous = _release_record(paths.app_dir, "previous")
        manifest, raw_manifest = _load_active_manifest(paths, expected_source_sha)
        if (
            current != plan.get("current_release")
            or previous != plan.get("previous_release")
            or hashlib.sha256(raw_manifest).hexdigest() != plan.get("active_manifest_sha256")
            or manifest.get("payload_tree_sha256") != plan.get("payload_tree_sha256")
        ):
            raise RepairError("plan_mismatch")

        payload = Path(str(manifest["payload"]))
        cache_path = payload / CACHE_DIR_RELATIVE
        cache_parent = cache_path.parent
        quarantine_root_metadata = _ensure_private_dir(paths.quarantine_root)
        if quarantine_root_metadata.st_dev != payload.stat().st_dev:
            raise RepairError("quarantine_exists")
        quarantine = paths.quarantine_root / plan_sha256
        receipt = quarantine / "intent.json"
        cache_quarantine = quarantine / "bytecode-cache"
        cache_name = cache_path.name
        quarantine_name = cache_quarantine.name

        def receipt_value(phase: str, identity: dict[str, object]) -> dict[str, object]:
            return {
                "schema": 1,
                "plan_sha256": plan_sha256,
                "phase": phase,
                "quarantine_identity": identity,
            }

        def restore_after_failure(
            moved_identity: dict[str, object],
            quarantine_identity: dict[str, object],
        ) -> None:
            if os.path.lexists(cache_path):
                raise RepairError("rollback_failed")
            _rename_noreplace(
                quarantine,
                quarantine_name,
                cache_parent,
                cache_name,
                moved_identity,
                failure_reason="rollback_failed",
                conflict_reason="rollback_failed",
            )
            _fsync_dir(cache_parent)
            _fsync_dir(quarantine)
            _write_receipt(
                receipt,
                receipt_value("rolled_back", quarantine_identity),
                quarantine_identity,
            )

        if os.path.lexists(quarantine) and not os.path.lexists(receipt):
            # A process may stop after creating the plan-named private directory
            # but before the initial intent is atomically published.  No cache
            # move occurs until that receipt exists, so only this exact empty or
            # single-temp state may be discarded after the original tree rechecks.
            extras, cache_identity = _verify_tree_delta(paths, manifest)
            if extras != plan.get("extra_files") or {
                "relative_path": CACHE_DIR_RELATIVE,
                **cache_identity,
            } != plan.get("cache_dir"):
                raise RepairError("extra_identity_mismatch")
            _remove_unstarted_quarantine(
                quarantine,
                _identity(quarantine_root_metadata),
                payload.stat().st_dev,
            )

        if os.path.lexists(quarantine):
            quarantine_identity, phase, has_cache_quarantine = _read_quarantine_state(
                paths, plan_sha256
            )
        else:
            extras, cache_identity = _verify_tree_delta(paths, manifest)
            if extras != plan.get("extra_files") or {
                "relative_path": CACHE_DIR_RELATIVE,
                **cache_identity,
            } != plan.get("cache_dir"):
                raise RepairError("extra_identity_mismatch")
            try:
                quarantine.mkdir(mode=0o700)
                os.chown(quarantine, 0, 0)
            except OSError as exc:
                raise RepairError("quarantine_exists") from exc
            quarantine_identity = _identity(_check_dir(quarantine, exact_mode=0o700))
            if quarantine.stat().st_dev != payload.stat().st_dev:
                _remove_created_empty_dir(
                    quarantine,
                    quarantine_identity,
                    _identity(quarantine_root_metadata),
                )
                raise RepairError("quarantine_exists")
            try:
                _publish_initial_receipt(
                    receipt,
                    receipt_value("prepared", quarantine_identity),
                    quarantine_identity,
                )
            except BaseException as exc:
                _remove_unstarted_quarantine(
                    quarantine,
                    _identity(quarantine_root_metadata),
                    payload.stat().st_dev,
                )
                if isinstance(exc, RepairError):
                    raise
                raise RepairError("receipt_write_failed") from exc
            phase = "prepared"
            has_cache_quarantine = False

        cache_present = os.path.lexists(cache_path)
        cache_quarantine_present = os.path.lexists(cache_quarantine)
        if cache_present and cache_quarantine_present:
            raise RepairError("quarantine_exists")
        if phase == "rolled_back":
            raise RepairError("plan_mismatch")
        if phase == "quarantined" and cache_present and not cache_quarantine_present:
            # Crash after restoring the original directory but before recording
            # the spent-plan receipt.  Prove the exact original cache and extras,
            # then make the plan permanently rolled back before refusing reuse.
            extras, cache_identity = _verify_tree_delta(paths, manifest)
            planned_cache = plan.get("cache_dir")
            if (
                extras != plan.get("extra_files")
                or not isinstance(planned_cache, dict)
                or planned_cache.get("relative_path") != CACHE_DIR_RELATIVE
                or any(
                    cache_identity.get(key) != planned_cache.get(key)
                    for key in IDENTITY_KEYS - {"ctime_ns"}
                )
            ):
                raise RepairError("extra_identity_mismatch")
            _write_receipt(
                receipt,
                receipt_value("rolled_back", quarantine_identity),
                quarantine_identity,
            )
            raise RepairError("plan_mismatch")
        if phase == "validated":
            if cache_present or not cache_quarantine_present:
                raise RepairError("quarantine_exists")
            _verify_quarantined_cache(payload, cache_quarantine, plan)
            _validate_active_payload(paths, expected_source_sha)
            return {
                "status": "validated",
                "plan_sha256": plan_sha256,
                "quarantined_file_count": 2,
            }

        if cache_present:
            if phase != "prepared" or cache_quarantine_present:
                raise RepairError("quarantine_exists")
            extras, cache_identity = _verify_tree_delta(paths, manifest)
            if extras != plan.get("extra_files") or {
                "relative_path": CACHE_DIR_RELATIVE,
                **cache_identity,
            } != plan.get("cache_dir"):
                raise RepairError("extra_identity_mismatch")
        elif cache_quarantine_present:
            if phase not in {"prepared", "quarantined"} or not has_cache_quarantine:
                raise RepairError("quarantine_exists")
            moved_identity = _verify_quarantined_cache(payload, cache_quarantine, plan)
            if phase == "prepared":
                _write_receipt(
                    receipt,
                    receipt_value("quarantined", quarantine_identity),
                    quarantine_identity,
                )
            try:
                _validate_active_payload(paths, expected_source_sha)
            except BaseException as exc:
                try:
                    restore_after_failure(moved_identity, quarantine_identity)
                except BaseException as rollback_exc:
                    raise RepairError("rollback_failed") from rollback_exc
                if isinstance(exc, RepairError):
                    raise
                raise RepairError("validation_failed") from exc
            _write_receipt(
                receipt,
                receipt_value("validated", quarantine_identity),
                quarantine_identity,
            )
            return {
                "status": "validated",
                "plan_sha256": plan_sha256,
                "quarantined_file_count": 2,
            }
        else:
            raise RepairError("quarantine_exists")

        moved_identity: dict[str, object] | None = None
        try:
            _rename_noreplace(
                cache_parent,
                cache_name,
                quarantine,
                quarantine_name,
                plan["cache_dir"],
                failure_reason="extra_identity_mismatch",
            )
            moved_identity = _identity(_metadata(cache_quarantine))
            _fsync_dir(cache_parent)
            _fsync_dir(quarantine)
            _write_receipt(
                receipt,
                receipt_value("quarantined", quarantine_identity),
                quarantine_identity,
            )
            _validate_active_payload(paths, expected_source_sha)
        except BaseException as exc:
            if os.path.lexists(cache_quarantine):
                try:
                    if moved_identity is None:
                        moved_identity = _verify_quarantined_cache(payload, cache_quarantine, plan)
                    restore_after_failure(moved_identity, quarantine_identity)
                except BaseException as rollback_exc:
                    raise RepairError("rollback_failed") from rollback_exc
            if isinstance(exc, RepairError):
                raise
            raise RepairError("validation_failed") from exc

        try:
            _write_receipt(
                receipt,
                receipt_value("validated", quarantine_identity),
                quarantine_identity,
            )
        except RepairError as exc:
            # Strict verification already committed the repair; never restore
            # the unmanifested cache on a receipt-only failure.
            raise RepairError("receipt_write_failed") from exc
        return {"status": "validated", "plan_sha256": plan_sha256, "quarantined_file_count": 2}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--expected-source-sha", required=True)
    prepare.add_argument("--runner-source-sha", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--expected-source-sha", required=True)
    apply.add_argument("--runner-source-sha", required=True)
    apply.add_argument("--plan-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.operation == "prepare":
            result = prepare_repair_plan(args.expected_source_sha, args.runner_source_sha)
        else:
            result = apply_repair_plan(
                args.plan_sha256, args.expected_source_sha, args.runner_source_sha
            )
        print(
            "LIVE_QA_PAYLOAD_BYTECODE_REPAIR "
            + " ".join(f"{key}={value}" for key, value in result.items())
        )
        return 0
    except RepairError as exc:
        print(
            f"LIVE_QA_PAYLOAD_BYTECODE_REPAIR status=failed reason={exc.reason}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
