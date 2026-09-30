#!/usr/bin/env python3
"""Build and verify the immutable production host-tools handoff.

This module is intentionally stdlib-only.  CI uses it on a secret-free
runner to create a deterministic archive for an operator/host-image
provisioning step.  The production workflow may verify the archive as data,
but it never executes an installer from it or copies it to the host.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import zipfile


SCHEMA = 1
TOOLSET_VERSION = "production-host-tools-v2"
MAX_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024
MAX_FILE_COUNT = 32
MAX_OUTER_MEMBER_BYTES = MAX_BUNDLE_BYTES
MAX_ZIP_COMPRESSION_RATIO = 100
MAX_ZIP_NAME_BYTES = 256
MEMBER_ROOT = "platform-host-tools"
OUTER_MEMBER_NAME = "platform-host-tools-bundle.zip"
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_FILE_RE = re.compile(r"^platform_[A-Za-z0-9_.-]+\.(?:py|sh)$")

# This is the complete release-independent closure used by the fixed
# dispatcher and production deployment control after one generation is
# provisioned.  Runtime/application files, run wrappers, and the candidate
# deploy closure remain outside this host trust bundle.
PREPARE_ARTIFACT_FILES = (
    "platform_workflow_remote_dispatch.py",
    "platform_workflow_input_guard.py",
    "platform_prepare_artifact_dir.py",
)
PRODUCTION_DEPLOY_CONTROL_FILES = (
    "platform_production_deploy_supervisor.sh",
    "platform_release_lock.sh",
    "platform_release_preflight.sh",
    "platform_validate_release_artifact.py",
    "platform_safe_env_exec.py",
    "platform_render_service_envs.py",
    "platform_validate_edge_policy.py",
    "platform_configure_shared_env.py",
    "platform_update_cloudflare_ips.py",
    "platform_storage_evidence_summary.py",
)
HOST_TOOL_FILES = PREPARE_ARTIFACT_FILES + PRODUCTION_DEPLOY_CONTROL_FILES
COMPONENT_FILES = {
    "prepare_artifact": PREPARE_ARTIFACT_FILES,
    "production_deploy_control": PRODUCTION_DEPLOY_CONTROL_FILES,
}
CAPABILITIES = (
    "artifact_prepare",
    "input_guard",
    "production_dispatcher",
    "production_supervisor",
    "production_deploy_control",
    "python_isolated",
    "python_bytecode_disabled",
)
EXECUTABLE_MODE = 0o555
DATA_MODE = 0o444
HOST_GENERATION_MODE = 0o555
STAGE_MODE = 0o700
EVIDENCE_MODE = 0o600
RENAME_NOREPLACE = 1
HOST_TOOLS_INVENTORY = frozenset((*HOST_TOOL_FILES, "manifest.json", "capabilities.txt"))
MAX_EVIDENCE_BYTES = 64 * 1024
ATTESTATION_MAX_BYTES = 64 * 1024


class HostToolsBundleError(ValueError):
    """Bounded validation failure for the offline host-tools contract."""


def _require_no_follow() -> int:
    flag = getattr(os, "O_NOFOLLOW", 0)
    if not flag:
        raise HostToolsBundleError("host-tools no-follow open primitive is unavailable")
    return flag


def _source_path(source_root: Path, name: str) -> Path:
    if (
        not isinstance(name, str)
        or SAFE_FILE_RE.fullmatch(name) is None
        or name not in HOST_TOOL_FILES
    ):
        raise HostToolsBundleError("host-tools file allowlist is invalid")
    path = source_root / "platform" / "tools" / name
    if path.parent != source_root / "platform" / "tools":
        raise HostToolsBundleError("host-tools source path escaped its directory")
    return path


def _regular_file(path: Path, max_size: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HostToolsBundleError("host-tools source file is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > max_size
    ):
        raise HostToolsBundleError("host-tools source file metadata is unsafe")
    return metadata


def _regular_source(path: Path) -> os.stat_result:
    return _regular_file(path, MAX_FILE_BYTES)


def _regular_directory(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HostToolsBundleError("host-tools source directory is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise HostToolsBundleError("host-tools source directory is unsafe")
    return metadata


def _read_regular_bytes(path: Path, maximum: int, description: str) -> tuple[bytes, os.stat_result]:
    """Read a bounded regular file while retaining an inode identity proof.

    Archive paths and operator handoff files are untrusted even when their
    names are supplied by a reviewed workflow.  Reading through a no-follow
    descriptor and comparing the before/open/after identities prevents a
    replacement race from turning a verified path into different bytes.
    """

    if not isinstance(path, Path) or not path.is_absolute():
        raise HostToolsBundleError(f"{description} path is invalid")
    try:
        before = path.lstat()
    except OSError as exc:
        raise HostToolsBundleError(f"{description} is unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 0
        or before.st_size > maximum
    ):
        raise HostToolsBundleError(f"{description} metadata is unsafe")
    parent_fd: int | None = None
    descriptor: int | None = None
    try:
        parent_fd, _ = _open_no_symlink_directory(path.parent)
        no_follow = _require_no_follow()
        descriptor = os.open(
            path.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | no_follow,
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size < 0
            or opened.st_size > maximum
        ):
            raise HostToolsBundleError(f"{description} changed while opening")
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_nlink != 1
            or not stat.S_ISREG(after.st_mode)
            or after.st_size != opened.st_size
            or len(data) != after.st_size
            or len(data) > maximum
        ):
            raise HostToolsBundleError(f"{description} changed while reading")
        return bytes(data), after
    except HostToolsBundleError:
        raise
    except OSError as exc:
        raise HostToolsBundleError(f"{description} cannot be read") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _write_all(descriptor: int, data: bytes) -> None:
    """Write every byte, including when the kernel performs a short write."""

    offset = 0
    while offset < len(data):
        try:
            written = os.write(descriptor, data[offset:])
        except OSError as exc:
            raise HostToolsBundleError("host-tools file could not be written") from exc
        if written <= 0:
            raise HostToolsBundleError("host-tools file write made no progress")
        offset += written


def _digest_argument(value: str, description: str) -> str:
    if not isinstance(value, str):
        raise HostToolsBundleError(f"{description} is invalid")
    digest = value.removeprefix("sha256:")
    if SHA256_RE.fullmatch(digest) is None:
        raise HostToolsBundleError(f"{description} is invalid")
    return digest


def _zip_member_mode(info: zipfile.ZipInfo) -> int:
    """Return a closed Unix mode for a ZIP member, rejecting special files."""

    # ZIP metadata is attacker-controlled.  A Unix creator with a missing
    # type bit is treated as a regular file; every explicit non-regular type
    # is rejected.  DOS directory attributes are also rejected below.
    mode = (info.external_attr >> 16) & 0o177777
    file_type = stat.S_IFMT(mode)
    if stat.S_ISLNK(mode) or stat.S_ISDIR(mode) or file_type not in {0, stat.S_IFREG}:
        raise HostToolsBundleError("host-tools archive member type is unsafe")
    if info.external_attr & 0x10:
        raise HostToolsBundleError("host-tools archive member type is unsafe")
    return mode & 0o7777


def _reject_zip64(info: zipfile.ZipInfo) -> None:
    # ``allowZip64=False`` rejects ZIP64 central-directory records, but an
    # individual member may still carry a ZIP64 extra field.  Reject the
    # field explicitly and keep the accepted format deterministic.
    cursor = 0
    extra = info.extra
    while cursor + 4 <= len(extra):
        field, size = int.from_bytes(extra[cursor:cursor + 2], "little"), int.from_bytes(
            extra[cursor + 2:cursor + 4], "little"
        )
        cursor += 4
        if cursor + size > len(extra):
            raise HostToolsBundleError("host-tools archive extra field is invalid")
        if field == 0x0001:
            raise HostToolsBundleError("host-tools archive uses ZIP64")
        cursor += size
    if cursor != len(extra) or info.extract_version >= 45:
        raise HostToolsBundleError("host-tools archive uses ZIP64")


def _bounded_zip_info(
    info: zipfile.ZipInfo,
    *,
    expected_name: str | None,
    maximum_member_bytes: int,
    expected_prefix: str | None = None,
) -> str:
    """Validate path/type/size/ratio metadata before reading a member."""

    name = info.filename
    if not isinstance(name, str) or not name or len(name.encode("utf-8", "strict")) > MAX_ZIP_NAME_BYTES:
        raise HostToolsBundleError("host-tools archive member path is unsafe")
    if (
        "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or name.startswith("./")
        or name.endswith("/")
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise HostToolsBundleError("host-tools archive member path is unsafe")
    if expected_name is not None and name != expected_name:
        raise HostToolsBundleError("host-tools archive member allowlist is invalid")
    if expected_prefix is not None and (
        not name.startswith(expected_prefix + "/") or name.count("/") != 1
    ):
        raise HostToolsBundleError("host-tools archive member path is unsafe")
    if info.flag_bits & 0x1:
        raise HostToolsBundleError("host-tools archive member is encrypted")
    _reject_zip64(info)
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise HostToolsBundleError("host-tools archive compression is unsafe")
    _zip_member_mode(info)
    if (
        info.file_size < 0
        or info.compress_size < 0
        or info.file_size > maximum_member_bytes
        or info.compress_size > MAX_ARTIFACT_ARCHIVE_BYTES
    ):
        raise HostToolsBundleError("host-tools archive member exceeds its bound")
    if info.file_size and (
        info.compress_size <= 0
        or info.file_size > info.compress_size * MAX_ZIP_COMPRESSION_RATIO
    ):
        raise HostToolsBundleError("host-tools archive compression ratio exceeds its bound")
    return name


def _read_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, maximum: int) -> bytes:
    try:
        data = archive.read(info)
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        raise HostToolsBundleError("host-tools archive member cannot be read") from exc
    if len(data) != info.file_size or len(data) > maximum:
        raise HostToolsBundleError("host-tools archive member changed while reading")
    return data


def _read_source(path: Path) -> bytes:
    """Read one source member while rejecting replacement races."""

    before = _regular_source(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _require_no_follow()
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HostToolsBundleError("host-tools source file cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size > MAX_FILE_BYTES
        ):
            raise HostToolsBundleError("host-tools source file changed")
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
            or not stat.S_ISREG(after.st_mode)
            or after.st_size != opened.st_size
            or len(data) != after.st_size
            or len(data) > MAX_FILE_BYTES
        ):
            raise HostToolsBundleError("host-tools source file changed")
        return bytes(data)
    except OSError as exc:
        raise HostToolsBundleError("host-tools source file cannot be read") from exc
    finally:
        os.close(descriptor)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")


def _capabilities_text(source_sha: str) -> bytes:
    lines = [
        f"schema={SCHEMA}",
        f"toolset_version={TOOLSET_VERSION}",
        f"source_sha={source_sha}",
        *(f"capability={capability}" for capability in CAPABILITIES),
        *(f"component={component}" for component in COMPONENT_FILES),
        *(
            f"component_file={component}:{name}"
            for component, names in COMPONENT_FILES.items()
            for name in names
        ),
    ]
    return ("\n".join(lines) + "\n").encode("ascii")


def _zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(f"{MEMBER_ROOT}/{name}")
    info.date_time = (1980, 1, 1, 0, 0, 0)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | (mode & 0o7777)) << 16
    info.flag_bits = 0
    return info


def _manifest(source_sha: str, records: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "toolset_version": TOOLSET_VERSION,
        "source_sha": source_sha,
        "generation": source_sha,
        "capabilities": list(CAPABILITIES),
        "components": {
            component: list(names) for component, names in COMPONENT_FILES.items()
        },
        "limits": {
            "max_bundle_bytes": MAX_BUNDLE_BYTES,
            "max_file_bytes": MAX_FILE_BYTES,
            "max_file_count": MAX_FILE_COUNT,
        },
        "files": records,
    }


def build_bundle(source_root: Path, source_sha: str, output: Path) -> dict[str, object]:
    """Create a deterministic ZIP and return its verified manifest summary."""

    if SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise HostToolsBundleError("source SHA is invalid")
    if len(HOST_TOOL_FILES) > MAX_FILE_COUNT:
        raise HostToolsBundleError("host-tools closure exceeds its file bound")

    _regular_directory(source_root)
    _regular_directory(source_root / "platform")
    _regular_directory(source_root / "platform" / "tools")

    contents: dict[str, bytes] = {}
    records: list[dict[str, object]] = []
    for name in HOST_TOOL_FILES:
        path = _source_path(source_root, name)
        data = _read_source(path)
        mode = EXECUTABLE_MODE
        contents[name] = data
        records.append(
            {"path": name, "sha256": _sha256_bytes(data), "mode": mode}
        )

    capabilities = _capabilities_text(source_sha)
    contents["capabilities.txt"] = capabilities
    records.append(
        {
            "path": "capabilities.txt",
            "sha256": _sha256_bytes(capabilities),
            "mode": DATA_MODE,
        }
    )
    records.sort(key=lambda record: str(record["path"]))
    manifest = _manifest(source_sha, records)
    manifest_bytes = _canonical_json(manifest)

    output.parent.mkdir(parents=True, exist_ok=True)
    _regular_directory(output.parent)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary_created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        temporary_created = True
        with os.fdopen(descriptor, "w+b") as stream:
            with zipfile.ZipFile(
                stream,
                mode="w",
                compression=zipfile.ZIP_STORED,
                allowZip64=False,
            ) as archive:
                archive.writestr(_zip_info("capabilities.txt", DATA_MODE), capabilities)
                for name in HOST_TOOL_FILES:
                    archive.writestr(_zip_info(name, EXECUTABLE_MODE), contents[name])
                archive.writestr(_zip_info("manifest.json", DATA_MODE), manifest_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_metadata = _regular_file(temporary, MAX_BUNDLE_BYTES)
        if temporary_metadata.st_mode & 0o077 or temporary_metadata.st_size > MAX_BUNDLE_BYTES:
            raise HostToolsBundleError("host-tools bundle exceeds its bound")
        os.replace(temporary, output)
        output_directory_fd = os.open(
            output.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(output_directory_fd)
        finally:
            os.close(output_directory_fd)
    except (HostToolsBundleError, OSError, ValueError, zipfile.BadZipFile) as exc:
        if temporary_created:
            try:
                temporary.unlink()
            except OSError:
                pass
        if isinstance(exc, HostToolsBundleError):
            raise
        raise HostToolsBundleError("host-tools bundle could not be written") from exc
    return verify_bundle(output, expected_source_sha=source_sha)


def _safe_member(info: zipfile.ZipInfo) -> str:
    name = _bounded_zip_info(
        info,
        expected_name=None,
        maximum_member_bytes=MAX_FILE_BYTES,
        expected_prefix=MEMBER_ROOT,
    )
    return name.split("/", 1)[1]


def _validate_manifest(payload: object, expected_source_sha: str | None) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise HostToolsBundleError("host-tools manifest is not an object")
    if set(payload) != {
        "schema",
        "toolset_version",
        "source_sha",
        "generation",
        "capabilities",
        "components",
        "limits",
        "files",
    }:
        raise HostToolsBundleError("host-tools manifest schema is not closed")
    source_sha = payload.get("source_sha")
    if not isinstance(source_sha, str) or SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise HostToolsBundleError("host-tools source SHA is invalid")
    if expected_source_sha is not None and source_sha != expected_source_sha:
        raise HostToolsBundleError("host-tools source SHA does not match target")
    if (
        type(payload.get("schema")) is not int
        or payload.get("schema") != SCHEMA
        or type(payload.get("toolset_version")) is not str
        or payload.get("toolset_version") != TOOLSET_VERSION
    ):
        raise HostToolsBundleError("host-tools manifest version is invalid")
    if payload.get("generation") != source_sha:
        raise HostToolsBundleError("host-tools generation is not source-bound")
    capabilities = payload.get("capabilities")
    if capabilities != list(CAPABILITIES):
        raise HostToolsBundleError("host-tools capabilities are invalid")
    components = payload.get("components")
    if components != {
        component: list(names) for component, names in COMPONENT_FILES.items()
    }:
        raise HostToolsBundleError("host-tools component closure is invalid")
    limits = payload.get("limits")
    expected_limits = {
        "max_bundle_bytes": MAX_BUNDLE_BYTES,
        "max_file_bytes": MAX_FILE_BYTES,
        "max_file_count": MAX_FILE_COUNT,
    }
    if (
        not isinstance(limits, dict)
        or set(limits) != set(expected_limits)
        or any(
            type(limits[key]) is not int or limits[key] != expected_limits[key]
            for key in expected_limits
        )
    ):
        raise HostToolsBundleError("host-tools limits are invalid")
    records = payload.get("files")
    if not isinstance(records, list) or len(records) != len(HOST_TOOL_FILES) + 1:
        raise HostToolsBundleError("host-tools manifest file count is invalid")
    expected_paths = set(HOST_TOOL_FILES) | {"capabilities.txt"}
    actual_paths: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "mode"}:
            raise HostToolsBundleError("host-tools file record is invalid")
        path = record.get("path")
        digest = record.get("sha256")
        mode = record.get("mode")
        if (
            not isinstance(path, str)
            or path not in expected_paths
            or path in actual_paths
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
            or type(mode) is not int
            or mode != (DATA_MODE if path == "capabilities.txt" else EXECUTABLE_MODE)
        ):
            raise HostToolsBundleError("host-tools file record is invalid")
        actual_paths.add(path)
    if actual_paths != expected_paths or [
        record["path"] for record in records
    ] != sorted(actual_paths):
        raise HostToolsBundleError("host-tools manifest ordering is invalid")
    return payload


def _verify_bundle_bytes(
    bundle_bytes: bytes,
    *,
    expected_source_sha: str | None = None,
    expected_bundle_sha256: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_capabilities_sha256: str | None = None,
) -> dict[str, object]:
    """Verify one inner bundle from immutable bytes, without extraction."""

    if not isinstance(bundle_bytes, bytes) or len(bundle_bytes) > MAX_BUNDLE_BYTES:
        raise HostToolsBundleError("host-tools bundle exceeds its bound")
    if expected_bundle_sha256 is not None and _sha256_bytes(bundle_bytes) != _digest_argument(
        expected_bundle_sha256, "expected inner bundle digest"
    ):
        raise HostToolsBundleError("host-tools inner bundle digest does not match")
    try:
        with zipfile.ZipFile(BytesIO(bundle_bytes), mode="r", allowZip64=False) as archive:
            infos = archive.infolist()
            expected_count = len(HOST_TOOL_FILES) + 2
            if len(infos) != expected_count or len(infos) > MAX_FILE_COUNT + 1:
                raise HostToolsBundleError("host-tools archive member count is invalid")
            members: dict[str, bytes] = {}
            modes: dict[str, int] = {}
            total_uncompressed = 0
            for info in infos:
                name = _safe_member(info)
                if name in members:
                    raise HostToolsBundleError("host-tools archive contains duplicate members")
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_FILE_BYTES * MAX_FILE_COUNT:
                    raise HostToolsBundleError("host-tools archive exceeds its bound")
                data = _read_zip_member(archive, info, MAX_FILE_BYTES)
                members[name] = data
                modes[name] = _zip_member_mode(info)
    except HostToolsBundleError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise HostToolsBundleError("host-tools archive is invalid") from exc

    expected_members = set(HOST_TOOL_FILES) | {"capabilities.txt", "manifest.json"}
    if set(members) != expected_members:
        raise HostToolsBundleError("host-tools archive member allowlist is invalid")
    if modes.get("manifest.json") != DATA_MODE or modes.get("capabilities.txt") != DATA_MODE:
        raise HostToolsBundleError("host-tools data mode is invalid")
    if any(modes[name] != EXECUTABLE_MODE for name in HOST_TOOL_FILES):
        raise HostToolsBundleError("host-tools executable mode is invalid")

    try:
        manifest = json.loads(
            members["manifest.json"].decode("ascii"),
            object_pairs_hook=_strict_object,
        )
    except (UnicodeError, json.JSONDecodeError, HostToolsBundleError) as exc:
        raise HostToolsBundleError("host-tools manifest encoding is invalid") from exc
    _validate_manifest(manifest, expected_source_sha)
    manifest_sha256 = _sha256_bytes(members["manifest.json"])
    capabilities_sha256 = _sha256_bytes(members["capabilities.txt"])
    if expected_manifest_sha256 is not None and manifest_sha256 != _digest_argument(
        expected_manifest_sha256, "expected manifest digest"
    ):
        raise HostToolsBundleError("host-tools manifest digest does not match")
    if expected_capabilities_sha256 is not None and capabilities_sha256 != _digest_argument(
        expected_capabilities_sha256, "expected capabilities digest"
    ):
        raise HostToolsBundleError("host-tools capabilities digest does not match")
    records = {str(record["path"]): record for record in manifest["files"]}
    if members["capabilities.txt"] != _capabilities_text(str(manifest["source_sha"])):
        raise HostToolsBundleError("host-tools capabilities payload is invalid")
    for name, record in records.items():
        if _sha256_bytes(members[name]) != record["sha256"]:
            raise HostToolsBundleError("host-tools member digest is invalid")

    return {
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "capabilities_sha256": capabilities_sha256,
        "bundle_sha256": _sha256_bytes(bundle_bytes),
        "members": members,
    }


def verify_bundle(
    bundle: Path,
    expected_source_sha: str | None = None,
    *,
    expected_bundle_sha256: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_capabilities_sha256: str | None = None,
) -> dict[str, object]:
    """Verify an inner bundle through a bounded, identity-checked read."""

    bundle_bytes, _ = _read_regular_bytes(bundle, MAX_BUNDLE_BYTES, "host-tools bundle")
    return _verify_bundle_bytes(
        bundle_bytes,
        expected_source_sha=expected_source_sha,
        expected_bundle_sha256=expected_bundle_sha256,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_capabilities_sha256=expected_capabilities_sha256,
    )


def verify_single_member_archive(
    archive_path: Path,
    *,
    expected_member: str,
    maximum_archive_bytes: int,
    maximum_member_bytes: int,
    expected_member_digest: str | None = None,
) -> bytes:
    """Verify a bounded ZIP envelope with one exact regular member."""

    if (
        not isinstance(expected_member, str)
        or not expected_member
        or "/" in expected_member
        or "\\" in expected_member
        or any(part in {"", ".", ".."} for part in expected_member.split("/"))
    ):
        raise HostToolsBundleError("host-tools archive member name is invalid")
    archive_bytes, _ = _read_regular_bytes(
        archive_path, maximum_archive_bytes, "host-tools envelope"
    )
    try:
        with zipfile.ZipFile(BytesIO(archive_bytes), mode="r", allowZip64=False) as archive:
            infos = archive.infolist()
            if len(infos) != 1:
                raise HostToolsBundleError("host-tools envelope member count is invalid")
            info = infos[0]
            _bounded_zip_info(
                info,
                expected_name=expected_member,
                maximum_member_bytes=maximum_member_bytes,
            )
            member = _read_zip_member(archive, info, maximum_member_bytes)
    except HostToolsBundleError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise HostToolsBundleError("host-tools envelope is invalid") from exc
    if expected_member_digest is not None and _sha256_bytes(member) != _digest_argument(
        expected_member_digest, "expected envelope member digest"
    ):
        raise HostToolsBundleError("host-tools envelope member digest does not match")
    return member


def verify_outer_bundle(
    outer: Path,
    *,
    expected_outer_sha256: str | None = None,
    expected_inner_sha256: str | None = None,
    expected_source_sha: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_capabilities_sha256: str | None = None,
) -> dict[str, object]:
    """Verify and extract the one-member GitHub artifact envelope in memory.

    The returned ``inner_bytes`` are the exact member bytes that were covered
    by the outer digest and independently verified by :func:`_verify_bundle_bytes`.
    No archive member is ever extracted by ``zipfile``.
    """

    outer_bytes, _ = _read_regular_bytes(
        outer, MAX_ARTIFACT_ARCHIVE_BYTES, "host-tools outer artifact"
    )
    outer_sha256 = _sha256_bytes(outer_bytes)
    if expected_outer_sha256 is not None and outer_sha256 != _digest_argument(
        expected_outer_sha256, "expected outer artifact digest"
    ):
        raise HostToolsBundleError("host-tools outer artifact digest does not match")
    # Validate the envelope from the same bounded bytes read above.  Keeping
    # the raw read here lets the returned outer digest remain tied to exactly
    # the bytes that were parsed.
    try:
        with zipfile.ZipFile(BytesIO(outer_bytes), mode="r", allowZip64=False) as archive:
            infos = archive.infolist()
            if len(infos) != 1:
                raise HostToolsBundleError("host-tools outer archive member count is invalid")
            info = infos[0]
            _bounded_zip_info(
                info,
                expected_name=OUTER_MEMBER_NAME,
                maximum_member_bytes=MAX_OUTER_MEMBER_BYTES,
            )
            inner_bytes = _read_zip_member(archive, info, MAX_OUTER_MEMBER_BYTES)
    except HostToolsBundleError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise HostToolsBundleError("host-tools outer archive is invalid") from exc
    inner_summary = _verify_bundle_bytes(
        inner_bytes,
        expected_source_sha=expected_source_sha,
        expected_bundle_sha256=expected_inner_sha256,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_capabilities_sha256=expected_capabilities_sha256,
    )
    return {
        **inner_summary,
        "outer_sha256": outer_sha256,
        "inner_bytes": inner_bytes,
    }


def _open_no_symlink_directory(path: Path) -> tuple[int, os.stat_result]:
    """Open every directory component with ``openat`` and ``O_NOFOLLOW``."""

    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise HostToolsBundleError("host-tools directory path is invalid")
    no_follow = _require_no_follow()
    descriptor = os.open(
        "/",
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | no_follow,
    )
    try:
        for component in path.parts[1:]:
            try:
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | no_follow,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise HostToolsBundleError("host-tools directory is unavailable") from exc
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise HostToolsBundleError("host-tools directory is unsafe")
        return descriptor, metadata
    except HostToolsBundleError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise HostToolsBundleError("host-tools directory cannot be opened") from exc


def extract_outer_bundle(
    outer: Path,
    output: Path,
    *,
    expected_outer_sha256: str | None = None,
    expected_inner_sha256: str | None = None,
) -> dict[str, object]:
    """Write the verified inner ZIP exactly once using an identity-safe file."""

    if expected_outer_sha256 is None or expected_inner_sha256 is None:
        raise HostToolsBundleError(
            "outer extraction requires expected outer and inner digests"
        )
    summary = verify_outer_bundle(
        outer,
        expected_outer_sha256=expected_outer_sha256,
        expected_inner_sha256=expected_inner_sha256,
    )
    parent_fd, parent_before = _open_no_symlink_directory(output.parent)
    descriptor: int | None = None
    created = False
    written_identity: os.stat_result | None = None
    failure: BaseException | None = None
    try:
        descriptor = os.open(
            output.name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        created = True
        _write_all(descriptor, bytes(summary["inner_bytes"]))
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        written_identity = metadata
        if (
            metadata.st_dev != parent_before.st_dev
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != len(summary["inner_bytes"])
            or _sha256_bytes(os.pread(descriptor, metadata.st_size, 0)) != summary["bundle_sha256"]
        ):
            raise HostToolsBundleError("host-tools extracted member identity is unsafe")
    except HostToolsBundleError as exc:
        failure = exc
    except OSError as exc:
        failure = HostToolsBundleError("host-tools extracted member could not be written")
        failure.__cause__ = exc
    finally:
        if descriptor is not None and created and written_identity is None:
            try:
                written_identity = os.fstat(descriptor)
            except OSError:
                pass
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if created and written_identity is not None:
        try:
            after = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                after.st_dev != written_identity.st_dev
                or after.st_ino != written_identity.st_ino
                or not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
            ):
                failure = failure or HostToolsBundleError("host-tools extracted member identity changed")
        except OSError as exc:
            failure = failure or HostToolsBundleError("host-tools extracted member disappeared")
            failure.__cause__ = exc
    if failure is not None:
        # Do not unlink a path whose identity is no longer ours.  This is the
        # only cleanup performed by the extractor after a partial write.
        if created and written_identity is not None:
            try:
                current = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
                if current.st_dev == written_identity.st_dev and current.st_ino == written_identity.st_ino:
                    os.unlink(output.name, dir_fd=parent_fd)
            except OSError:
                pass
        try:
            os.close(parent_fd)
        except OSError:
            pass
        raise failure
    try:
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise HostToolsBundleError("host-tools output parent could not be synced") from exc
    return {key: value for key, value in summary.items() if key != "inner_bytes"}


def _safe_leaf(value: str, description: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", value) is None:
        raise HostToolsBundleError(f"{description} is invalid")
    return value


def _safe_generation_sha(value: str, description: str = "host-tools source SHA") -> str:
    if not isinstance(value, str) or SOURCE_SHA_RE.fullmatch(value) is None:
        raise HostToolsBundleError(f"{description} is invalid")
    return value


def _validate_provenance(
    evidence_path: Path,
    *,
    host_tools_sha: str,
    source_head_sha: str,
    packaging_commit: str,
    outer_sha256: str,
    inner_sha256: str,
    artifact_id: str,
) -> dict[str, object]:
    """Require the external attestation gate and preserve C/E/packaging IDs.

    This helper does not perform cryptographic attestation.  It accepts a
    bounded receipt emitted by an independently pinned verifier and checks
    that the receipt's tuple is exactly the bundle being installed.  The
    distinction is deliberate: an installer must never claim a verifier it
    does not ship.
    """

    host = _safe_generation_sha(host_tools_sha)
    source = _safe_generation_sha(source_head_sha, "source head SHA")
    packaging = _safe_generation_sha(packaging_commit, "packaging commit")
    if not isinstance(artifact_id, str) or re.fullmatch(r"[1-9][0-9]{0,31}", artifact_id) is None:
        raise HostToolsBundleError("artifact ID is invalid")
    if len({host, source, packaging}) != 3:
        raise HostToolsBundleError("host-tools provenance tuple is not distinct")
    payload, _ = _read_regular_bytes(
        evidence_path, ATTESTATION_MAX_BYTES, "external attestation evidence"
    )
    try:
        parsed = json.loads(payload.decode("ascii"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, HostToolsBundleError) as exc:
        raise HostToolsBundleError("external attestation evidence is invalid") from exc
    if not isinstance(parsed, dict):
        raise HostToolsBundleError("external attestation evidence is invalid")
    expected_keys = {
        "schema",
        "status",
        "verifier",
        "host_tools_sha",
        "source_head_sha",
        "packaging_commit",
        "artifact_id",
        "outer_sha256",
        "inner_sha256",
    }
    if set(parsed) != expected_keys:
        raise HostToolsBundleError("external attestation evidence schema is not closed")
    verifier = parsed.get("verifier")
    if (
        parsed.get("schema") != 1
        or parsed.get("status") != "satisfied"
        or not isinstance(verifier, str)
        or re.fullmatch(r"[A-Za-z0-9_.:/@+-]{1,200}", verifier) is None
        or parsed.get("host_tools_sha") != host
        or parsed.get("source_head_sha") != source
        or parsed.get("packaging_commit") != packaging
        or parsed.get("artifact_id") != artifact_id
        or _digest_argument(str(parsed.get("outer_sha256")), "attestation outer digest")
        != outer_sha256
        or _digest_argument(str(parsed.get("inner_sha256")), "attestation inner digest")
        != inner_sha256
    ):
        raise HostToolsBundleError("external attestation evidence does not bind the handoff")
    return {
        "schema": 1,
        "status": "satisfied",
        "verifier": verifier,
        "host_tools_sha": host,
        "source_head_sha": source,
        "packaging_commit": packaging,
        "artifact_id": artifact_id,
    }


def _open_host_tools_root(path: Path) -> tuple[int, os.stat_result]:
    """Open an existing root-owned, no-symlink host-tools parent directory."""

    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or not path.parts
        or path.parts[0] != "/"
        or ".." in path.parts
    ):
        raise HostToolsBundleError("host-tools root path is invalid")
    if path == Path("/"):
        raise HostToolsBundleError("host-tools root path is invalid")
    no_follow = _require_no_follow()
    descriptor = os.open(
        "/",
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | no_follow,
    )
    try:
        components = path.parts[1:]
        for index, component in enumerate(components):
            try:
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | no_follow,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise HostToolsBundleError("host-tools root parent is unavailable") from exc
            previous_descriptor = descriptor
            descriptor = next_descriptor
            os.close(previous_descriptor)
            try:
                component_metadata = os.fstat(descriptor)
            except OSError as exc:
                raise HostToolsBundleError("host-tools root metadata is unavailable") from exc
            final = index == len(components) - 1
            mode = stat.S_IMODE(component_metadata.st_mode)
            if (
                not stat.S_ISDIR(component_metadata.st_mode)
                or component_metadata.st_uid != 0
                or component_metadata.st_gid != 0
                or (
                    mode & 0o022
                    and (
                        final
                        or not (
                            mode & stat.S_ISVTX
                            and mode & 0o002
                        )
                    )
                )
            ):
                raise HostToolsBundleError("host-tools root parent metadata is unsafe")
        metadata = os.fstat(descriptor)
    except HostToolsBundleError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise HostToolsBundleError("host-tools root cannot be opened") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink < 2
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        os.close(descriptor)
        raise HostToolsBundleError("host-tools root metadata is unsafe")
    return descriptor, metadata


def _lstat_at(parent_fd: int, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise HostToolsBundleError("host-tools path metadata is unavailable") from exc


def _rename_noreplace(
    parent_fd: int,
    source: str,
    target: str,
    expected_source_identity: os.stat_result | None = None,
) -> None:
    """Publish within one directory using Linux ``RENAME_NOREPLACE`` only."""

    _safe_leaf(source, "host-tools stage name")
    _safe_leaf(target, "host-tools generation name")
    if expected_source_identity is not None:
        current_source = _lstat_at(parent_fd, source)
        if (
            current_source.st_dev != expected_source_identity.st_dev
            or current_source.st_ino != expected_source_identity.st_ino
            or not stat.S_ISDIR(current_source.st_mode)
            or stat.S_ISLNK(current_source.st_mode)
            or current_source.st_nlink != expected_source_identity.st_nlink
        ):
            raise HostToolsBundleError("host-tools private stage identity changed")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
    except (AttributeError, OSError) as exc:
        raise HostToolsBundleError("host-tools no-overwrite publish primitive is unavailable") from exc
    result = renameat2(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(target),
        RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise HostToolsBundleError("host-tools generation already exists")
        if error in {errno.EXDEV, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP}:
            raise HostToolsBundleError("host-tools no-overwrite publish cannot be proven")
        raise HostToolsBundleError("host-tools generation could not be published")


def _new_stage(host_fd: int, source_sha: str) -> tuple[str, os.stat_result]:
    for _ in range(16):
        name = f".host-tools-stage-{source_sha}-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, STAGE_MODE, dir_fd=host_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise HostToolsBundleError("host-tools private stage could not be created") from exc
        metadata = _lstat_at(host_fd, name)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 2
            or stat.S_IMODE(metadata.st_mode) != STAGE_MODE
            or metadata.st_dev != os.fstat(host_fd).st_dev
        ):
            # The directory was just created by this process, but retain the
            # same inode/empty-directory proof used for all later cleanup.
            _cleanup_owned_directory(host_fd, name, metadata, {})
            raise HostToolsBundleError("host-tools private stage metadata is unsafe")
        return name, metadata
    raise HostToolsBundleError("host-tools private stage name collision")


def _write_stage_member(stage_fd: int, name: str, data: bytes, mode: int) -> os.stat_result:
    _safe_leaf(name, "host-tools member name")
    if (
        name not in HOST_TOOLS_INVENTORY
        or mode not in {EXECUTABLE_MODE, DATA_MODE}
        or not isinstance(data, bytes)
        or len(data) > MAX_FILE_BYTES
    ):
        raise HostToolsBundleError("host-tools stage member is not allowlisted")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=stage_fd,
        )
        _write_all(descriptor, data)
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size != len(data)
        ):
            raise HostToolsBundleError("host-tools staged member metadata is unsafe")
        if _sha256_bytes(os.pread(descriptor, metadata.st_size, 0)) != _sha256_bytes(data):
            raise HostToolsBundleError("host-tools staged member digest is invalid")
        return metadata
    except HostToolsBundleError:
        raise
    except OSError as exc:
        raise HostToolsBundleError("host-tools staged member could not be written") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _verify_generation_dir(
    generation: Path,
    *,
    expected_source_sha: str,
    expected_manifest_sha256: str,
    expected_capabilities_sha256: str,
    expected_bundle_sha256: str | None = None,
) -> dict[str, object]:
    """Verify exact installed metadata and all 15 flat members."""

    host = _safe_generation_sha(expected_source_sha)
    if generation.name != host or not generation.is_absolute():
        raise HostToolsBundleError("host-tools generation path is invalid")
    try:
        root_metadata = generation.lstat()
    except OSError as exc:
        raise HostToolsBundleError("host-tools generation is unavailable") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or root_metadata.st_uid != 0
        or root_metadata.st_gid != 0
        or root_metadata.st_nlink != 2
        or stat.S_IMODE(root_metadata.st_mode) != HOST_GENERATION_MODE
    ):
        raise HostToolsBundleError("host-tools generation metadata is unsafe")
    entries = list(generation.iterdir())
    if {entry.name for entry in entries} != HOST_TOOLS_INVENTORY:
        raise HostToolsBundleError("host-tools generation inventory is not closed")
    members: dict[str, bytes] = {}
    for entry in entries:
        metadata = entry.lstat()
        mode = DATA_MODE if entry.name in {"manifest.json", "capabilities.txt"} else EXECUTABLE_MODE
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size > MAX_FILE_BYTES
        ):
            raise HostToolsBundleError("host-tools generation member metadata is unsafe")
        data, opened = _read_regular_bytes(entry, MAX_FILE_BYTES, "host-tools generation member")
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_uid != metadata.st_uid
            or opened.st_gid != metadata.st_gid
            or opened.st_nlink != metadata.st_nlink
            or stat.S_IMODE(opened.st_mode) != mode
        ):
            raise HostToolsBundleError("host-tools generation member changed")
        members[entry.name] = data
    manifest_sha = _sha256_bytes(members["manifest.json"])
    capabilities_sha = _sha256_bytes(members["capabilities.txt"])
    if manifest_sha != _digest_argument(expected_manifest_sha256, "expected manifest digest"):
        raise HostToolsBundleError("host-tools installed manifest digest does not match")
    if capabilities_sha != _digest_argument(expected_capabilities_sha256, "expected capabilities digest"):
        raise HostToolsBundleError("host-tools installed capabilities digest does not match")
    # Reuse the same manifest/capability checks as the archive verifier.  The
    # bytes are synthesized into the verified inventory in memory; no second
    # parser or alternate allowlist is introduced.
    try:
        manifest = json.loads(
            members["manifest.json"].decode("ascii"), object_pairs_hook=_strict_object
        )
    except (UnicodeError, json.JSONDecodeError, HostToolsBundleError) as exc:
        raise HostToolsBundleError("host-tools installed manifest is invalid") from exc
    _validate_manifest(manifest, host)
    if members["capabilities.txt"] != _capabilities_text(host):
        raise HostToolsBundleError("host-tools installed capabilities are invalid")
    records = {str(record["path"]): record for record in manifest["files"]}
    for name, record in records.items():
        if name not in members or _sha256_bytes(members[name]) != record["sha256"]:
            raise HostToolsBundleError("host-tools installed member digest is invalid")
    return {
        "source_sha": host,
        "manifest_sha256": manifest_sha,
        "capabilities_sha256": capabilities_sha,
        "bundle_sha256": expected_bundle_sha256,
        "inventory": sorted(HOST_TOOLS_INVENTORY),
    }


def _cleanup_owned_directory(
    parent_fd: int,
    name: str,
    identity: os.stat_result,
    child_identities: dict[str, os.stat_result],
) -> bool:
    """Remove only the exact stage inode and its exact created children."""

    try:
        current = _lstat_at(parent_fd, name)
        if (
            current.st_dev != identity.st_dev
            or current.st_ino != identity.st_ino
            or not stat.S_ISDIR(current.st_mode)
            or current.st_nlink != identity.st_nlink
        ):
            return False
        stage_fd = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            if {entry for entry in os.listdir(stage_fd)} != set(child_identities):
                return False
            for child, child_identity in child_identities.items():
                current_child = _lstat_at(stage_fd, child)
                if (
                    current_child.st_dev != child_identity.st_dev
                    or current_child.st_ino != child_identity.st_ino
                    or current_child.st_nlink != child_identity.st_nlink
                    or not stat.S_ISREG(current_child.st_mode)
                ):
                    return False
            for child in child_identities:
                os.unlink(child, dir_fd=stage_fd)
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        # Re-check before rmdir.  If the pathname was replaced, leave it for
        # an operator; never recursively delete an unowned object.
        current = _lstat_at(parent_fd, name)
        if current.st_dev != identity.st_dev or current.st_ino != identity.st_ino:
            return False
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    except (HostToolsBundleError, OSError):
        return False


def _run_post_install_self_tests(
    generation: Path,
    *,
    source_sha: str,
    manifest_sha256: str,
    capabilities_sha256: str,
) -> dict[str, str]:
    dispatcher = generation / "platform_workflow_remote_dispatch.py"
    expected_capabilities = (
        "HOST_TOOLS schema=1 "
        f"source_sha={source_sha} generation={source_sha} "
        "dispatcher=2 artifact_prepare=2 supervisor=2 input_guard=1 "
        "python_isolated=1 python_bytecode_disabled=1\n"
    )
    expected_contract = (
        "HOST_TOOLS_CONTRACT "
        f"source_sha={source_sha} generation={source_sha} "
        f"manifest_sha256={manifest_sha256} capabilities_sha256={capabilities_sha256}\n"
    )
    commands = {
        "host-capabilities": [
            "/usr/bin/python3.12",
            "-I",
            "-B",
            str(dispatcher),
            "host-capabilities",
        ],
        "host-contract": [
            "/usr/bin/python3.12",
            "-I",
            "-B",
            str(dispatcher),
            "host-contract",
            source_sha,
            manifest_sha256,
            capabilities_sha256,
        ],
    }
    outputs: dict[str, str] = {}
    for name, command in commands.items():
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"},
                timeout=15,
                check=False,
                text=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HostToolsBundleError(f"host-tools {name} self-test could not run") from exc
        stdout = completed.stdout if isinstance(completed.stdout, bytes) else b""
        stderr = completed.stderr if isinstance(completed.stderr, bytes) else b""
        if len(stdout) > 4096 or len(stderr) > 4096 or completed.returncode != 0:
            raise HostToolsBundleError(f"host-tools {name} self-test failed")
        expected = expected_capabilities if name == "host-capabilities" else expected_contract
        if stdout.decode("ascii", "strict") != expected or stderr:
            raise HostToolsBundleError(f"host-tools {name} self-test contract is invalid")
        outputs[name] = stdout.decode("ascii")
    return outputs


def _remove_owned_file(
    parent_fd: int,
    name: str,
    identity: os.stat_result | None,
) -> None:
    """Unlink one created file only while its inode identity is unchanged."""

    if identity is None:
        return
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            current.st_dev != identity.st_dev
            or current.st_ino != identity.st_ino
            or current.st_nlink != identity.st_nlink
        ):
            return
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError:
        pass


def _write_evidence(path: Path, payload: dict[str, object]) -> None:
    encoded = _canonical_json(payload)
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise HostToolsBundleError("host-tools install evidence exceeds its bound")
    parent_fd, _ = _open_no_symlink_directory(path.parent)
    descriptor: int | None = None
    created = False
    written_identity: os.stat_result | None = None
    failure: BaseException | None = None
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            EVIDENCE_MODE,
            dir_fd=parent_fd,
        )
        created = True
        _write_all(descriptor, encoded)
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, EVIDENCE_MODE)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        written_identity = metadata
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != EVIDENCE_MODE
            or metadata.st_size != len(encoded)
        ):
            raise HostToolsBundleError("host-tools install evidence metadata is unsafe")
    except HostToolsBundleError as exc:
        failure = exc
    except OSError as exc:
        failure = HostToolsBundleError("host-tools install evidence could not be written")
        failure.__cause__ = exc
    except BaseException as exc:
        failure = exc
    finally:
        if descriptor is not None and created and written_identity is None:
            try:
                written_identity = os.fstat(descriptor)
            except OSError:
                pass
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if failure is not None:
        # An interrupted evidence write is removed only when the path still
        # names the inode opened above.  An identity mismatch is quarantined.
        if created:
            _remove_owned_file(parent_fd, path.name, written_identity)
        try:
            os.close(parent_fd)
        except OSError:
            pass
        raise failure
    try:
        os.fsync(parent_fd)
    except OSError as exc:
        _remove_owned_file(parent_fd, path.name, written_identity)
        raise HostToolsBundleError("host-tools install evidence parent could not be synced") from exc
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def install_bundle(
    outer: Path,
    *,
    host_tools_root: Path,
    expected_source_sha: str,
    expected_outer_sha256: str,
    expected_inner_sha256: str,
    expected_manifest_sha256: str,
    expected_capabilities_sha256: str,
    artifact_id: str,
    attestation_evidence: Path,
    source_head_sha: str,
    packaging_commit: str,
    evidence_output: Path | None = None,
) -> dict[str, object]:
    """Install one verified generation from an outer artifact envelope.

    This is an operator/host-image helper.  It never mutates release
    pointers, databases, systemd units, ``current`` or ``previous``.
    """

    if os.geteuid() != 0 or os.getuid() != 0:
        raise HostToolsBundleError("host-tools installer requires root")
    source_sha = _safe_generation_sha(expected_source_sha)
    outer_summary = verify_outer_bundle(
        outer,
        expected_outer_sha256=expected_outer_sha256,
        expected_inner_sha256=expected_inner_sha256,
        expected_source_sha=source_sha,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_capabilities_sha256=expected_capabilities_sha256,
    )
    outer_sha = str(outer_summary["outer_sha256"])
    inner_sha = str(outer_summary["bundle_sha256"])
    manifest_sha = str(outer_summary["manifest_sha256"])
    capabilities_sha = str(outer_summary["capabilities_sha256"])
    attestation = _validate_provenance(
        attestation_evidence,
        host_tools_sha=source_sha,
        source_head_sha=source_head_sha,
        packaging_commit=packaging_commit,
        outer_sha256=outer_sha,
        inner_sha256=inner_sha,
        artifact_id=artifact_id,
    )
    host_fd, host_metadata = _open_host_tools_root(host_tools_root)
    stage_name: str | None = None
    stage_identity: os.stat_result | None = None
    child_identities: dict[str, os.stat_result] = {}
    published = False
    target_identity: os.stat_result | None = None
    installation_complete = False
    try:
        target_name = source_sha
        try:
            existing_target = os.stat(target_name, dir_fd=host_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing_target = None
        except OSError as exc:
            raise HostToolsBundleError("host-tools generation target is unavailable") from exc
        if existing_target is not None:
            raise HostToolsBundleError("host-tools generation already exists")
        stage_name, stage_identity = _new_stage(host_fd, source_sha)
        stage_fd = os.open(
            stage_name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=host_fd,
        )
        try:
            members = outer_summary["members"]
            if not isinstance(members, dict):
                raise HostToolsBundleError("host-tools bundle inventory is invalid")
            manifest = outer_summary["manifest"]
            if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
                raise HostToolsBundleError("host-tools bundle manifest is invalid")
            for name in HOST_TOOL_FILES:
                child_identities[name] = _write_stage_member(
                    stage_fd, name, bytes(members[name]), EXECUTABLE_MODE
                )
            child_identities["capabilities.txt"] = _write_stage_member(
                stage_fd, "capabilities.txt", bytes(members["capabilities.txt"]), DATA_MODE
            )
            child_identities["manifest.json"] = _write_stage_member(
                stage_fd, "manifest.json", bytes(members["manifest.json"]), DATA_MODE
            )
            if set(os.listdir(stage_fd)) != HOST_TOOLS_INVENTORY:
                raise HostToolsBundleError("host-tools private stage inventory is not closed")
            os.fchown(stage_fd, 0, 0)
            os.fchmod(stage_fd, HOST_GENERATION_MODE)
            os.fsync(stage_fd)
            current_stage = _lstat_at(host_fd, stage_name)
            if (
                current_stage.st_dev != stage_identity.st_dev
                or current_stage.st_ino != stage_identity.st_ino
                or current_stage.st_uid != 0
                or current_stage.st_gid != 0
                or stat.S_IMODE(current_stage.st_mode) != HOST_GENERATION_MODE
                or current_stage.st_nlink != 2
                or current_stage.st_dev != host_metadata.st_dev
            ):
                raise HostToolsBundleError("host-tools private stage changed")
        finally:
            os.close(stage_fd)
        os.fsync(host_fd)
        _rename_noreplace(host_fd, stage_name, target_name, stage_identity)
        published = True
        target_identity = _lstat_at(host_fd, target_name)
        os.fsync(host_fd)
        installed = _verify_generation_dir(
            host_tools_root / target_name,
            expected_source_sha=source_sha,
            expected_manifest_sha256=manifest_sha,
            expected_capabilities_sha256=capabilities_sha,
            expected_bundle_sha256=inner_sha,
        )
        current_target = _lstat_at(host_fd, target_name)
        if (
            target_identity is None
            or current_target.st_dev != target_identity.st_dev
            or current_target.st_ino != target_identity.st_ino
            or not stat.S_ISDIR(current_target.st_mode)
        ):
            raise HostToolsBundleError("host-tools generation identity changed")
        self_tests = _run_post_install_self_tests(
            host_tools_root / target_name,
            source_sha=source_sha,
            manifest_sha256=manifest_sha,
            capabilities_sha256=capabilities_sha,
        )
        # Re-run the exact inventory/digest check after both child processes.
        # This is also the no-`__pycache__`/no-extra proof for the installed
        # generation rather than only a pre-self-test assertion.
        current_target = _lstat_at(host_fd, target_name)
        if (
            target_identity is None
            or current_target.st_dev != target_identity.st_dev
            or current_target.st_ino != target_identity.st_ino
        ):
            raise HostToolsBundleError("host-tools generation identity changed")
        _verify_generation_dir(
            host_tools_root / target_name,
            expected_source_sha=source_sha,
            expected_manifest_sha256=manifest_sha,
            expected_capabilities_sha256=capabilities_sha,
            expected_bundle_sha256=inner_sha,
        )
        evidence = {
            "schema": 1,
            "kind": "host_tools_install",
            "status": "installed",
            "host_tools_sha": source_sha,
            "outer_bundle_sha256": outer_sha,
            "inner_bundle_sha256": inner_sha,
            "manifest_sha256": manifest_sha,
            "capabilities_sha256": capabilities_sha,
            "artifact_id": artifact_id,
            "generation": str(host_tools_root / target_name),
            "inventory": installed["inventory"],
            "provenance": attestation,
            "self_tests": {
                name: "passed" if value != "skipped" else "skipped"
                for name, value in self_tests.items()
            },
            "pointers_unchanged": True,
            "release_side_effects": False,
        }
        if evidence_output is not None:
            _write_evidence(evidence_output, evidence)
        installation_complete = True
        print(
            "HOST_TOOLS_INSTALL schema=1 status=installed "
            f"artifact_id={artifact_id} source_sha={source_sha} outer_sha256={outer_sha} inner_sha256={inner_sha} "
            f"manifest_sha256={manifest_sha} capabilities_sha256={capabilities_sha}"
        )
        return evidence
    except BaseException:
        if (
            published
            and target_identity is not None
            and not installation_complete
        ):
            _cleanup_owned_directory(host_fd, source_sha, target_identity, child_identities)
        elif stage_name is not None and stage_identity is not None and not published:
            _cleanup_owned_directory(host_fd, stage_name, stage_identity, child_identities)
        raise
    finally:
        os.close(host_fd)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HostToolsBundleError("host-tools manifest has duplicate keys")
        result[key] = value
    return result


def _read_bounded_json(path: Path, *, description: str) -> object:
    """Read one untrusted JSON response through a bounded, no-follow fd."""

    raw, _ = _read_regular_bytes(path, MAX_FILE_BYTES, description)
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
        )
    except HostToolsBundleError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise HostToolsBundleError(f"{description} is invalid") from exc


def write_contract_files(summary: dict[str, object], output_dir: Path) -> None:
    """Write bounded scalar/checksum files for shell-only remote preflight."""

    manifest = summary["manifest"]
    if not isinstance(manifest, dict):
        raise HostToolsBundleError("host-tools manifest summary is invalid")
    output_dir.mkdir(parents=True, exist_ok=True)
    records = manifest["files"]
    if not isinstance(records, list):
        raise HostToolsBundleError("host-tools file summary is invalid")
    output_dir_metadata = _regular_directory(output_dir)
    if output_dir_metadata.st_mode & 0o077:
        os.chmod(output_dir, 0o700)
    values = {
        "manifest.sha256": f"{summary['manifest_sha256']}\n",
        "capabilities.sha256": f"{summary['capabilities_sha256']}\n",
        "files.sha256": "".join(
            f"{record['sha256']}  {record['path']}\n" for record in records
        ),
        "files.modes": "".join(
            # This sidecar is consumed by the shell preflight together with
            # `stat -c %a`, whose output is the conventional octal text form
            # (444/555).  Keep the JSON manifest's numeric Unix-mode values
            # unchanged; only this line-oriented text contract is formatted
            # for its shell consumer.
            f"{record['mode']:o}  {record['path']}\n" for record in records
        ),
        "source_sha": f"{manifest['source_sha']}\n",
        "toolset_version": f"{manifest['toolset_version']}\n",
    }
    for name, value in values.items():
        destination = output_dir / name
        temporary = output_dir / f".{name}.tmp"
        temporary_created = False
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
            temporary_created = True
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except (OSError, ValueError) as exc:
            if temporary_created:
                try:
                    temporary.unlink()
                except OSError:
                    pass
            raise HostToolsBundleError("host-tools contract could not be written") from exc
    directory_fd = os.open(
        output_dir,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def verify_artifact_metadata(
    metadata_path: Path,
    *,
    artifact_id: str,
    artifact_name: str,
    run_id: str,
    run_attempt: str,
    source_sha: str,
    expected_branch: str,
    artifact_digest: str,
    archive_path: Path,
) -> None:
    """Validate the exact GitHub artifact envelope without executing data."""

    if not all(
        isinstance(value, str) and value
        for value in (
            artifact_id,
            artifact_name,
            run_id,
            run_attempt,
            source_sha,
            expected_branch,
            artifact_digest,
        )
    ):
        raise HostToolsBundleError("host-tools artifact metadata arguments are invalid")
    if not isinstance(archive_path, Path):
        raise HostToolsBundleError("host-tools artifact archive argument is invalid")
    if not re.fullmatch(r"[1-9][0-9]{0,31}", artifact_id):
        raise HostToolsBundleError("host-tools artifact id is invalid")
    if not re.fullmatch(r"[1-9][0-9]{0,31}", run_id) or not re.fullmatch(
        r"[1-9][0-9]{0,31}", run_attempt
    ):
        raise HostToolsBundleError("host-tools workflow identity is invalid")
    if SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise HostToolsBundleError("host-tools source SHA is invalid")
    if expected_branch != "dev" or re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest) is None:
        raise HostToolsBundleError("host-tools artifact binding arguments are invalid")
    payload = _read_bounded_json(metadata_path, description="host-tools artifact metadata")
    if not isinstance(payload, dict):
        raise HostToolsBundleError("host-tools artifact metadata is invalid")
    if (
        type(payload.get("id")) is not int
        or payload.get("id") != int(artifact_id)
        or type(payload.get("name")) is not str
        or payload.get("name") != artifact_name
        or type(payload.get("expired")) is not bool
        or payload.get("expired") is not False
        or type(payload.get("digest")) is not str
        or payload.get("digest") != artifact_digest
        or type(payload.get("size_in_bytes")) is not int
        or payload.get("size_in_bytes") <= 0
        or payload.get("size_in_bytes") > MAX_ARTIFACT_ARCHIVE_BYTES
    ):
        raise HostToolsBundleError("host-tools artifact identity is invalid")
    try:
        archive_stat = archive_path.lstat()
    except OSError as exc:
        raise HostToolsBundleError("host-tools artifact archive is unavailable") from exc
    if (
        not stat.S_ISREG(archive_stat.st_mode)
        or stat.S_ISLNK(archive_stat.st_mode)
        or archive_stat.st_nlink != 1
        or archive_stat.st_size != payload["size_in_bytes"]
    ):
        raise HostToolsBundleError("host-tools artifact archive size is invalid")
    workflow_run = payload.get("workflow_run")
    if not isinstance(workflow_run, dict):
        raise HostToolsBundleError("host-tools artifact workflow binding is invalid")
    if (
        type(workflow_run.get("id")) is not int
        or workflow_run.get("id") != int(run_id)
        or type(workflow_run.get("head_sha")) is not str
        or workflow_run.get("head_sha") != source_sha
        or type(workflow_run.get("head_branch")) is not str
        or workflow_run.get("head_branch") != expected_branch
    ):
        raise HostToolsBundleError("host-tools artifact workflow binding is invalid")
    # The artifact API's nested workflow_run object omits run_attempt.  When
    # GitHub supplies it, keep the field fail-closed rather than trusting a
    # coerced bool/string value; the authoritative attempt is validated from
    # the dedicated workflow-run-attempt endpoint below.
    if "run_attempt" in workflow_run and (
        type(workflow_run.get("run_attempt")) is not int
        or workflow_run.get("run_attempt") != int(run_attempt)
    ):
        raise HostToolsBundleError("host-tools artifact workflow binding is invalid")


def verify_workflow_attempt(
    metadata_path: Path,
    *,
    run_id: str,
    run_attempt: str,
    source_sha: str,
    repository: str,
    expected_branch: str,
    expected_event: str,
) -> None:
    """Validate the authoritative GitHub workflow-run-attempt response."""

    values = (run_id, run_attempt, source_sha, repository, expected_branch, expected_event)
    if not all(isinstance(value, str) and value for value in values):
        raise HostToolsBundleError("host-tools workflow attempt arguments are invalid")
    if not re.fullmatch(r"[1-9][0-9]{0,31}", run_id) or not re.fullmatch(
        r"[1-9][0-9]{0,31}", run_attempt
    ):
        raise HostToolsBundleError("host-tools workflow identity is invalid")
    if SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise HostToolsBundleError("host-tools source SHA is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repository):
        raise HostToolsBundleError("host-tools repository identity is invalid")
    if expected_branch != "dev" or expected_event not in {
        "push",
        "workflow_dispatch",
    }:
        raise HostToolsBundleError("host-tools workflow route is invalid")
    payload = _read_bounded_json(
        metadata_path,
        description="host-tools workflow attempt metadata",
    )
    if not isinstance(payload, dict):
        raise HostToolsBundleError("host-tools workflow attempt metadata is invalid")
    repository_payload = payload.get("repository")
    if not isinstance(repository_payload, dict):
        raise HostToolsBundleError("host-tools workflow attempt provenance is invalid")
    owner = repository.split("/", 1)[0]
    name = repository.split("/", 1)[1]
    owner_payload = repository_payload.get("owner")
    if (
        type(payload.get("id")) is not int
        or payload.get("id") != int(run_id)
        or type(payload.get("run_attempt")) is not int
        or payload.get("run_attempt") != int(run_attempt)
        or payload.get("head_sha") != source_sha
        or type(payload.get("head_sha")) is not str
        or payload.get("head_branch") != expected_branch
        or type(payload.get("head_branch")) is not str
        or payload.get("event") != expected_event
        or type(payload.get("event")) is not str
        or repository_payload.get("full_name") != repository
        or repository_payload.get("name") != name
        or not isinstance(owner_payload, dict)
        or owner_payload.get("login") != owner
    ):
        raise HostToolsBundleError("host-tools workflow attempt provenance is invalid")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--source-root", required=True)
    build.add_argument("--source-sha", required=True)
    build.add_argument("--output", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle", required=True)
    verify.add_argument("--expected-source-sha", required=True)
    verify.add_argument("--contract-dir", required=True)
    outer = subparsers.add_parser("verify-outer")
    outer.add_argument("--outer-bundle", required=True)
    outer.add_argument("--expected-outer-sha256")
    outer.add_argument("--expected-inner-sha256")
    outer.add_argument("--expected-source-sha")
    outer.add_argument("--expected-manifest-sha256")
    outer.add_argument("--expected-capabilities-sha256")
    extract = subparsers.add_parser("extract-outer")
    extract.add_argument("--outer-bundle", required=True)
    extract.add_argument("--output", required=True)
    extract.add_argument("--expected-outer-sha256", required=True)
    extract.add_argument("--expected-inner-sha256", required=True)
    install = subparsers.add_parser("install")
    install.add_argument("--outer-bundle", required=True)
    install.add_argument("--host-tools-root", required=True)
    install.add_argument("--expected-source-sha", required=True)
    install.add_argument("--expected-outer-sha256", required=True)
    install.add_argument("--expected-inner-sha256", required=True)
    install.add_argument("--expected-manifest-sha256", required=True)
    install.add_argument("--expected-capabilities-sha256", required=True)
    install.add_argument("--artifact-id", required=True)
    install.add_argument("--attestation-evidence", required=True)
    install.add_argument("--source-head-sha", required=True)
    install.add_argument("--packaging-commit", required=True)
    install.add_argument("--evidence-output", required=True)
    metadata = subparsers.add_parser("verify-artifact-metadata")
    metadata.add_argument("--metadata", required=True)
    metadata.add_argument("--artifact-id", required=True)
    metadata.add_argument("--artifact-name", required=True)
    metadata.add_argument("--run-id", required=True)
    metadata.add_argument("--run-attempt", required=True)
    metadata.add_argument("--source-sha", required=True)
    metadata.add_argument("--expected-branch", required=True)
    metadata.add_argument("--artifact-digest", required=True)
    metadata.add_argument("--archive", required=True)
    attempt = subparsers.add_parser("verify-workflow-attempt")
    attempt.add_argument("--metadata", required=True)
    attempt.add_argument("--run-id", required=True)
    attempt.add_argument("--run-attempt", required=True)
    attempt.add_argument("--source-sha", required=True)
    attempt.add_argument("--repository", required=True)
    attempt.add_argument("--expected-branch", required=True)
    attempt.add_argument("--expected-event", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "build":
            summary = build_bundle(
                Path(arguments.source_root), arguments.source_sha, Path(arguments.output)
            )
            print(
                "HOST_TOOLS_BUNDLE schema=1 "
                f"source_sha={summary['manifest']['source_sha']} "
                f"bundle_sha256={summary['bundle_sha256']}"
            )
            return 0
        if arguments.command == "verify":
            summary = verify_bundle(
                Path(arguments.bundle), expected_source_sha=arguments.expected_source_sha
            )
            write_contract_files(summary, Path(arguments.contract_dir))
            print(
                "HOST_TOOLS_BUNDLE schema=1 status=verified "
                f"source_sha={summary['manifest']['source_sha']} "
                f"manifest_sha256={summary['manifest_sha256']}"
            )
            return 0
        if arguments.command == "verify-outer":
            summary = verify_outer_bundle(
                Path(arguments.outer_bundle),
                expected_outer_sha256=arguments.expected_outer_sha256,
                expected_inner_sha256=arguments.expected_inner_sha256,
                expected_source_sha=arguments.expected_source_sha,
                expected_manifest_sha256=arguments.expected_manifest_sha256,
                expected_capabilities_sha256=arguments.expected_capabilities_sha256,
            )
            print(
                "HOST_TOOLS_OUTER schema=1 status=verified "
                f"outer_sha256={summary['outer_sha256']} "
                f"inner_sha256={summary['bundle_sha256']} "
                f"manifest_sha256={summary['manifest_sha256']} "
                f"capabilities_sha256={summary['capabilities_sha256']}"
            )
            return 0
        if arguments.command == "extract-outer":
            summary = extract_outer_bundle(
                Path(arguments.outer_bundle),
                Path(arguments.output),
                expected_outer_sha256=arguments.expected_outer_sha256,
                expected_inner_sha256=arguments.expected_inner_sha256,
            )
            print(
                "HOST_TOOLS_OUTER schema=1 status=extracted "
                f"outer_sha256={summary['outer_sha256']} inner_sha256={summary['bundle_sha256']}"
            )
            return 0
        if arguments.command == "install":
            install_bundle(
                Path(arguments.outer_bundle),
                host_tools_root=Path(arguments.host_tools_root),
                expected_source_sha=arguments.expected_source_sha,
                expected_outer_sha256=arguments.expected_outer_sha256,
                expected_inner_sha256=arguments.expected_inner_sha256,
                expected_manifest_sha256=arguments.expected_manifest_sha256,
                expected_capabilities_sha256=arguments.expected_capabilities_sha256,
                attestation_evidence=Path(arguments.attestation_evidence),
                source_head_sha=arguments.source_head_sha,
                packaging_commit=arguments.packaging_commit,
                evidence_output=Path(arguments.evidence_output),
            )
            return 0
        if arguments.command == "verify-artifact-metadata":
            verify_artifact_metadata(
                Path(arguments.metadata),
                artifact_id=arguments.artifact_id,
                artifact_name=arguments.artifact_name,
                run_id=arguments.run_id,
                run_attempt=arguments.run_attempt,
                source_sha=arguments.source_sha,
                expected_branch=arguments.expected_branch,
                artifact_digest=arguments.artifact_digest,
                archive_path=Path(arguments.archive),
            )
            print("HOST_TOOLS_BUNDLE schema=1 artifact_metadata=verified")
        if arguments.command == "verify-workflow-attempt":
            verify_workflow_attempt(
                Path(arguments.metadata),
                run_id=arguments.run_id,
                run_attempt=arguments.run_attempt,
                source_sha=arguments.source_sha,
                repository=arguments.repository,
                expected_branch=arguments.expected_branch,
                expected_event=arguments.expected_event,
            )
            print("HOST_TOOLS_BUNDLE schema=1 workflow_attempt=verified")
        return 0
    except (HostToolsBundleError, OSError, ValueError):
        print("host-tools bundle is invalid", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
