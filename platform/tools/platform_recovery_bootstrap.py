#!/usr/bin/env python3
"""Build, validate, install and run the retained-release recovery bootstrap.

The recovery bootstrap is a deliberately small, release-independent control
plane.  It is not an application artifact: it contains no application code,
Python dependencies, environment files, credentials or Alembic runner.  A
trusted CI job builds a deterministic archive, while the secret-bearing
workflow only validates that archive and asks the host to install one
content-addressed generation.  The generation's fixed entrypoint is the only
recovery command executed after installation.

The old release recorded in ``.release-operation.json`` remains the source of
service units, Nginx configuration and runtime configuration.  This module
owns receipt validation, lock/transaction/systemd control and the corrected
live-QA reconciliation helper used during retained recovery.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import zipfile


SCHEMA = 1
CAPABILITY = "abort_retained_only"
RECOVER_PENDING_CAPABILITY = "recover_pending"
ENTRYPOINT = "platform_abort_retained_only.sh"
MEMBER_ROOT = "platform-recovery-bootstrap"
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
ATTEMPT_RE = RUN_ID_RE
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORKFLOW_RE = re.compile(r"^[A-Za-z0-9_. -]{1,128}$")
JOB_RE = re.compile(r"^[A-Za-z0-9_. -]{1,128}$")
MAX_FILE_BYTES = 768 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_MEMBER_BYTES = 8 * 1024 * 1024
MAX_FILES = 32
MAX_MANIFEST_BYTES = 256 * 1024
MAX_PROVENANCE_BYTES = 64 * 1024
EXECUTABLE_MODE = 0o555
DATA_MODE = 0o444

# This is intentionally closed.  Do not add application, dependency, env,
# secret, migration-runner or source-checkout files here.
RECOVERY_FILES = (
    ENTRYPOINT,
    "platform_recovery_bootstrap.py",
    "platform_release_lock.sh",
    "platform_release_transaction.py",
    "platform_release_restore_runtime.sh",
    "platform_release_systemd_state.py",
    "platform_live_qa_guard.py",
    "platform_live_qa_runtime_install.py",
    "platform_recover_pending.sh",
)


class RecoveryBootstrapError(ValueError):
    """The recovery bootstrap archive or host state is not safe."""


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryBootstrapError("recovery bootstrap JSON has duplicate keys")
        result[key] = value
    return result


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_source_file(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RecoveryBootstrapError("recovery source file is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_size > MAX_FILE_BYTES
    ):
        raise RecoveryBootstrapError("recovery source file metadata is unsafe")
    return metadata


def _safe_source_root(path: Path) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RecoveryBootstrapError("recovery source root is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_nlink < 2
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or resolved != path
    ):
        raise RecoveryBootstrapError("recovery source root metadata is unsafe")


def _read_source_file(path: Path) -> bytes:
    before = _safe_source_file(path)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RecoveryBootstrapError("recovery source file cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size != before.st_size
        ):
            raise RecoveryBootstrapError("recovery source file changed")
        data = bytearray()
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(descriptor, MAX_FILE_BYTES + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_nlink != 1
            or after.st_size != opened.st_size
            or len(data) != after.st_size
            or len(data) > MAX_FILE_BYTES
        ):
            raise RecoveryBootstrapError("recovery source file changed")
        return bytes(data)
    except OSError as exc:
        raise RecoveryBootstrapError("recovery source file cannot be read") from exc
    finally:
        os.close(descriptor)


def _metadata_matches(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare all file facts that can change while a descriptor is read."""

    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_mode == right.st_mode
        and left.st_uid == right.st_uid
        and left.st_gid == right.st_gid
        and left.st_nlink == right.st_nlink
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _read_stable_file(
    path: Path,
    *,
    maximum: int,
    label: str,
    require_root: bool = False,
    mode: int | None = None,
    allowed_modes: set[int] | None = None,
) -> bytes:
    """Read one regular file through one no-follow descriptor.

    Validation, hashing and the returned bytes must describe the same inode.
    In particular, no caller may validate a pathname and then use a second
    pathname lookup for the trusted contents.
    """

    try:
        before = path.lstat()
    except OSError as exc:
        raise RecoveryBootstrapError(f"{label} is unavailable") from exc
    before_mode = stat.S_IMODE(before.st_mode)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (require_root and (before.st_uid != 0 or before.st_gid != 0))
        or (mode is not None and before_mode != mode)
        or (allowed_modes is not None and before_mode not in allowed_modes)
        or before.st_size > maximum
    ):
        raise RecoveryBootstrapError(f"{label} metadata is unsafe")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RecoveryBootstrapError(f"{label} cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if not _metadata_matches(opened, before):
            raise RecoveryBootstrapError(f"{label} changed during validation")
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(descriptor, maximum + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if (
            not _metadata_matches(after, opened)
            or len(data) != after.st_size
            or len(data) > maximum
        ):
            raise RecoveryBootstrapError(f"{label} changed during read")
        return bytes(data)
    except OSError as exc:
        raise RecoveryBootstrapError(f"{label} cannot be read") from exc
    finally:
        os.close(descriptor)


def _validate_source_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or name not in RECOVERY_FILES
        or PurePosixPath(name).is_absolute()
        or "\\" in name
        or any(part in {"", ".", ".."} for part in PurePosixPath(name).parts)
    ):
        raise RecoveryBootstrapError("recovery closure contains an invalid path")


def _source_path(source_root: Path, name: str) -> Path:
    _validate_source_name(name)
    path = source_root / "platform" / "tools" / name
    tools_root = source_root / "platform" / "tools"
    if path.parent != tools_root:
        raise RecoveryBootstrapError("recovery source path escaped tools")
    return path


def _validate_source_tree(source_root: Path) -> None:
    _safe_source_root(source_root)
    _safe_source_root(source_root / "platform")
    _safe_source_root(source_root / "platform" / "tools")
    for name in RECOVERY_FILES:
        _safe_source_file(_source_path(source_root, name))


def _provenance_schema(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise RecoveryBootstrapError("recovery provenance is not an object")
    expected = {
        "repository",
        "workflow",
        "job",
        "run_id",
        "run_attempt",
        "source_sha",
        "artifact_name",
        "artifact_sha256",
        "deployable",
    }
    if set(payload) != expected:
        raise RecoveryBootstrapError("recovery provenance schema is not closed")
    repository = payload.get("repository")
    workflow = payload.get("workflow")
    job = payload.get("job")
    run_id = payload.get("run_id")
    run_attempt = payload.get("run_attempt")
    source_sha = payload.get("source_sha")
    artifact_name = payload.get("artifact_name")
    artifact_sha256 = payload.get("artifact_sha256")
    if not isinstance(repository, str) or REPOSITORY_RE.fullmatch(repository) is None:
        raise RecoveryBootstrapError("recovery repository provenance is invalid")
    if not isinstance(workflow, str) or WORKFLOW_RE.fullmatch(workflow) is None:
        raise RecoveryBootstrapError("recovery workflow provenance is invalid")
    if not isinstance(job, str) or JOB_RE.fullmatch(job) is None:
        raise RecoveryBootstrapError("recovery job provenance is invalid")
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise RecoveryBootstrapError("recovery run provenance is invalid")
    if not isinstance(run_attempt, str) or ATTEMPT_RE.fullmatch(run_attempt) is None:
        raise RecoveryBootstrapError("recovery attempt provenance is invalid")
    if not isinstance(source_sha, str) or SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise RecoveryBootstrapError("recovery source provenance is invalid")
    if (
        not isinstance(artifact_name, str)
        or not (
            artifact_name.startswith("platform-ci-route-")
            or artifact_name.startswith("platform-recovery-bootstrap-")
        )
    ):
        raise RecoveryBootstrapError("recovery artifact provenance is invalid")
    if not isinstance(artifact_sha256, str) or HEX64_RE.fullmatch(artifact_sha256) is None:
        raise RecoveryBootstrapError("recovery artifact digest provenance is invalid")
    if payload.get("deployable") is not False:
        raise RecoveryBootstrapError("recovery bootstrap must be non-deployable")
    return payload


def _read_bounded_json(
    path: Path, *, maximum: int, label: str, require_root: bool = False
) -> object:
    try:
        raw = _read_stable_file(
            path,
            maximum=maximum,
            label=label,
            require_root=require_root,
            allowed_modes={0o400, 0o444, 0o600},
        )
    except RecoveryBootstrapError:
        raise
    try:
        return json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, RecoveryBootstrapError) as exc:
        raise RecoveryBootstrapError(f"{label} is invalid") from exc


def _manifest(
    *, source_sha: str, provenance: dict[str, object], records: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "capability": CAPABILITY,
        "capabilities": [CAPABILITY, RECOVER_PENDING_CAPABILITY],
        "entrypoint": ENTRYPOINT,
        "source_sha": source_sha,
        "deployable": False,
        "provenance": provenance,
        "limits": {
            "max_archive_bytes": MAX_ARCHIVE_BYTES,
            "max_file_bytes": MAX_FILE_BYTES,
            "max_total_member_bytes": MAX_TOTAL_MEMBER_BYTES,
            "max_files": MAX_FILES,
        },
        "files": records,
    }


def _zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(f"{MEMBER_ROOT}/{name}")
    info.date_time = (1980, 1, 1, 0, 0, 0)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.flag_bits = 0
    return info


def build_bundle(
    source_root: Path,
    *,
    source_sha: str,
    provenance: dict[str, object],
    output: Path,
) -> dict[str, object]:
    """Build a deterministic immutable archive and verify it before returning."""

    if SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise RecoveryBootstrapError("recovery source SHA is invalid")
    provenance = _provenance_schema(provenance)
    if provenance["source_sha"] != source_sha:
        raise RecoveryBootstrapError("recovery source SHA does not match provenance")
    _validate_source_tree(source_root)
    if len(RECOVERY_FILES) > MAX_FILES:
        raise RecoveryBootstrapError("recovery closure exceeds its bound")
    contents: dict[str, bytes] = {}
    records: list[dict[str, object]] = []
    for name in RECOVERY_FILES:
        data = _read_source_file(_source_path(source_root, name))
        mode = EXECUTABLE_MODE if name.endswith(".sh") else DATA_MODE
        contents[name] = data
        records.append({"path": name, "sha256": _sha256(data), "mode": mode})
    records.sort(key=lambda value: str(value["path"]))
    manifest = _manifest(source_sha=source_sha, provenance=provenance, records=records)
    manifest_bytes = _canonical_json(manifest)
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise RecoveryBootstrapError("recovery manifest exceeds its bound")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w+b") as stream:
            with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
                for name in sorted(RECOVERY_FILES):
                    archive.writestr(_zip_info(name, EXECUTABLE_MODE if name.endswith(".sh") else DATA_MODE), contents[name])
                archive.writestr(_zip_info("manifest.json", DATA_MODE), manifest_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        metadata = temporary.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_ARCHIVE_BYTES
        ):
            raise RecoveryBootstrapError("recovery archive metadata is unsafe")
        os.replace(temporary, output)
        directory_fd = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        if isinstance(exc, RecoveryBootstrapError):
            raise
        raise RecoveryBootstrapError("recovery archive could not be written") from exc
    return verify_bundle(output, expected_source_sha=source_sha, expected_provenance=provenance)


def _safe_member(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if (
        not name.startswith(f"{MEMBER_ROOT}/")
        or name.count("/") != 1
        or "\\" in name
        or info.is_dir()
        or info.compress_type != zipfile.ZIP_STORED
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise RecoveryBootstrapError("recovery archive member path is unsafe")
    mode = (info.external_attr >> 16) & 0o177777
    if not stat.S_ISREG(mode):
        raise RecoveryBootstrapError("recovery archive member type is unsafe")
    return name.split("/", 1)[1]


def _validate_manifest(
    manifest: object,
    *,
    expected_source_sha: str | None,
    expected_provenance: dict[str, object] | None,
) -> dict[str, object]:
    if not isinstance(manifest, dict):
        raise RecoveryBootstrapError("recovery manifest is not an object")
    expected_keys = {"schema", "capability", "capabilities", "entrypoint", "source_sha", "deployable", "provenance", "limits", "files"}
    if set(manifest) != expected_keys:
        raise RecoveryBootstrapError("recovery manifest schema is not closed")
    if manifest.get("schema") != SCHEMA or manifest.get("capability") != CAPABILITY or manifest.get("entrypoint") != ENTRYPOINT:
        raise RecoveryBootstrapError("recovery manifest capability is invalid")
    if manifest.get("capabilities") != [CAPABILITY, RECOVER_PENDING_CAPABILITY]:
        raise RecoveryBootstrapError("recovery manifest capabilities are invalid")
    if manifest.get("deployable") is not False:
        raise RecoveryBootstrapError("recovery manifest is deployable")
    source_sha = manifest.get("source_sha")
    if not isinstance(source_sha, str) or SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise RecoveryBootstrapError("recovery manifest source SHA is invalid")
    if expected_source_sha is not None and source_sha != expected_source_sha:
        raise RecoveryBootstrapError("recovery manifest source SHA does not match")
    provenance = _provenance_schema(manifest.get("provenance"))
    if provenance["source_sha"] != source_sha:
        raise RecoveryBootstrapError("recovery provenance source SHA does not match")
    if expected_provenance is not None and provenance != expected_provenance:
        raise RecoveryBootstrapError("recovery provenance does not match")
    limits = manifest.get("limits")
    if limits != {
        "max_archive_bytes": MAX_ARCHIVE_BYTES,
        "max_file_bytes": MAX_FILE_BYTES,
        "max_total_member_bytes": MAX_TOTAL_MEMBER_BYTES,
        "max_files": MAX_FILES,
    }:
        raise RecoveryBootstrapError("recovery manifest limits are invalid")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != len(RECOVERY_FILES):
        raise RecoveryBootstrapError("recovery manifest inventory is invalid")
    expected = set(RECOVERY_FILES)
    actual: list[str] = []
    for record in files:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "mode"}:
            raise RecoveryBootstrapError("recovery manifest file record is invalid")
        path = record.get("path")
        digest = record.get("sha256")
        mode = record.get("mode")
        if (
            not isinstance(path, str)
            or path not in expected
            or path in actual
            or not isinstance(digest, str)
            or HEX64_RE.fullmatch(digest) is None
            or type(mode) is not int
            or mode != (EXECUTABLE_MODE if path.endswith(".sh") else DATA_MODE)
        ):
            raise RecoveryBootstrapError("recovery manifest file record is invalid")
        actual.append(path)
    if actual != sorted(expected):
        raise RecoveryBootstrapError("recovery manifest inventory ordering is invalid")
    return manifest


def verify_bundle(
    bundle: Path,
    *,
    expected_source_sha: str | None = None,
    expected_provenance: dict[str, object] | None = None,
) -> dict[str, object]:
    try:
        metadata = bundle.lstat()
    except OSError as exc:
        raise RecoveryBootstrapError("recovery archive is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_ARCHIVE_BYTES
    ):
        raise RecoveryBootstrapError("recovery archive metadata is unsafe")
    try:
        descriptor = os.open(
            bundle,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RecoveryBootstrapError("recovery archive cannot be opened") from exc
    stream = None
    try:
        opened = os.fstat(descriptor)
        if not _metadata_matches(opened, metadata):
            raise RecoveryBootstrapError("recovery archive changed during validation")
        stream = os.fdopen(descriptor, "rb", closefd=False)
        with zipfile.ZipFile(stream, mode="r", allowZip64=False) as archive:
            infos = archive.infolist()
            if len(infos) != len(RECOVERY_FILES) + 1:
                raise RecoveryBootstrapError("recovery archive member count is invalid")
            members: dict[str, bytes] = {}
            modes: dict[str, int] = {}
            total_member_bytes = 0
            for info in infos:
                name = _safe_member(info)
                if (
                    name in members
                    or info.file_size > MAX_FILE_BYTES
                    or total_member_bytes > MAX_TOTAL_MEMBER_BYTES - info.file_size
                ):
                    raise RecoveryBootstrapError("recovery archive member is invalid")
                data = archive.read(info)
                if len(data) != info.file_size or len(data) > MAX_FILE_BYTES:
                    raise RecoveryBootstrapError("recovery archive member size is invalid")
                members[name] = data
                modes[name] = (info.external_attr >> 16) & 0o7777
                total_member_bytes += len(data)
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while total <= MAX_ARCHIVE_BYTES:
            chunk = os.read(descriptor, MAX_ARCHIVE_BYTES + 1 - total)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        if (
            not _metadata_matches(after, opened)
            or total != after.st_size
            or total > MAX_ARCHIVE_BYTES
        ):
            raise RecoveryBootstrapError("recovery archive changed during read")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        if isinstance(exc, RecoveryBootstrapError):
            raise
        raise RecoveryBootstrapError("recovery archive is invalid") from exc
    finally:
        if stream is not None:
            stream.close()
        try:
            os.close(descriptor)
        except OSError:
            pass
    expected_members = set(RECOVERY_FILES) | {"manifest.json"}
    if set(members) != expected_members:
        raise RecoveryBootstrapError("recovery archive inventory is not closed")
    if modes.get("manifest.json") != DATA_MODE:
        raise RecoveryBootstrapError("recovery manifest mode is invalid")
    for name in RECOVERY_FILES:
        expected_mode = EXECUTABLE_MODE if name.endswith(".sh") else DATA_MODE
        if modes.get(name) != expected_mode:
            raise RecoveryBootstrapError("recovery member mode is invalid")
    try:
        manifest = json.loads(members["manifest.json"].decode("ascii"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, RecoveryBootstrapError) as exc:
        raise RecoveryBootstrapError("recovery manifest encoding is invalid") from exc
    manifest = _validate_manifest(
        manifest,
        expected_source_sha=expected_source_sha,
        expected_provenance=expected_provenance,
    )
    records = {str(record["path"]): record for record in manifest["files"]}
    for name, data in members.items():
        if name == "manifest.json":
            continue
        if _sha256(data) != records[name]["sha256"]:
            raise RecoveryBootstrapError("recovery member digest is invalid")
    return {
        "manifest": manifest,
        "bundle_sha256": digest.hexdigest(),
        "members": members,
    }


def _safe_host_directory(path: Path, *, mode: int | None = None) -> os.stat_result:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RecoveryBootstrapError("recovery host directory is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink < 2
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or resolved != path
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
    ):
        raise RecoveryBootstrapError("recovery host directory metadata is unsafe")
    return metadata


def _safe_generation_file(path: Path, *, mode: int, maximum: int = MAX_FILE_BYTES) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RecoveryBootstrapError("recovery generation file is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_size > maximum
    ):
        raise RecoveryBootstrapError("recovery generation file metadata is unsafe")
    return metadata


def _validate_generation_tree(
    generation: Path,
    *,
    bundle_sha: str,
    require_generation_name: bool = True,
    expected_members: dict[str, bytes] | None = None,
    expected_provenance: dict[str, object] | None = None,
    required_capability: str = CAPABILITY,
) -> None:
    if not HEX64_RE.fullmatch(bundle_sha) or (
        require_generation_name and generation.name != bundle_sha
    ):
        raise RecoveryBootstrapError("recovery generation identity is invalid")
    _safe_host_directory(generation, mode=0o555)
    members = list(generation.iterdir())
    if len(members) != len(RECOVERY_FILES) + 1:
        raise RecoveryBootstrapError("recovery generation inventory is invalid")
    for path in sorted(members, key=lambda item: item.name):
        if path.name not in set(RECOVERY_FILES) | {"manifest.json"}:
            raise RecoveryBootstrapError("recovery generation has an unexpected member")
        if path.name == "manifest.json":
            _safe_generation_file(path, mode=0o444, maximum=MAX_MANIFEST_BYTES)
        else:
            _safe_generation_file(path, mode=EXECUTABLE_MODE if path.name.endswith(".sh") else DATA_MODE)
    expected_manifest = generation / "manifest.json"
    manifest = _read_bounded_json(
        expected_manifest,
        maximum=MAX_MANIFEST_BYTES,
        label="recovery generation manifest",
        require_root=True,
    )
    manifest = _validate_manifest(
        manifest, expected_source_sha=None, expected_provenance=expected_provenance
    )
    capabilities = manifest.get("capabilities")
    if required_capability not in capabilities:
        raise RecoveryBootstrapError("recovery generation capability is unavailable")
    records = {str(record["path"]): record for record in manifest["files"]}
    for name, record in records.items():
        data = _read_stable_file(
            generation / name,
            maximum=MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_FILE_BYTES,
            label=f"recovery generation member {name}",
            require_root=True,
            mode=0o444 if name == "manifest.json" else (
                EXECUTABLE_MODE if name.endswith(".sh") else DATA_MODE
            ),
        )
        if _sha256(data) != record["sha256"]:
            raise RecoveryBootstrapError("recovery generation member digest is invalid")
    if expected_members is not None:
        if set(expected_members) != set(records) | {"manifest.json"}:
            raise RecoveryBootstrapError("recovery generation does not match the bundle")
        for name, expected in expected_members.items():
            actual = _read_stable_file(
                generation / name,
                maximum=MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_FILE_BYTES,
                label=f"recovery generation member {name}",
                require_root=True,
                mode=0o444 if name == "manifest.json" else (
                    EXECUTABLE_MODE if name.endswith(".sh") else DATA_MODE
                ),
            )
            if actual != expected:
                raise RecoveryBootstrapError("recovery generation does not match the bundle")


def install_bundle(
    bundle: Path,
    *,
    app_dir: Path,
    expected_bundle_sha: str | None = None,
    expected_source_sha: str | None = None,
    expected_provenance: dict[str, object] | None = None,
) -> Path:
    """Atomically install one verified bundle under its content digest."""

    if os.geteuid() != 0:
        raise RecoveryBootstrapError("recovery generation install requires root")
    verified = verify_bundle(
        bundle,
        expected_source_sha=expected_source_sha,
        expected_provenance=expected_provenance,
    )
    bundle_sha = str(verified["bundle_sha256"])
    if expected_bundle_sha is not None and bundle_sha != expected_bundle_sha:
        raise RecoveryBootstrapError("recovery archive digest does not match")
    if not HEX64_RE.fullmatch(bundle_sha):
        raise RecoveryBootstrapError("recovery archive digest is invalid")
    if not app_dir.is_absolute() or app_dir == Path("/"):
        raise RecoveryBootstrapError("recovery application path is invalid")
    _safe_host_directory(app_dir)
    shared = app_dir / "shared"
    _safe_host_directory(shared)
    recovery = shared / ".release-recovery"
    if not os.path.lexists(recovery):
        recovery.mkdir(mode=0o755)
        os.chown(recovery, 0, 0)
        os.chmod(recovery, 0o755)
    _safe_host_directory(recovery, mode=0o755)
    generations = recovery / "generations"
    if not os.path.lexists(generations):
        generations.mkdir(mode=0o755)
        os.chown(generations, 0, 0)
        os.chmod(generations, 0o755)
    _safe_host_directory(generations, mode=0o755)
    target = generations / bundle_sha
    if os.path.lexists(target):
        _validate_generation_tree(
            target,
            bundle_sha=bundle_sha,
            expected_members=verified["members"],
            expected_provenance=expected_provenance,
        )
        return target
    temporary = generations / f".{bundle_sha}.install-{os.getpid()}"
    if os.path.lexists(temporary):
        raise RecoveryBootstrapError("recovery generation staging path already exists")
    temporary.mkdir(mode=0o700)
    os.chown(temporary, 0, 0)
    try:
        members = verified["members"]
        for name in sorted(set(RECOVERY_FILES) | {"manifest.json"}):
            mode = DATA_MODE if name == "manifest.json" or not name.endswith(".sh") else EXECUTABLE_MODE
            destination = temporary / name
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            try:
                view = memoryview(members[name])
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise RecoveryBootstrapError("recovery generation member could not be written")
                    view = view[written:]
                os.fchmod(descriptor, mode)
                os.fchown(descriptor, 0, 0)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.chmod(temporary, 0o555)
        _validate_generation_tree(
            temporary,
            bundle_sha=bundle_sha,
            require_generation_name=False,
            expected_provenance=expected_provenance,
        )
        os.rename(temporary, target)
        directory_fd = os.open(generations, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if os.path.lexists(temporary) and not temporary.is_symlink():
            for item in sorted(temporary.iterdir(), key=lambda value: value.name):
                if item.is_file() and not item.is_symlink():
                    item.unlink()
            temporary.rmdir()
        raise
    return target


def _safe_receipt(path: Path) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > 64 * 1024
    ):
        raise RecoveryBootstrapError("release receipt metadata is unsafe")


def _receipt_json(path: Path) -> dict[str, object]:
    _safe_receipt(path)
    value = _read_bounded_json(
        path, maximum=64 * 1024, label="release receipt", require_root=True
    )
    if not isinstance(value, dict):
        raise RecoveryBootstrapError("release receipt is invalid")
    return value


def _release_pointer(app_dir: Path, name: str) -> Path:
    pointer = app_dir / name
    metadata = pointer.lstat()
    if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_nlink != 1:
        raise RecoveryBootstrapError("release pointer metadata is unsafe")
    target = pointer.resolve(strict=True)
    releases = app_dir / "releases"
    if target.parent != releases or not target.is_dir() or target.is_symlink():
        raise RecoveryBootstrapError("release pointer target is unsafe")
    return target


def _receipt_identity(value: object, *, required: bool) -> dict[str, int] | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"dev", "ino"}
        or type(value.get("dev")) is not int
        or type(value.get("ino")) is not int
        or value["dev"] < 0
        or value["ino"] <= 0
    ):
        raise RecoveryBootstrapError("release receipt identity is invalid")
    return {"dev": value["dev"], "ino": value["ino"]}


def _receipt_release_path(value: object, *, app_dir: Path, label: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RecoveryBootstrapError(f"release receipt {label} is invalid")
    path = Path(value)
    if path.parent != app_dir / "releases" or path.name in {"", ".", ".."}:
        raise RecoveryBootstrapError(f"release receipt {label} is invalid")
    return path


def _validate_receipt_directory(path: Path, identity: dict[str, int], *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RecoveryBootstrapError(f"release receipt {label} is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink < 2
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or {"dev": metadata.st_dev, "ino": metadata.st_ino} != identity
    ):
        raise RecoveryBootstrapError(f"release receipt {label} identity changed")


def _validate_receipt_identity(receipt: dict[str, object], app_dir: Path) -> Path:
    expected = {
        "operation_id",
        "version", "operation", "phase", "app_dir", "current_before", "previous_before", "candidate_release",
        "shared_venv", "peer", "snapshot", "transition", "shared_before", "peer_before", "current_before_identity",
        "previous_before_identity", "candidate_identity", "remove_env_on_recovery", "service_state_before",
        "quiesced_services", "timer_active_before",
    }
    legacy_expected = expected - {"operation_id"}
    legacy = set(receipt) == legacy_expected
    if (
        (set(receipt) != expected and not legacy)
        or type(receipt.get("version")) is not int
        or receipt.get("version") != 2
        or receipt.get("operation") != "install"
    ):
        raise RecoveryBootstrapError("release receipt schema is invalid")
    if not legacy and (
        not isinstance(receipt.get("operation_id"), str)
        or OPERATION_ID_RE.fullmatch(receipt["operation_id"]) is None
    ):
        raise RecoveryBootstrapError("release receipt operation identity is invalid")
    phase = receipt.get("phase")
    if phase in {"migration-pending", "migration-failed", "migration-applied", "activation-pending", "services-restarted", "nginx-pending", "nginx-applied", "smoke-passed", "activation-committed"}:
        raise RecoveryBootstrapError("migration outcome is uncertain")
    if phase != "recovery-restored":
        raise RecoveryBootstrapError("release receipt is not recovery-restored")
    if receipt.get("app_dir") != str(app_dir):
        raise RecoveryBootstrapError("release receipt application identity is invalid")
    if type(receipt.get("remove_env_on_recovery")) is not bool:
        raise RecoveryBootstrapError("release receipt recovery flag is invalid")
    service_state = receipt.get("service_state_before")
    if (
        not isinstance(service_state, dict)
        or set(service_state) != {"deadlock-api", "deadlock-worker", "deadlock-web"}
        or any(type(value) is not str or value not in {"active", "inactive"} for value in service_state.values())
    ):
        raise RecoveryBootstrapError("release receipt service state is invalid")
    if receipt.get("quiesced_services") != ["deadlock-api", "deadlock-worker", "deadlock-web"]:
        raise RecoveryBootstrapError("release receipt quiesced services are invalid")
    if type(receipt.get("timer_active_before")) is not bool:
        raise RecoveryBootstrapError("release receipt timer state is invalid")
    current_before = _receipt_release_path(
        receipt.get("current_before"), app_dir=app_dir, label="current identity"
    )
    if current_before is None:
        raise RecoveryBootstrapError("release receipt current identity is invalid")
    previous_before = _receipt_release_path(
        receipt.get("previous_before"), app_dir=app_dir, label="previous identity"
    )
    current_identity = _receipt_identity(
        receipt.get("current_before_identity"), required=True
    )
    previous_identity = _receipt_identity(
        receipt.get("previous_before_identity"), required=previous_before is not None
    )
    candidate = _receipt_release_path(
        receipt.get("candidate_release"), app_dir=app_dir, label="candidate identity"
    )
    candidate_identity = _receipt_identity(
        receipt.get("candidate_identity"), required=True
    )
    if candidate is None or candidate in {current_before, previous_before}:
        raise RecoveryBootstrapError("release receipt candidate identity is invalid")
    shared_venv = Path(str(receipt.get("shared_venv")))
    snapshot = Path(str(receipt.get("snapshot")))
    peer = Path(str(receipt.get("peer")))
    if shared_venv != app_dir / "shared" / "venv" or snapshot != candidate / ".rollback" / "shared-venv-before-install":
        raise RecoveryBootstrapError("release receipt venv paths are invalid")
    if peer.parent != app_dir / "shared" or not peer.name.startswith(f".venv-install-{candidate.name}."):
        raise RecoveryBootstrapError("release receipt venv peer path is invalid")
    transition = receipt.get("transition")
    shared_before = receipt.get("shared_before")
    peer_before = receipt.get("peer_before")
    if transition == "exchange":
        _receipt_identity(shared_before, required=True)
        _receipt_identity(peer_before, required=True)
        if shared_before == peer_before:
            raise RecoveryBootstrapError("release receipt venv identities are ambiguous")
    elif transition == "create":
        if shared_before is not None or _receipt_identity(peer_before, required=True) is None:
            raise RecoveryBootstrapError("release receipt created venv identity is invalid")
    elif transition == "none":
        if peer_before is not None:
            raise RecoveryBootstrapError("release receipt no-op peer identity is invalid")
        if shared_before is not None:
            _receipt_identity(shared_before, required=True)
    else:
        raise RecoveryBootstrapError("release receipt venv transition is invalid")
    current = _release_pointer(app_dir, "current")
    if current != current_before:
        raise RecoveryBootstrapError("current release does not match receipt")
    _validate_receipt_directory(current, current_identity, label="current release")
    if previous_before is not None:
        if _release_pointer(app_dir, "previous") != previous_before:
            raise RecoveryBootstrapError("previous release does not match receipt")
        assert previous_identity is not None
        _validate_receipt_directory(previous_before, previous_identity, label="previous release")
    elif os.path.lexists(app_dir / "previous"):
        raise RecoveryBootstrapError("unexpected previous release pointer")
    if candidate_identity is None:
        raise RecoveryBootstrapError("release receipt candidate identity is invalid")
    if os.path.lexists(candidate):
        _validate_receipt_directory(candidate, candidate_identity, label="candidate release")
    elif receipt["phase"] != "recovery-restored":
        raise RecoveryBootstrapError("release receipt candidate release is unavailable")
    return current


def abort_retained_only(*, app_dir: Path, generation: Path) -> None:
    """Restore runtime and complete only an already pointer-restored receipt."""

    if os.geteuid() != 0:
        raise RecoveryBootstrapError("retained recovery requires root")
    _validate_generation_tree(generation, bundle_sha=generation.name)
    shared = app_dir / "shared"
    state = shared / ".release-operation.json"
    systemd_state = shared / ".release-systemd-state.json"
    state_present = os.path.lexists(state)
    systemd_state_present = os.path.lexists(systemd_state)
    # A retry after the final transaction receipt was removed is a safe
    # idempotent no-op.  A half-pair is never treated as completed: it could
    # represent an interrupted cleanup or an unrelated host mutation.
    if not state_present:
        if systemd_state_present:
            raise RecoveryBootstrapError("retained recovery receipts are incomplete")
        return
    receipt = _receipt_json(state)
    release = _validate_receipt_identity(receipt, app_dir)
    transaction = generation / "platform_release_transaction.py"
    runtime = generation / "platform_release_restore_runtime.sh"
    systemd = generation / "platform_release_systemd_state.py"
    liveqa = generation / "platform_live_qa_runtime_install.py"
    if "operation_id" not in receipt:
        # The deployed v2 receipt predates operation correlation.  It is a
        # narrowly scoped, read-only compatibility bridge: with no systemd
        # receipt present, prove the peer is already absent and let only the
        # trusted transaction cleanup consume the inactive candidate.  Never
        # synthesize an identity or execute retained runtime helpers for this
        # legacy path.
        if systemd_state_present:
            raise RecoveryBootstrapError(
                "legacy release receipt cannot authorize systemd recovery"
            )
        peer_value = receipt.get("peer")
        if not isinstance(peer_value, str):
            raise RecoveryBootstrapError("release receipt peer identity is invalid")
        peer = Path(peer_value)
        if os.path.lexists(peer):
            raise RecoveryBootstrapError("legacy release receipt peer is present")
        subprocess.run([
            "/usr/bin/python3", "-I", str(transaction), "complete-recovery",
            "--state", str(state), "--retain-receipt",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, close_fds=True)
        subprocess.run([
            "/usr/bin/python3", "-I", str(transaction), "complete-recovery",
            "--state", str(state),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, close_fds=True)
        return
    if systemd_state_present:
        if (
            not isinstance(receipt.get("operation_id"), str)
            or OPERATION_ID_RE.fullmatch(receipt["operation_id"]) is None
        ):
            raise RecoveryBootstrapError(
                "legacy release receipt cannot authorize systemd recovery"
            )
        _safe_receipt(systemd_state)
        # Correlate both durable receipts before the runtime helper can touch
        # unit files, Nginx, or the live-QA runtime.  A stale systemd receipt
        # may be individually valid after a crash; it is not valid for a new
        # transaction merely because the app directory still matches.
        subprocess.run([
            "/usr/bin/python3", "-I", str(systemd), "validate",
            "--state", str(systemd_state),
            "--app-dir", str(app_dir),
            "--helper-release", str(release),
            "--transaction", str(state),
            "--systemctl", "/usr/bin/systemctl",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, close_fds=True)
        command = [
            str(runtime),
            "--app-dir", str(app_dir),
            "--release", str(release),
            "--systemd-state", str(systemd_state),
            "--transaction", str(state),
            "--systemctl", "/usr/bin/systemctl",
            "--live-qa-runtime-installer", str(liveqa),
        ]
        subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, close_fds=True)
        if _release_pointer(app_dir, "current") != release:
            raise RecoveryBootstrapError("current release changed during retained recovery")
        if receipt.get("previous_before") is not None and _release_pointer(app_dir, "previous") != Path(str(receipt["previous_before"])):
            raise RecoveryBootstrapError("previous release changed during retained recovery")
        subprocess.run([
            "/usr/bin/python3", "-I", str(systemd), "verify", "--state", str(systemd_state),
            "--app-dir", str(app_dir), "--helper-release", str(release),
            "--transaction", str(state),
            "--systemctl", "/usr/bin/systemctl",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, close_fds=True)
        # Remove candidate/venv cleanup artifacts but retain the operation
        # receipt until the durable systemd receipt is also cleared.  A crash
        # after either side effect can therefore resume without guessing.
        subprocess.run([
            "/usr/bin/python3", "-I", str(transaction), "complete-recovery", "--state", str(state),
            "--retain-receipt",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, close_fds=True)
        subprocess.run([
            "/usr/bin/python3", "-I", str(systemd), "clear", "--state", str(systemd_state),
            "--app-dir", str(app_dir), "--helper-release", str(release),
            "--transaction", str(state),
            "--systemctl", "/usr/bin/systemctl",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, close_fds=True)
    else:
        # The first attempt may have retained the operation receipt after
        # completing its filesystem cleanup, then removed the systemd receipt
        # before a process failure.  Do not rerun runtime/systemd side effects;
        # prove the cleanup half is complete and finish the receipt deletion.
        candidate = _receipt_release_path(
            receipt.get("candidate_release"), app_dir=app_dir, label="candidate identity"
        )
        peer = Path(str(receipt.get("peer")))
        if os.path.lexists(candidate) or os.path.lexists(peer):
            raise RecoveryBootstrapError("systemd receipt is missing before transaction cleanup")

    # This is the only operation that removes the release receipt.  It is
    # deliberately after successful systemd clear, and is safe to retry after
    # a failure in either prior subprocess.
    subprocess.run([
        "/usr/bin/python3", "-I", str(transaction), "complete-recovery", "--state", str(state),
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, close_fds=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Immutable retained-release recovery bootstrap")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--source-root", type=Path, required=True)
    build.add_argument("--source-sha", required=True)
    build.add_argument("--provenance", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--bundle", type=Path, required=True)
    validate.add_argument("--source-sha")
    validate.add_argument("--provenance", type=Path)
    validate_generation = commands.add_parser("validate-generation")
    validate_generation.add_argument("--generation", type=Path, required=True)
    validate_generation.add_argument("--bundle-sha", required=True)
    validate_generation.add_argument("--provenance", type=Path)
    validate_generation.add_argument(
        "--capability",
        choices=(CAPABILITY, RECOVER_PENDING_CAPABILITY),
        default=CAPABILITY,
    )
    install = commands.add_parser("install")
    install.add_argument("--bundle", type=Path, required=True)
    install.add_argument("--app-dir", type=Path, required=True)
    install.add_argument("--expected-bundle-sha")
    install.add_argument("--source-sha")
    install.add_argument("--provenance", type=Path)
    install.add_argument(
        "--capability",
        choices=(CAPABILITY, RECOVER_PENDING_CAPABILITY),
        default=CAPABILITY,
    )
    abort = commands.add_parser("abort_retained_only")
    abort.add_argument("--app-dir", type=Path, required=True)
    abort.add_argument("--generation", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        expected_provenance = None
        provenance_path = getattr(args, "provenance", None)
        if provenance_path is not None:
            value = _read_bounded_json(provenance_path, maximum=MAX_PROVENANCE_BYTES, label="recovery provenance")
            expected_provenance = _provenance_schema(value)
        if args.command == "build":
            provenance = _read_bounded_json(args.provenance, maximum=MAX_PROVENANCE_BYTES, label="recovery provenance")
            if not isinstance(provenance, dict):
                raise RecoveryBootstrapError("recovery provenance is invalid")
            result = build_bundle(args.source_root, source_sha=args.source_sha, provenance=provenance, output=args.output)
            print(json.dumps({"schema": SCHEMA, "capability": "recovery_bootstrap", "capabilities": [CAPABILITY, RECOVER_PENDING_CAPABILITY], "bundle_sha256": result["bundle_sha256"], "deployable": False}, sort_keys=True))
        elif args.command == "validate":
            result = verify_bundle(args.bundle, expected_source_sha=args.source_sha, expected_provenance=expected_provenance)
            print(json.dumps({"schema": SCHEMA, "capability": "recovery_bootstrap", "capabilities": [CAPABILITY, RECOVER_PENDING_CAPABILITY], "bundle_sha256": result["bundle_sha256"], "deployable": False}, sort_keys=True))
        elif args.command == "validate-generation":
            _validate_generation_tree(
                args.generation,
                bundle_sha=args.bundle_sha,
                expected_provenance=expected_provenance,
                required_capability=args.capability,
            )
            print(json.dumps({"schema": SCHEMA, "capability": args.capability, "generation": str(args.generation), "deployable": False}, sort_keys=True))
        elif args.command == "install":
            target = install_bundle(args.bundle, app_dir=args.app_dir, expected_bundle_sha=args.expected_bundle_sha, expected_source_sha=args.source_sha, expected_provenance=expected_provenance)
            _validate_generation_tree(
                target,
                bundle_sha=target.name,
                expected_provenance=expected_provenance,
                required_capability=args.capability,
            )
            print(json.dumps({"schema": SCHEMA, "capability": args.capability, "generation": str(target), "deployable": False}, sort_keys=True))
        elif args.command == "abort_retained_only":
            generation = args.generation or Path(__file__).resolve().parent
            abort_retained_only(app_dir=args.app_dir, generation=generation)
            print("RECOVERY_BOOTSTRAP schema=1 status=complete capability=abort_retained_only deployable=false")
        else:  # pragma: no cover - argparse enforces commands
            raise RecoveryBootstrapError("unknown recovery command")
        return 0
    except (RecoveryBootstrapError, OSError, subprocess.CalledProcessError) as exc:
        del exc
        print("RECOVERY_BOOTSTRAP schema=1 status=failed capability=abort_retained_only deployable=false", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
