#!/usr/bin/env python3
"""Read-only proof that the original 9ce artifact matches the active runtime.

This module is called by the signed recovery generation while the canonical
release lock is held. It never extracts an archive, installs files, changes a
pointer, or invokes an installer. The workflow must verify both GitHub
attestation policies before creating the closed receipt consumed here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import stat
import subprocess
import sys
import tarfile
import time
import types
from typing import Any


APP_DIR = Path("/opt/oldsparky/platform")
SOURCE_SHA = "9ceaf09ab7ba4bf5c5e636865106910eaa034c8d"
RELEASE_SLUG = "gha-38185744516-1-9ceaf09ab7ba"
DEPLOY_RUN_ID = 38185744516
DEPLOY_RUN_ATTEMPT = 1
ARTIFACT_ID = 11719716640
ARTIFACT_NAME = "platform-release-artifact-38185744516-1"
ARTIFACT_ZIP_SHA256 = "9509ff1fe8a4a7520b20a7b1f62bafa90758d32a50d071f02cb25f7110bc7617"
RELEASE_TAR_SHA256 = "2f305c4f85a2a9b6e5a12739fb02f04e823a8327925f0019032e2d32203f2c62"
REPOSITORY = "StrayForest/old_sparky"
DEPLOY_WORKFLOW = ".github/workflows/platform-production-deploy.yml"
SOURCE_REF = "refs/heads/dev"
ATTESTATION_POLICIES = (
    {"id": "certificate_identity_issuer", "result": "passed"},
    {"id": "signer_workflow", "result": "passed"},
)

MAX_RECEIPT_BYTES = 16 * 1024
MAX_CHECKSUM_BYTES = 256
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 200_000
MAX_INPUT_FILES = 200_000
MAX_INPUT_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 768 * 1024 * 1024
MAX_OPTIONAL_CACHE_FILES = 128
MAX_OPTIONAL_CACHE_BYTES = 64 * 1024 * 1024
MAX_OPTIONAL_CACHE_FILE_BYTES = 4 * 1024 * 1024
DEFAULT_DEADLINE_SECONDS = 600
CHUNK_BYTES = 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")

_RECEIPT_KEYS = {
    "schema",
    "repository",
    "run_id",
    "run_attempt",
    "artifact_id",
    "artifact_name",
    "artifact_zip_sha256",
    "tar_sha256",
    "source_sha",
    "release_slug",
    "source_ref",
    "policies",
}
_PROVENANCE_KEYS = {
    "schema",
    "artifact_file",
    "artifact_sha256",
    "source_git_commit",
}
_OPTIONAL_CACHE_ROOTS = ("apps/platform_api", "python_packages")


class ActiveArtifactProofError(RuntimeError):
    """Closed refusal; ``code`` contains no paths or private source details."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> None:
    raise ActiveArtifactProofError(code)


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        _fail("timeout")


def _pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _fail("duplicate_json_key")
        value[key] = item
    return value


def _safe_file(path: Path, *, maximum: int, mode: int = 0o600) -> tuple[int, os.stat_result]:
    try:
        before = path.lstat()
    except OSError:
        _fail("input_unavailable")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or before.st_gid != 0
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_size < 0
        or before.st_size > maximum
    ):
        _fail("input_metadata")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
    except OSError:
        _fail("input_unavailable")
    if _fingerprint(opened) != _fingerprint(before):
        os.close(descriptor)
        _fail("input_changed")
    return descriptor, opened


def _fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_closed_json(path: Path, *, maximum: int) -> tuple[dict[str, object], tuple[int, ...]]:
    descriptor, opened = _safe_file(path, maximum=maximum)
    try:
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
    except OSError:
        _fail("input_read")
    finally:
        os.close(descriptor)
    if len(raw) != opened.st_size or len(raw) > maximum or _fingerprint(after) != _fingerprint(opened):
        _fail("input_changed")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs_no_duplicates)
    except (UnicodeError, json.JSONDecodeError):
        _fail("json_invalid")
    if not isinstance(value, dict):
        _fail("json_schema")
    return value, _fingerprint(opened)


def _validate_receipt(path: Path) -> tuple[dict[str, object], tuple[int, ...]]:
    receipt, identity = _read_closed_json(path, maximum=MAX_RECEIPT_BYTES)
    if set(receipt) != _RECEIPT_KEYS:
        _fail("receipt_schema")
    if (
        type(receipt.get("schema")) is not int
        or receipt["schema"] != 1
        or receipt.get("repository") != REPOSITORY
        or type(receipt.get("run_id")) is not int
        or receipt["run_id"] != DEPLOY_RUN_ID
        or type(receipt.get("run_attempt")) is not int
        or receipt["run_attempt"] != DEPLOY_RUN_ATTEMPT
        or type(receipt.get("artifact_id")) is not int
        or receipt["artifact_id"] != ARTIFACT_ID
        or receipt.get("artifact_name") != ARTIFACT_NAME
        or receipt.get("artifact_zip_sha256") != ARTIFACT_ZIP_SHA256
        or receipt.get("tar_sha256") != RELEASE_TAR_SHA256
        or receipt.get("source_sha") != SOURCE_SHA
        or receipt.get("release_slug") != RELEASE_SLUG
        or receipt.get("source_ref") != SOURCE_REF
        or receipt.get("policies") != list(ATTESTATION_POLICIES)
    ):
        _fail("receipt_binding")
    return receipt, identity


def _hash_fd(descriptor: int, *, deadline: float) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        _check_deadline(deadline)
        chunk = os.read(descriptor, CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _hash_regular(path: Path, *, maximum: int, deadline: float) -> tuple[str, os.stat_result]:
    descriptor, before = _safe_release_file(path, maximum=maximum)
    try:
        digest = _hash_fd(descriptor, deadline=deadline)
        after = os.fstat(descriptor)
        path_after = path.lstat()
    except OSError:
        _fail("release_file_read")
    finally:
        os.close(descriptor)
    if _fingerprint(after) != _fingerprint(before) or _fingerprint(path_after) != _fingerprint(before):
        _fail("release_file_changed")
    return digest, before


def _safe_release_file(path: Path, *, maximum: int) -> tuple[int, os.stat_result]:
    try:
        before = path.lstat()
    except OSError:
        _fail("release_file_missing")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or before.st_gid != 0
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o022
        or before.st_size < 0
        or before.st_size > maximum
    ):
        _fail("release_file_metadata")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
    except OSError:
        _fail("release_file_missing")
    if _fingerprint(opened) != _fingerprint(before):
        os.close(descriptor)
        _fail("release_file_changed")
    return descriptor, opened


def _check_protected_directory(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        _fail("release_directory_missing")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_nlink < 2
    ):
        _fail("release_directory_metadata")
    return metadata


def _check_current(app_dir: Path) -> tuple[Path, tuple[int, ...]]:
    if app_dir != APP_DIR:
        _fail("app_dir_binding")
    _check_protected_directory(app_dir)
    releases = app_dir / "releases"
    _check_protected_directory(releases)
    current = app_dir / "current"
    try:
        pointer = current.lstat()
        link = os.readlink(current)
        resolved = current.resolve(strict=True)
    except OSError:
        _fail("current_pointer")
    expected = releases / RELEASE_SLUG
    if (
        not stat.S_ISLNK(pointer.st_mode)
        or pointer.st_uid != 0
        or pointer.st_gid != 0
        or pointer.st_nlink != 1
        or link not in {f"releases/{RELEASE_SLUG}", str(expected)}
        or resolved != expected
    ):
        _fail("current_target")
    release_metadata = _check_protected_directory(expected)
    return expected, (_fingerprint(pointer) + _fingerprint(release_metadata))


def _safe_member_digest(archive: tarfile.TarFile, member: tarfile.TarInfo, *, deadline: float) -> str:
    if not member.isfile() or member.size < 0 or member.size > MAX_FILE_BYTES:
        _fail("mapped_member_type")
    stream = archive.extractfile(member)
    if stream is None:
        _fail("mapped_member_unavailable")
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            _check_deadline(deadline)
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > member.size or total > MAX_FILE_BYTES:
                _fail("mapped_member_bound")
            digest.update(chunk)
    finally:
        stream.close()
    if total != member.size:
        _fail("mapped_member_truncated")
    return digest.hexdigest()


def _cache_source_relative(relative: str) -> str | None:
    path = PurePosixPath(relative)
    parts = path.parts
    if (
        len(parts) < 3
        or parts[-2] != "__pycache__"
        or not parts[-1].endswith(".pyc")
    ):
        return None
    stem = parts[-1][:-4]
    for marker in (".cpython-", ".pypy-"):
        if marker in stem:
            stem = stem.split(marker, 1)[0]
            break
    else:
        if stem.endswith((".opt-1", ".opt-2")):
            stem = stem.rsplit(".opt-", 1)[0]
    if not stem or "/" in stem:
        return None
    source_name = "__init__.py" if stem == "__init__" else f"{stem}.py"
    return PurePosixPath(*parts[:-2], source_name).as_posix()


def _source_tree_cache_relative(installer: Any, relative: str) -> str | None:
    helper = getattr(installer, "_source_tree_cache_relative", None)
    if not callable(helper):
        _fail("source_cache_policy")
    paired = helper(relative)
    if paired is None:
        return None
    path = PurePosixPath(paired)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix != ".py"
    ):
        _fail("source_cache_policy")
    return path.as_posix()


def _read_mapped_inputs(
    installer: Any,
    artifact_tar: Path,
    release_root: Path,
    *,
    deadline: float,
) -> tuple[dict[str, str], set[str], int, int, int]:
    """Derive the install payload map from archive members, then bind source bytes."""
    try:
        with tarfile.open(artifact_tar, mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_MEMBERS:
                _fail("archive_member_bound")
            names = {member.name: member for member in members}
            if len(names) != len(members):
                _fail("archive_duplicate_member")
            cache_policy = getattr(installer, "SOURCE_TREE_CACHE_POLICY", None)
            if cache_policy not in {None, "paired-python-bytecode-v1"}:
                _fail("source_cache_policy")
            source_cache_policy = cache_policy == "paired-python-bytecode-v1"
            root = f"{RELEASE_SLUG}/"
            tool_names = {f"{root}tools/{name}" for name in installer.TOOL_FILES}
            source_file_names = {f"{root}{name}" for name in installer.SOURCE_FILES}
            tree_prefixes = tuple(f"{root}{name}/" for name in installer.SOURCE_TREES)
            runtime_prefix = f"{root}liveqa-runtime/"

            selected: dict[str, tuple[str, tarfile.TarInfo]] = {}
            directories: set[str] = set()
            for member in members:
                name = member.name
                if name in tool_names:
                    relative = name.removeprefix(root)
                    selected[name] = (f"platform/{relative}", member)
                elif name in source_file_names:
                    relative = name.removeprefix(root)
                    selected[name] = (f"platform/{relative}", member)
                elif any(name.startswith(prefix) for prefix in tree_prefixes):
                    source_tree = next(
                        prefix.removeprefix(root).removesuffix("/")
                        for prefix in tree_prefixes
                        if name.startswith(prefix)
                    )
                    relative_in_tree = name.removeprefix(f"{root}{source_tree}/")
                    relative_parts = PurePosixPath(relative_in_tree).parts
                    if cache_policy == "paired-python-bytecode-v1" and "__pycache__" in relative_parts:
                        if member.isdir() and relative_parts[-1] == "__pycache__":
                            if relative_parts.count("__pycache__") != 1:
                                _fail("source_cache_member")
                            continue
                        if not member.isfile() or _source_tree_cache_relative(installer, relative_in_tree) is None:
                            _fail("source_cache_member")
                        paired_source = _source_tree_cache_relative(installer, relative_in_tree)
                        paired_member = names.get(f"{root}{source_tree}/{paired_source}")
                        if paired_member is None or not paired_member.isfile():
                            _fail("source_cache_pair")
                        continue
                    relative = name.removeprefix(root)
                    selected[name] = (f"platform/{relative}", member)
                elif name == f"{root}liveqa-runtime" or name.startswith(runtime_prefix):
                    relative = name.removeprefix(runtime_prefix) if name != f"{root}liveqa-runtime" else ""
                    destination = "runtime" if not relative else f"runtime/{relative}"
                    selected[name] = (destination, member)
            if not tool_names.issubset(selected) or not source_file_names.issubset(selected):
                _fail("archive_input_missing")
            for prefix in tree_prefixes:
                if not any(name == prefix[:-1] or name.startswith(prefix) for name in names):
                    _fail("archive_input_missing")
            if f"{root}liveqa-runtime" not in selected:
                _fail("archive_runtime_missing")

            payload_files: dict[str, str] = {}
            input_files: dict[str, tuple[str, int, int]] = {}
            mapped_bytes = 0
            for archive_name, (destination, member) in selected.items():
                _check_deadline(deadline)
                if member.isdir():
                    directories.add(destination)
                    continue
                if not member.isfile():
                    _fail("mapped_member_type")
                source_relative = archive_name.removeprefix(root)
                digest = _safe_member_digest(archive, member, deadline=deadline)
                if destination in payload_files:
                    _fail("archive_input_duplicate")
                payload_files[destination] = digest
                input_files[source_relative] = (digest, member.size, member.mode)
                mapped_bytes += member.size
                if len(payload_files) > MAX_INPUT_FILES or mapped_bytes > MAX_INPUT_BYTES:
                    _fail("archive_input_bound")
            if not any(name.startswith(runtime_prefix) for name in selected):
                _fail("archive_runtime_missing")

            # Every source-directory parent is represented in the payload tree,
            # including archive directories that contain only nested directories.
            payload_directories = {"platform", "platform/tools", "runtime"}
            for destination in directories:
                if destination == "runtime" or destination.startswith("runtime/") or destination.startswith("platform/"):
                    payload_directories.add(destination)
            for destination in payload_files:
                parent = PurePosixPath(destination).parent
                while str(parent) != ".":
                    payload_directories.add(parent.as_posix())
                    parent = parent.parent

            optional_cache_count = 0
            optional_cache_bytes = 0
            optional_cache_dirs: set[str] = set()
            observed_cache_dirs: set[str] = set()
            expected_source_paths = set(input_files)
            # Compare all mapped release inputs with the original archive.
            for relative, (expected_digest, expected_size, expected_mode) in input_files.items():
                _check_deadline(deadline)
                actual = release_root / relative
                actual_digest, actual_metadata = _hash_regular(
                    actual,
                    maximum=MAX_FILE_BYTES,
                    deadline=deadline,
                )
                if (
                    actual_digest != expected_digest
                    or actual_metadata.st_size != expected_size
                    or stat.S_IMODE(actual_metadata.st_mode) != stat.S_IMODE(expected_mode)
                ):
                    _fail("active_source_mismatch")

            # Exact input-tree inventory. The only extra admitted inside mapped
            # Python source trees is a small, paired, regular __pycache__ .pyc.
            expected_dirs: set[str] = set()
            for relative in input_files:
                parent = PurePosixPath(relative).parent
                while str(parent) != ".":
                    expected_dirs.add(parent.as_posix())
                    parent = parent.parent
            for archive_name, (_destination, member) in selected.items():
                if member.isdir():
                    expected_dirs.add(archive_name.removeprefix(root))
            input_roots = set(installer.SOURCE_TREES) | {"liveqa-runtime"}
            for input_root in input_roots:
                base = release_root / input_root
                _check_protected_directory(base)
                actual_files: set[str] = set()
                actual_dirs: set[str] = set()
                for directory, dirnames, filenames in _walk_release_tree(base):
                    _check_deadline(deadline)
                    directory_path = Path(directory)
                    relative_dir = directory_path.relative_to(release_root).as_posix()
                    actual_dirs.add(relative_dir)
                    retained_dirs: list[str] = []
                    for dirname in dirnames:
                        child = directory_path / dirname
                        metadata = child.lstat()
                        child_relative = child.relative_to(release_root).as_posix()
                        if not stat.S_ISDIR(metadata.st_mode):
                            _fail("active_source_type")
                        _check_dir_metadata(metadata)
                        if child_relative not in expected_dirs:
                            # Only source-tree bytecode caches may add directories.
                            if (
                                input_root not in _OPTIONAL_CACHE_ROOTS
                                or dirname != "__pycache__"
                                or child_relative.split("/").count("__pycache__") != 1
                            ):
                                _fail("active_source_extra")
                            retained_dirs.append(dirname)
                            observed_cache_dirs.add(child_relative)
                        else:
                            retained_dirs.append(dirname)
                        actual_dirs.add(child_relative)
                    dirnames[:] = retained_dirs
                    for filename in filenames:
                        path = directory_path / filename
                        relative = path.relative_to(release_root).as_posix()
                        metadata = path.lstat()
                        if not stat.S_ISREG(metadata.st_mode):
                            _fail("active_source_type")
                        if relative in input_files:
                            actual_files.add(relative)
                            continue
                        if input_root not in _OPTIONAL_CACHE_ROOTS:
                            _fail("active_source_extra")
                        if source_cache_policy:
                            relative_in_tree = relative.removeprefix(input_root + "/")
                            paired_in_tree = _source_tree_cache_relative(installer, relative_in_tree)
                            paired = (
                                f"{input_root}/{paired_in_tree}"
                                if paired_in_tree is not None
                                else None
                            )
                        else:
                            paired = _cache_source_relative(relative)
                        if (
                            paired is None
                            or paired not in expected_source_paths
                            or metadata.st_size > MAX_OPTIONAL_CACHE_FILE_BYTES
                            or metadata.st_uid != 0
                            or metadata.st_gid != 0
                            or metadata.st_nlink != 1
                            or stat.S_IMODE(metadata.st_mode) & 0o022
                        ):
                            _fail("active_source_extra")
                        _validate_derived_pyc(
                            path,
                            release_root / paired,
                            relative,
                            deadline=deadline,
                        )
                        optional_cache_count += 1
                        optional_cache_bytes += metadata.st_size
                        optional_cache_dirs.add(PurePosixPath(relative).parent.as_posix())
                        if (
                            optional_cache_count > MAX_OPTIONAL_CACHE_FILES
                            or optional_cache_bytes > MAX_OPTIONAL_CACHE_BYTES
                        ):
                            _fail("active_cache_bound")
                        actual_files.add(relative)
                expected_root_dirs = {
                    directory
                    for directory in expected_dirs
                    if directory == input_root or directory.startswith(input_root + "/")
                }
                actual_root_dirs = {
                    directory
                    for directory in actual_dirs
                    if directory == input_root or directory.startswith(input_root + "/")
                }
                expected_root_files = {
                    path
                    for path in input_files
                    if path == input_root or path.startswith(input_root + "/")
                }
                actual_root_files = {
                    path
                    for path in actual_files
                    if path == input_root or path.startswith(input_root + "/")
                }
                if expected_root_dirs - actual_root_dirs or expected_root_files - actual_root_files:
                    _fail("active_source_missing")
                if input_root == "liveqa-runtime" and actual_root_dirs != expected_root_dirs:
                    _fail("active_runtime_directory_mismatch")
                if input_root in _OPTIONAL_CACHE_ROOTS and actual_root_dirs - expected_root_dirs:
                    extras = actual_root_dirs - expected_root_dirs
                    allowed_cache_dirs = (
                        observed_cache_dirs
                        if source_cache_policy
                        else optional_cache_dirs
                    )
                    if (
                        any(not value.endswith("/__pycache__") for value in extras)
                        or extras - allowed_cache_dirs
                    ):
                        _fail("active_source_extra")

            return (
                payload_files,
                payload_directories,
                len(input_files),
                mapped_bytes,
                optional_cache_count,
            )
    except ActiveArtifactProofError:
        raise
    except (OSError, tarfile.TarError, ValueError, KeyError):
        _fail("archive_map_invalid")


def _check_dir_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink < 2
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        _fail("active_source_directory_metadata")


def _walk_release_tree(base: Path):
    def fail_walk(error: OSError) -> None:
        raise error

    return os.walk(base, topdown=True, onerror=fail_walk, followlinks=False)


def _validate_derived_pyc(
    pyc_path: Path,
    source_path: Path,
    relative_pyc: str,
    *,
    deadline: float,
) -> None:
    """Accept only a bounded checked-hash cache reproducible from its source."""
    source_digest, source_metadata = _hash_regular(
        source_path,
        maximum=MAX_OPTIONAL_CACHE_FILE_BYTES,
        deadline=deadline,
    )
    del source_digest
    source_fd, source_opened = _safe_release_file(
        source_path,
        maximum=MAX_OPTIONAL_CACHE_FILE_BYTES,
    )
    try:
        source_bytes = bytearray()
        while True:
            _check_deadline(deadline)
            chunk = os.read(source_fd, CHUNK_BYTES)
            if not chunk:
                break
            source_bytes.extend(chunk)
            if len(source_bytes) > MAX_OPTIONAL_CACHE_FILE_BYTES:
                _fail("active_cache_bound")
        source_after = os.fstat(source_fd)
    except OSError:
        _fail("active_source_read")
    finally:
        os.close(source_fd)
    if _fingerprint(source_after) != _fingerprint(source_opened) or _fingerprint(source_after) != _fingerprint(source_metadata):
        _fail("active_source_changed")
    try:
        if _fingerprint(source_path.lstat()) != _fingerprint(source_metadata):
            _fail("active_source_changed")
    except OSError:
        _fail("active_source_changed")

    pyc_fd, pyc_before = _safe_release_file(
        pyc_path,
        maximum=MAX_OPTIONAL_CACHE_FILE_BYTES,
    )
    try:
        pyc_bytes = bytearray()
        while True:
            _check_deadline(deadline)
            chunk = os.read(pyc_fd, CHUNK_BYTES)
            if not chunk:
                break
            pyc_bytes.extend(chunk)
            if len(pyc_bytes) > MAX_OPTIONAL_CACHE_FILE_BYTES:
                _fail("active_cache_bound")
        pyc_after = os.fstat(pyc_fd)
    except OSError:
        _fail("active_cache_read")
    finally:
        os.close(pyc_fd)
    if _fingerprint(pyc_after) != _fingerprint(pyc_before) or len(pyc_bytes) < 16:
        _fail("active_cache_changed")
    try:
        if _fingerprint(pyc_path.lstat()) != _fingerprint(pyc_before):
            _fail("active_cache_changed")
    except OSError:
        _fail("active_cache_changed")
    _validate_derived_pyc_bytes(
        bytes(pyc_bytes),
        bytes(source_bytes),
        source_metadata,
        source_path,
        relative_pyc,
        deadline=deadline,
    )


def _validate_derived_pyc_bytes(
    pyc_bytes: bytes,
    source_bytes: bytes,
    source_metadata: os.stat_result,
    source_path: Path,
    relative_pyc: str,
    *,
    deadline: float | None = None,
) -> None:
    if bytes(pyc_bytes[:4]) != importlib.util.MAGIC_NUMBER:
        _fail("active_cache_header")
    flags = int.from_bytes(pyc_bytes[4:8], "little")
    if flags == 3:
        if pyc_bytes[8:16] != importlib.util.source_hash(source_bytes):
            _fail("active_cache_source_hash")
    elif flags == 0:
        source_timestamp = (source_metadata.st_mtime_ns // 1_000_000_000) & 0xFFFFFFFF
        source_size = source_metadata.st_size & 0xFFFFFFFF
        if (
            int.from_bytes(pyc_bytes[8:12], "little") != source_timestamp
            or int.from_bytes(pyc_bytes[12:16], "little") != source_size
        ):
            _fail("active_cache_source_stat")
    else:
        _fail("active_cache_header")

    release_root = Path(APP_DIR) / "releases" / RELEASE_SLUG
    try:
        source_relative = source_path.relative_to(release_root)
    except ValueError:
        _fail("active_cache_source_path")
    source_filenames = (
        str(source_path),
        str(Path(APP_DIR) / "current" / source_relative),
    )
    basename = PurePosixPath(relative_pyc).name
    optimization = 1 if ".opt-1." in basename else 2 if ".opt-2." in basename else 0
    cache_deadline = deadline if deadline is not None else time.monotonic() + 5
    if not _cache_body_matches_source(
        bytes(pyc_bytes[16:]),
        source_bytes,
        source_filenames,
        optimization=optimization,
        deadline=cache_deadline,
    ):
        _fail("active_cache_code_mismatch")


_CACHE_COMPARE_CHILD = r'''
import io
import marshal
import sys
import types
from collections import Counter

MAX_INPUT = 8 * 1024 * 1024
def read_exact(stream, count):
    chunks = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ValueError("truncated input")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)

def adaptive_code_map(root):
    result = Counter()
    pending = [(root, 0)]
    seen = set()
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if depth > 100 or visited > 100000:
            raise ValueError("code object bound")
        if isinstance(value, types.CodeType):
            identity = id(value)
            if identity in seen:
                continue
            seen.add(identity)
            adaptive = getattr(value, "_co_code_adaptive", value.co_code)
            result[(marshal.dumps(value, 2), adaptive)] += 1
            pending.extend((item, depth + 1) for item in value.co_consts)
        elif type(value) is tuple or type(value) is frozenset:
            pending.extend((item, depth + 1) for item in value)
    return result

def same_code(actual, expected, filename):
    if type(actual) is not types.CodeType or type(expected) is not types.CodeType:
        return False
    if actual.co_filename != filename or expected.co_filename != filename:
        return False
    # Marshal v2 serializes the complete code-object contract, including
    # localsplus names/kinds and recursively typed constants, without the
    # reference-table representation differences of later marshal versions.
    # Compare adaptive instructions too: marshal normalizes/deoptimizes them.
    return (
        marshal.dumps(actual, 2) == marshal.dumps(expected, 2)
        and adaptive_code_map(actual) == adaptive_code_map(expected)
    )

try:
    if len(sys.argv) != 4:
        raise ValueError("argv")
    filenames = tuple(sys.argv[1:3])
    optimization = int(sys.argv[3])
    stream = sys.stdin.buffer
    source_size = int.from_bytes(read_exact(stream, 8), "big")
    code_size = int.from_bytes(read_exact(stream, 8), "big")
    if source_size + code_size > MAX_INPUT or optimization not in (0, 1, 2):
        raise ValueError("input bound")
    source = read_exact(stream, source_size)
    body = read_exact(stream, code_size)
    if stream.read(1):
        raise ValueError("trailing input")
    body_stream = io.BytesIO(body)
    actual = marshal.load(body_stream)
    if body_stream.read(1):
        raise ValueError("trailing marshal bytes")
    if type(actual) is not types.CodeType:
        raise ValueError("not code")
    matched = False
    for filename in filenames:
        if actual.co_filename != filename:
            continue
        expected = compile(source, filename, "exec", dont_inherit=True, optimize=optimization)
        if same_code(actual, expected, filename):
            matched = True
            break
    sys.stdout.write("match\n" if matched else "mismatch\n")
except BaseException:
    sys.stdout.write("refuse\n")
'''


def _cache_body_matches_source(
    code_body: bytes,
    source_bytes: bytes,
    filenames: tuple[str, str],
    *,
    optimization: int,
    deadline: float,
) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or len(code_body) + len(source_bytes) > 8 * 1024 * 1024:
        _fail("active_cache_bound")
    payload = (
        len(source_bytes).to_bytes(8, "big")
        + len(code_body).to_bytes(8, "big")
        + source_bytes
        + code_body
    )

    def restrict_child() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
        resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (8, 8))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)

    try:
        completed = subprocess.run(
            [
                os.path.realpath(sys.executable),
                "-I",
                "-S",
                "-c",
                _CACHE_COMPARE_CHILD,
                *filenames,
                str(optimization),
            ],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=min(5.0, remaining),
            check=False,
            env={},
            close_fds=True,
            start_new_session=True,
            preexec_fn=restrict_child,
        )
    except subprocess.TimeoutExpired:
        _fail("active_cache_compare_timeout")
    except (OSError, ValueError, subprocess.SubprocessError):
        _fail("active_cache_compare_unavailable")
    _check_deadline(deadline)
    return completed.returncode == 0 and completed.stdout == b"match\n"


def _expected_tree_digest(
    installer: Any,
    files: dict[str, str],
    directories: set[str],
    links: dict[str, str] | None = None,
) -> str:
    declared_links = links or {}
    digest = hashlib.sha256()
    for relative in sorted(files.keys() | directories | declared_links.keys(), key=installer._runtime_path_order_key):
        digest.update(relative.encode("utf-8") + b"\0")
        if relative in directories:
            digest.update(b"d\0")
        elif relative in declared_links:
            digest.update(b"l\0" + declared_links[relative].encode("utf-8") + b"\0")
        else:
            digest.update(b"f\0" + bytes.fromhex(files[relative]))
    return digest.hexdigest()


def _add_generated_payload(
    installer: Any,
    files: dict[str, str],
    directories: set[str],
    *,
    source_sha: str,
    runtime_provider: dict[str, object] | None,
    runtime_provider_source: Path | None = None,
) -> dict[str, str]:
    payload_links: dict[str, str] = {}
    source_raw = (source_sha + "\n").encode("ascii")
    files["source-sha"] = hashlib.sha256(source_raw).hexdigest()
    if runtime_provider is not None:
        provider_raw = (
            json.dumps(
                {**runtime_provider, "source_sha": source_sha},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
        files["runtime-provider.json"] = hashlib.sha256(provider_raw).hexdigest()
        if runtime_provider.get("version") == 2:
            engine_files = runtime_provider.get("engine_files")
            engine_links = runtime_provider.get("engine_links")
            provider_sha = runtime_provider.get("provider_sha")
            if not isinstance(engine_files, dict) or not isinstance(engine_links, dict):
                _fail("active_payload_mismatch")
            if provider_sha == source_sha:
                provider_root = runtime_provider_source
                if provider_root is None:
                    provider_root = installer.PAYLOAD_ROOT / source_sha / "provider-runtime"
                for relative, _digest in sorted(engine_files.items()):
                    if relative in engine_links:
                        continue
                    source = provider_root / relative
                    installer._regular(
                        source,
                        allow_sandbox=relative == installer.RUNTIME_SANDBOX_RELATIVE.as_posix(),
                    )
                    files[f"provider-runtime/{relative}"] = (
                        installer._sandbox_digest(source)
                        if relative == installer.RUNTIME_SANDBOX_RELATIVE.as_posix()
                        else installer._digest_regular(source)
                    )
                    if files[f"provider-runtime/{relative}"] != _digest:
                        _fail("active_payload_mismatch")
                    parent = PurePosixPath(f"provider-runtime/{relative}").parent
                    while str(parent) != ".":
                        directories.add(parent.as_posix())
                        parent = parent.parent
                lock_source = provider_root / "web/package-lock.json"
                installer._regular(lock_source)
                files["provider-runtime/web/package-lock.json"] = installer._digest_regular(lock_source)
                for relative, target in engine_links.items():
                    payload_links[f"provider-runtime/{relative}"] = target
                    parent = PurePosixPath(f"provider-runtime/{relative}").parent
                    while str(parent) != ".":
                        directories.add(parent.as_posix())
                        parent = parent.parent
    directories.update({"platform", "platform/tools", "runtime"})
    for relative in files:
        parent = PurePosixPath(relative).parent
        while str(parent) != ".":
            directories.add(parent.as_posix())
            parent = parent.parent
    return payload_links


def _load_capture_probe(*, deadline: float) -> Any:
    # The recovery executor installs this sibling from the current signed
    # recovery generation and binds it in that generation's closed manifest.
    path = Path(__file__).with_name("platform_release_capture_probe.py")
    try:
        return _load_python_source_module(
            path,
            expected_digest=None,
            maximum=2 * 1024 * 1024,
            module_name="_signed_recovery_capture_probe",
            failure="probe_helper",
            deadline=deadline,
        )
    except ActiveArtifactProofError:
        raise
    except Exception:
        _fail("probe_helper_unavailable")


def _load_validator_from_active(
    validator_path: Path,
    expected_archive_digest: str,
    *,
    deadline: float,
) -> Any:
    return _load_python_source_module(
        validator_path,
        expected_digest=expected_archive_digest,
        maximum=2 * 1024 * 1024,
        module_name="_original_release_validate_artifact",
        failure="active_validator",
        deadline=deadline,
    )


def _load_python_source_module(
    path: Path,
    *,
    expected_digest: str | None,
    maximum: int,
    module_name: str,
    failure: str,
    deadline: float,
) -> Any:
    descriptor, before = _safe_release_file(path, maximum=maximum)
    try:
        source = bytearray()
        while True:
            _check_deadline(deadline)
            chunk = os.read(descriptor, min(CHUNK_BYTES, maximum + 1 - len(source)))
            if not chunk:
                break
            source.extend(chunk)
            if len(source) > maximum:
                _fail(f"{failure}_bound")
        after = os.fstat(descriptor)
    except OSError:
        _fail(f"{failure}_read")
    finally:
        os.close(descriptor)
    if len(source) != before.st_size or _fingerprint(after) != _fingerprint(before):
        _fail(f"{failure}_changed")
    digest = hashlib.sha256(source).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        _fail(f"{failure}_mismatch")
    try:
        module = types.ModuleType(module_name)
        module.__file__ = str(path)
        module.__verified_sha256__ = digest
        exec(compile(bytes(source), str(path), "exec"), module.__dict__)
    except ActiveArtifactProofError:
        raise
    except Exception:
        _fail(f"{failure}_unavailable")
    final_digest, final_metadata = _hash_regular(
        path,
        maximum=maximum,
        deadline=deadline,
    )
    if final_digest != digest or _fingerprint(final_metadata) != _fingerprint(before):
        _fail(f"{failure}_changed")
    return module


def verify_active_release_artifact(
    app_dir: Path,
    artifact_tar: Path,
    checksum_path: Path,
    attestation_receipt: Path,
    *,
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
) -> dict[str, object]:
    """Return a closed pass record only for an exact, already-active 9ce tree.

    ``artifact_tar`` and its adjacent ``.sha256`` are staged by the signed
    recovery workflow. ``attestation_receipt`` is written only after both
    pinned GitHub verification policies have succeeded. All inputs and the
    target release are rechecked for stable identity; there are no writes.
    """
    if type(deadline_seconds) is not int or not 30 <= deadline_seconds <= 900:
        _fail("deadline_invalid")
    deadline = time.monotonic() + deadline_seconds
    if artifact_tar.name != f"{RELEASE_SLUG}.tar.gz" or checksum_path != artifact_tar.with_name(f"{artifact_tar.name}.sha256"):
        _fail("archive_path_binding")
    if not artifact_tar.is_absolute() or not checksum_path.is_absolute() or not attestation_receipt.is_absolute():
        _fail("archive_path_binding")

    receipt, receipt_identity = _validate_receipt(attestation_receipt)
    expected, identity_before = _check_current(app_dir)
    tar_fd, tar_before = _safe_file(artifact_tar, maximum=MAX_ARCHIVE_BYTES)
    try:
        tar_digest = _hash_fd(tar_fd, deadline=deadline)
        tar_after = os.fstat(tar_fd)
    except OSError:
        _fail("archive_read")
    finally:
        os.close(tar_fd)
    if (
        tar_digest != RELEASE_TAR_SHA256
        or tar_digest != receipt.get("tar_sha256")
        or _fingerprint(tar_after) != _fingerprint(tar_before)
    ):
        _fail("archive_digest")

    try:
        checksum_fd, checksum_metadata = _safe_file(checksum_path, maximum=MAX_CHECKSUM_BYTES)
        try:
            raw_checksum = os.read(checksum_fd, MAX_CHECKSUM_BYTES + 1)
            checksum_after = os.fstat(checksum_fd)
        finally:
            os.close(checksum_fd)
    except ActiveArtifactProofError:
        raise
    except OSError:
        _fail("checksum_unavailable")
    expected_checksum = f"{RELEASE_TAR_SHA256}  {artifact_tar.name}\n".encode("ascii")
    if raw_checksum != expected_checksum or _fingerprint(checksum_after) != _fingerprint(checksum_metadata):
        _fail("checksum_binding")

    validator_path = expected / "tools/platform_validate_release_artifact.py"
    installer_path = expected / "tools/platform_live_qa_runtime_install.py"
    try:
        with tarfile.open(artifact_tar, mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_MEMBERS:
                _fail("archive_member_bound")
            by_name = {member.name: member for member in members}
            if len(by_name) != len(members):
                _fail("archive_duplicate_member")
            validator_member = by_name.get(f"{RELEASE_SLUG}/tools/platform_validate_release_artifact.py")
            installer_member = by_name.get(f"{RELEASE_SLUG}/tools/platform_live_qa_runtime_install.py")
            release_member = by_name.get(f"{RELEASE_SLUG}/RELEASE.json")
            if any(member is None or not member.isfile() for member in (validator_member, installer_member, release_member)):
                _fail("archive_required_tool")
            validator_digest = _safe_member_digest(archive, validator_member, deadline=deadline)
            installer_digest = _safe_member_digest(archive, installer_member, deadline=deadline)
            release_json_digest = _safe_member_digest(archive, release_member, deadline=deadline)
            validator = _load_validator_from_active(
                validator_path,
                validator_digest,
                deadline=deadline,
            )
            if validator._checksum_contract(artifact_tar, checksum_path) != RELEASE_TAR_SHA256:
                _fail("canonical_checksum")
            release_payload = validator.validate_archive(
                artifact_tar,
                release_slug=RELEASE_SLUG,
                expected_source_commit=SOURCE_SHA,
            )
            if (
                release_payload.get("source_git_commit") != SOURCE_SHA
                or release_payload.get("release_slug") != RELEASE_SLUG
            ):
                _fail("release_identity")
            active_release_digest, active_release_stat = _hash_regular(
                expected / "RELEASE.json",
                maximum=validator.MAX_RELEASE_JSON_BYTES,
                deadline=deadline,
            )
            if active_release_digest != release_json_digest:
                _fail("active_release_identity")

            probe = _load_capture_probe(deadline=deadline)
            installer_module = _load_active_installer(
                installer_path,
                installer_digest,
                probe=probe,
                deadline=deadline,
            )
            (
                expected_files,
                expected_directories,
                mapped_file_count,
                mapped_bytes,
                optional_cache_count,
            ) = _read_mapped_inputs(
                installer_module,
                artifact_tar,
                expected,
                deadline=deadline,
            )
            if len(expected_files) > installer_module.MAX_FILES or mapped_bytes > installer_module.MAX_RUNTIME_BYTES:
                _fail("payload_map_bound")

            # This canonical call is read-only and independently validates the
            # already-active payload, provider closure and trusted entrypoints.
            actual_manifest = installer_module.verify(APP_DIR, SOURCE_SHA)
            runtime_manifest = installer_module._validate_runtime_source(
                expected / "liveqa-runtime", source_sha=SOURCE_SHA
            )
            runtime_provider = None
            runtime_provider_source = None
            if runtime_manifest.get("version") == 2:
                runtime_provider = installer_module._runtime_provider_payload(
                    APP_DIR,
                    expected_manifest=runtime_manifest,
                )
            elif runtime_manifest.get("version") == 3:
                runtime_provider, runtime_provider_source = installer_module._runtime_provider_plan(
                    APP_DIR,
                    candidate_source_sha=SOURCE_SHA,
                    expected_manifest=runtime_manifest,
                )
            expected_links = _add_generated_payload(
                installer_module,
                expected_files,
                expected_directories,
                source_sha=SOURCE_SHA,
                runtime_provider=runtime_provider,
                runtime_provider_source=runtime_provider_source,
            )
            expected_digest = _expected_tree_digest(
                installer_module,
                expected_files,
                expected_directories,
                expected_links,
            )
            matches, _evidence = probe._compare_expected_payload(
                expected_digest,
                expected_files,
                actual_manifest,
            )
            if not matches:
                _fail("active_payload_mismatch")

            pointer_after = _check_current(app_dir)
            if pointer_after[0] != expected or pointer_after[1] != identity_before:
                _fail("active_release_changed")
            tar_final = artifact_tar.lstat()
            checksum_final = checksum_path.lstat()
            receipt_final = attestation_receipt.lstat()
            release_digest_final, release_stat_final = _hash_regular(
                expected / "RELEASE.json",
                maximum=validator.MAX_RELEASE_JSON_BYTES,
                deadline=deadline,
            )
            validator_digest_final, _ = _hash_regular(
                validator_path,
                maximum=2 * 1024 * 1024,
                deadline=deadline,
            )
            installer_digest_final, _ = _hash_regular(
                installer_path,
                maximum=2 * 1024 * 1024,
                deadline=deadline,
            )
            probe_digest_final, _ = _hash_regular(
                Path(probe.__file__),
                maximum=2 * 1024 * 1024,
                deadline=deadline,
            )
            if (
                _fingerprint(tar_final) != _fingerprint(tar_before)
                or _fingerprint(checksum_final) != _fingerprint(checksum_metadata)
                or _fingerprint(receipt_final) != receipt_identity
                or release_digest_final != release_json_digest
                or _fingerprint(release_stat_final) != _fingerprint(active_release_stat)
                or validator_digest_final != validator_digest
                or installer_digest_final != installer_digest
                or probe_digest_final != probe.__verified_sha256__
            ):
                _fail("input_changed")
            _check_deadline(deadline)
            return {
                "schema": 1,
                "status": "passed",
                "proof": "active_signed_artifact_reuse",
                "source_sha": SOURCE_SHA,
                "release_slug": RELEASE_SLUG,
                "artifact_id": ARTIFACT_ID,
                "run_id": DEPLOY_RUN_ID,
                "run_attempt": DEPLOY_RUN_ATTEMPT,
                "artifact_zip_sha256": ARTIFACT_ZIP_SHA256,
                "tar_sha256": RELEASE_TAR_SHA256,
                "release_json_sha256": release_json_digest,
                "payload_tree_sha256": expected_digest,
                "payload_file_count": len(expected_files),
                "payload_directory_count": len(expected_directories),
                "mapped_input_file_count": mapped_file_count,
                "mapped_input_bytes": mapped_bytes,
                "optional_cache_file_count": optional_cache_count,
                "attestation_policies": [item["id"] for item in receipt["policies"]],
            }
    except ActiveArtifactProofError:
        raise
    except Exception:
        _fail("proof_refused")


def _load_active_installer(
    path: Path,
    expected_digest: str,
    *,
    probe: Any,
    deadline: float,
) -> Any:
    _check_deadline(deadline)
    return probe._load_pinned_runtime_installer(path, expected_digest)
