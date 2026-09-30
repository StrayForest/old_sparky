#!/usr/bin/env python3
"""Build and verify the immutable production host-tools handoff.

This module is intentionally stdlib-only.  CI uses it on a secret-free
runner to create a deterministic archive for an operator/host-image
provisioning step.  The production workflow may verify the archive as data,
but it never executes an installer from it or copies it to the host.
"""

from __future__ import annotations

import argparse
from collections import deque
import ctypes
import errno
import fcntl
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
from collections.abc import Callable


SCHEMA = 1
PROVENANCE_SCHEMA = 2
TOOLSET_VERSION = "production-host-tools-v2"
MAX_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_RELEASE_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
MAX_RELEASE_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_RELEASE_MEMBER_COUNT = 3
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
RELEASE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")

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
INSTALL_LOCK_NAME = ".host-tools-install.lock"
INSTALL_LOCK_MODE = 0o600
RENAME_NOREPLACE = 1
AT_EMPTY_PATH = 0x1000
AT_FDCWD = -100
AT_SYMLINK_FOLLOW = 0x400
HOST_TOOLS_INVENTORY = frozenset((*HOST_TOOL_FILES, "manifest.json", "capabilities.txt"))
MAX_EVIDENCE_BYTES = 64 * 1024
PROVENANCE_RECEIPT_MAX_BYTES = 64 * 1024
ATTESTATION_ISSUER = "https://token.actions.githubusercontent.com"
ATTESTATION_REPOSITORY = "StrayForest/old_sparky"
ATTESTATION_WORKFLOW_NAME = "Platform production deploy"
ATTESTATION_WORKFLOW_PATH = ".github/workflows/platform-production-deploy.yml"
ATTESTATION_REF = "refs/heads/dev"
ATTESTATION_EVENT = "workflow_dispatch"
# This is an identifier for the independently approved verifier, not a
# cryptographic operation performed by this module.  The receipt digest must
# arrive through a separate trusted channel; the installer only binds the
# exact raw receipt and its closed claims.
ATTESTATION_VERIFIER_ID = (
    "actions/attest-build-provenance@"
    "e8998f949152b193b063cb0ec769d69d929409be"
)
ATTESTATION_VERIFIER_ALLOWLIST = frozenset({ATTESTATION_VERIFIER_ID})
HOST_ARTIFACT_NAME_RE = re.compile(
    r"^platform-host-tools-bundle-(?P<run_id>[1-9][0-9]{0,31})-"
    r"(?P<run_attempt>[1-9][0-9]{0,31})$"
)

# Tests and the operator-side verifier need deterministic ways to exercise
# every post-creation/post-publish interruption window.  The hook is inert in
# production and deliberately raises whatever BaseException the caller asks
# it to raise; cleanup code below must preserve that original exception.
INJECTION_HOOK: Callable[[str], None] | None = None
PROVENANCE_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "status",
        "verifier",
        "issuer",
        "repository",
        "workflow_name",
        "workflow_path",
        "ref",
        "event",
        "run_id",
        "run_attempt",
        "artifact_id",
        "artifact_name",
        "outer_sha256",
        "subject_name",
        "inner_sha256",
        "security_run_id",
        "security_run_attempt",
        "host_tools_sha",
        "source_head_sha",
        "trusted_source_sha",
        "tested_merge_sha",
        "packaging_commit",
    }
)

# Cleanup is best-effort by design once an operation has failed, but the
# failure itself must remain observable without retaining exception values,
# tracebacks or attacker-controlled paths.  Keep only a bounded type/scope
# marker so cleanup cannot replace the original operation exception or grow
# process memory without bound.
_CLEANUP_FAILURES: deque[str] = deque(maxlen=16)


def _record_cleanup_failure(scope: str, failure: BaseException) -> None:
    try:
        marker = f"{scope}:{type(failure).__name__}"
        _CLEANUP_FAILURES.append(marker[:160])
    except BaseException:
        # Recording is itself cleanup bookkeeping and must never escape.
        pass


def cleanup_failures() -> tuple[str, ...]:
    """Return bounded cleanup-failure markers for diagnostics/tests."""

    return tuple(_CLEANUP_FAILURES)


class HostToolsBundleError(ValueError):
    """Bounded validation failure for the offline host-tools contract."""


class _OwnedFile:
    """Identity and byte contract for one file created by this process."""

    __slots__ = ("metadata", "size", "sha256", "mode", "uid", "gid")

    def __init__(
        self,
        metadata: os.stat_result,
        size: int,
        sha256: str,
        mode: int,
        uid: int,
        gid: int,
    ) -> None:
        self.metadata = metadata
        self.size = size
        self.sha256 = sha256
        self.mode = mode
        self.uid = uid
        self.gid = gid


def _injection_point(name: str) -> None:
    hook = INJECTION_HOOK
    if hook is not None:
        hook(name)


def _require_os_flag(name: str) -> int:
    """Require a kernel flag instead of silently weakening the boundary."""

    flag = getattr(os, name, None)
    if not isinstance(flag, int) or flag == 0:
        raise HostToolsBundleError(f"host-tools {name} primitive is unavailable")
    return flag


def _require_fd_primitive(name: str) -> object:
    primitive = getattr(os, name, None)
    if primitive is None or not callable(primitive):
        raise HostToolsBundleError(f"host-tools {name} primitive is unavailable")
    return primitive


def _linkat_function() -> object:
    """Return the libc ``linkat`` primitive used for unnamed publication."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        linkat = libc.linkat
        linkat.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        linkat.restype = ctypes.c_int
        return linkat
    except (AttributeError, OSError, TypeError) as exc:
        raise HostToolsBundleError(
            "host-tools atomic file publish primitive is unavailable"
        ) from exc


def _require_dir_fd_support() -> None:
    supported = getattr(os, "supports_dir_fd", ())
    required = (os.open, os.stat, os.unlink, os.rmdir, os.mkdir)
    if any(primitive not in supported for primitive in required):
        raise HostToolsBundleError("host-tools dir_fd primitive is unavailable")


def _require_install_primitives() -> None:
    """Fail closed when any identity-safe filesystem primitive is absent."""

    for name in ("O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW", "O_EXCL", "O_TMPFILE"):
        _require_os_flag(name)
    _linkat_function()
    _require_dir_fd_support()
    for name in ("pread", "fchmod", "fchown", "fsync"):
        _require_fd_primitive(name)


def _require_no_follow() -> int:
    return _require_os_flag("O_NOFOLLOW")


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_nlink == right.st_nlink
    )


def _trusted_owner() -> tuple[int, int]:
    """Return the uid/gid allowed for secret-free runner output."""

    return os.getuid(), os.getgid()


def _read_fd_digest(descriptor: int, size: int) -> str:
    """Hash exactly ``size`` bytes from an already identity-checked fd."""

    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        if not chunk:
            raise HostToolsBundleError("host-tools file is truncated")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _owned_file(
    metadata: os.stat_result,
    *,
    data: bytes | None = None,
    digest: str | None = None,
    mode: int,
    uid: int,
    gid: int,
) -> _OwnedFile:
    if data is not None:
        size = len(data)
        expected_digest = _sha256_bytes(data)
    else:
        size = metadata.st_size
        expected_digest = digest
    if expected_digest is None:
        raise HostToolsBundleError("host-tools owned file digest is unavailable")
    return _OwnedFile(metadata, size, expected_digest, mode, uid, gid)


def _owned_file_matches(
    metadata: os.stat_result,
    owned: _OwnedFile,
    *,
    descriptor: int | None = None,
) -> bool:
    if (
        not _same_identity(metadata, owned.metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size != owned.size
        or stat.S_IMODE(metadata.st_mode) != owned.mode
        or metadata.st_uid != owned.uid
        or metadata.st_gid != owned.gid
    ):
        return False
    if descriptor is None:
        return False
    try:
        opened = os.fstat(descriptor)
        if not _same_identity(opened, metadata):
            return False
        return _read_fd_digest(descriptor, owned.size) == owned.sha256
    except (OSError, HostToolsBundleError):
        return False


def _close_quietly(descriptor: int | None, *, scope: str = "close") -> None:
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except BaseException as exc:
        # Cleanup must never replace the exception that caused the operation
        # to fail, including KeyboardInterrupt/SystemExit.
        _record_cleanup_failure(scope, exc)


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

    _require_install_primitives()
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
            | _require_os_flag("O_CLOEXEC")
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
        _close_quietly(descriptor)
        _close_quietly(parent_fd)


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
    maximum_compressed_bytes: int | None = None,
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
    compressed_bound = (
        MAX_ARTIFACT_ARCHIVE_BYTES
        if maximum_compressed_bytes is None
        else maximum_compressed_bytes
    )
    if (
        info.file_size < 0
        or info.compress_size < 0
        or info.file_size > maximum_member_bytes
        or info.compress_size > compressed_bound
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

    _require_install_primitives()
    before = _regular_source(path)
    flags = os.O_RDONLY | _require_os_flag("O_CLOEXEC") | _require_no_follow()
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
        _close_quietly(descriptor)


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

    _require_install_primitives()
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

    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _regular_directory(output.parent)
    _safe_leaf(output.name, "host-tools bundle output name")
    parent_fd, parent_metadata = _open_no_symlink_directory(
        output.parent, require_trusted_owner=True
    )
    descriptor: int | None = None
    temporary_identity: os.stat_result | None = None
    published = False
    try:
        descriptor = os.open(
            ".",
            os.O_RDWR
            | _require_os_flag("O_TMPFILE")
            | _require_os_flag("O_CLOEXEC")
            | _require_no_follow(),
            0o600,
            dir_fd=parent_fd,
        )
        owner_uid, owner_gid = _trusted_owner()
        os.fchown(descriptor, owner_uid, owner_gid)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(os.dup(descriptor), "w+b") as stream:
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
        temporary_identity = os.fstat(descriptor)
        if (
            not stat.S_ISREG(temporary_identity.st_mode)
            or temporary_identity.st_dev != parent_metadata.st_dev
            or temporary_identity.st_nlink not in {0, 1}
            or stat.S_IMODE(temporary_identity.st_mode) != 0o600
            or temporary_identity.st_uid != owner_uid
            or temporary_identity.st_gid != owner_gid
            or temporary_identity.st_size > MAX_BUNDLE_BYTES
        ):
            raise HostToolsBundleError("host-tools bundle exceeds its bound")
        _injection_point("build_bundle_after_temp_identity")
        _link_tmpfile_noreplace(descriptor, parent_fd, output.name)
        _injection_point("build_bundle_after_rename")
        published_identity = _reconcile_linked_file(
            parent_fd,
            output.name,
            descriptor,
            temporary_identity,
            expected_device=parent_metadata.st_dev,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            mode=0o600,
            size=temporary_identity.st_size,
            digest=_read_fd_digest(descriptor, temporary_identity.st_size),
        )
        if published_identity is None:
            raise HostToolsBundleError("host-tools bundle publication identity changed")
        published = True
        _injection_point("build_bundle_before_parent_fsync")
        os.fsync(parent_fd)
        _injection_point("build_bundle_after_parent_fsync")
    except HostToolsBundleError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise HostToolsBundleError("host-tools bundle could not be written") from exc
    except BaseException:
        raise
    finally:
        active_failure = sys.exc_info()[1]
        if (
            active_failure is not None
            and not published
            and descriptor is not None
            and temporary_identity is not None
        ):
            # Reconcile the exact inode if linkat completed before an
            # interrupt reached Python.  An uncertain/foreign pathname is
            # retained for operator inspection; no replacement is attempted.
            try:
                _reconcile_linked_file(
                    parent_fd,
                    output.name,
                    descriptor,
                    temporary_identity,
                    expected_device=parent_metadata.st_dev,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                    mode=0o600,
                    size=temporary_identity.st_size,
                    digest=_read_fd_digest(descriptor, temporary_identity.st_size),
                )
            except BaseException as cleanup_failure:
                _record_cleanup_failure("bundle publication reconciliation", cleanup_failure)
        _close_quietly(descriptor, scope="bundle temp close")
        _close_quietly(parent_fd, scope="bundle parent close")
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


def verify_outer_contract(
    outer: Path,
    contract_dir: Path,
    *,
    expected_outer_sha256: str | None = None,
    expected_inner_sha256: str | None = None,
    expected_source_sha: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_capabilities_sha256: str | None = None,
) -> dict[str, object]:
    """Verify an outer artifact and regenerate scalar sidecars from its inner bytes."""

    summary = verify_outer_bundle(
        outer,
        expected_outer_sha256=expected_outer_sha256,
        expected_inner_sha256=expected_inner_sha256,
        expected_source_sha=expected_source_sha,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_capabilities_sha256=expected_capabilities_sha256,
    )
    write_contract_files(summary, contract_dir)
    return summary


def _release_archive_member_name(release_slug: str, suffix: str) -> str:
    if RELEASE_SLUG_RE.fullmatch(release_slug) is None:
        raise HostToolsBundleError("release slug is invalid")
    return f"{release_slug}{suffix}"


def _open_input_archive(path: Path) -> tuple[int, int, os.stat_result]:
    """Open one raw API ZIP through a no-follow parent descriptor."""

    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise HostToolsBundleError("release artifact path is invalid")
    parent_fd, _ = _open_no_symlink_directory(path.parent)
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY
            | _require_os_flag("O_CLOEXEC")
            | _require_no_follow(),
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 0
            or metadata.st_size > MAX_RELEASE_ARTIFACT_BYTES
            or not _same_identity(metadata, current)
        ):
            _close_quietly(descriptor)
            _close_quietly(parent_fd)
            raise HostToolsBundleError("release artifact metadata is unsafe")
        return parent_fd, descriptor, metadata
    except HostToolsBundleError:
        raise
    except OSError as exc:
        _close_quietly(parent_fd)
        raise HostToolsBundleError("release artifact cannot be opened") from exc


def _snapshot_input_archive(path: Path) -> tuple[int, str, int]:
    """Copy the raw API ZIP once into an immutable anonymous snapshot.

    The API digest and the bytes later parsed by ``zipfile`` come from one
    source pass.  Parsing never reuses the mutable input descriptor: after the
    copy is fsynced, only the anonymous ``O_TMPFILE`` descriptor is opened by
    the ZIP reader.  A same-inode/same-size source rewrite after this point
    therefore cannot alter the verified extraction, while a rewrite during the
    copy can only produce a digest mismatch against the independently supplied
    expected digest.
    """

    parent_fd, source_fd, source_metadata = _open_input_archive(path)
    snapshot_fd: int | None = None
    try:
        owner_uid, owner_gid = _trusted_owner()
        snapshot_fd = os.open(
            ".",
            os.O_RDWR
            | _require_os_flag("O_TMPFILE")
            | _require_os_flag("O_CLOEXEC")
            | _require_no_follow(),
            0o600,
            dir_fd=parent_fd,
        )
        os.fchown(snapshot_fd, owner_uid, owner_gid)
        os.fchmod(snapshot_fd, 0o600)
        digest = hashlib.sha256()
        total = 0
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(
                source_fd,
                min(1024 * 1024, MAX_RELEASE_ARTIFACT_BYTES - total + 1),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RELEASE_ARTIFACT_BYTES:
                raise HostToolsBundleError("release artifact exceeds its size bound")
            _write_all(snapshot_fd, chunk)
            digest.update(chunk)
        source_after = os.fstat(source_fd)
        if (
            not _same_identity(source_after, source_metadata)
            or source_after.st_size != total
        ):
            raise HostToolsBundleError("release artifact changed while reading")
        os.fsync(snapshot_fd)
        snapshot_metadata = os.fstat(snapshot_fd)
        if (
            not stat.S_ISREG(snapshot_metadata.st_mode)
            or snapshot_metadata.st_dev != source_metadata.st_dev
            or snapshot_metadata.st_nlink not in {0, 1}
            or snapshot_metadata.st_uid != owner_uid
            or snapshot_metadata.st_gid != owner_gid
            or stat.S_IMODE(snapshot_metadata.st_mode) != 0o600
            or snapshot_metadata.st_size != total
            or _read_fd_digest(snapshot_fd, total) != digest.hexdigest()
        ):
            raise HostToolsBundleError("release artifact snapshot metadata is unsafe")
        os.lseek(snapshot_fd, 0, os.SEEK_SET)
        return snapshot_fd, digest.hexdigest(), total
    except HostToolsBundleError:
        _close_quietly(snapshot_fd, scope="release snapshot close")
        raise
    except OSError as exc:
        _close_quietly(snapshot_fd, scope="release snapshot close")
        raise HostToolsBundleError("release artifact snapshot could not be created") from exc
    finally:
        _close_quietly(source_fd, scope="release source close")
        _close_quietly(parent_fd, scope="release source parent close")


def _write_release_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    stage_fd: int,
    *,
    expected_device: int,
) -> _OwnedFile:
    """Stream one exact raw-release ZIP member into the private stage."""

    name = info.filename
    owner_uid, owner_gid = _trusted_owner()
    descriptor: int | None = None
    identity: os.stat_result | None = None
    created = False
    completed = False
    try:
        descriptor = os.open(
            name,
            os.O_RDWR
            | os.O_CREAT
            | _require_os_flag("O_EXCL")
            | _require_os_flag("O_CLOEXEC")
            | _require_no_follow(),
            0o600,
            dir_fd=stage_fd,
        )
        created = True
        total = 0
        digest = hashlib.sha256()
        try:
            source = archive.open(info, mode="r")
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
            raise HostToolsBundleError("release artifact member cannot be opened") from exc
        with source:
            while True:
                chunk = source.read(min(1024 * 1024, MAX_RELEASE_MEMBER_BYTES - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RELEASE_MEMBER_BYTES:
                    raise HostToolsBundleError("release artifact member exceeds its bound")
                _write_all(descriptor, chunk)
                digest.update(chunk)
        if total != info.file_size:
            raise HostToolsBundleError("release artifact member is truncated")
        os.fchown(descriptor, owner_uid, owner_gid)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        identity = os.fstat(descriptor)
        if (
            not stat.S_ISREG(identity.st_mode)
            or identity.st_dev != expected_device
            or identity.st_nlink != 1
            or identity.st_size != total
            or identity.st_uid != owner_uid
            or identity.st_gid != owner_gid
            or stat.S_IMODE(identity.st_mode) != 0o600
        ):
            raise HostToolsBundleError("release artifact member metadata is unsafe")
        pathname = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
        if not _same_identity(pathname, identity):
            raise HostToolsBundleError("release artifact member identity changed")
        completed = True
        return _OwnedFile(identity, total, digest.hexdigest(), 0o600, owner_uid, owner_gid)
    except HostToolsBundleError:
        raise
    except OSError as exc:
        raise HostToolsBundleError("release artifact member could not be written") from exc
    finally:
        _close_quietly(descriptor)
        if created and identity is None:
            try:
                identity = _lstat_at(stage_fd, name)
            except BaseException as cleanup_failure:
                identity = None
                _record_cleanup_failure("release member identity cleanup", cleanup_failure)
        if created and not completed and identity is not None:
            # The caller's directory cleanup owns the complete-file case.  A
            # partial member is removed here only after the exact inode is
            # still visible; a replacement remains quarantined.
            try:
                current = _lstat_at(stage_fd, name)
                if _same_identity(current, identity):
                    os.unlink(name, dir_fd=stage_fd)
                    os.fsync(stage_fd)
            except BaseException as cleanup_failure:
                _record_cleanup_failure("release member cleanup", cleanup_failure)


def extract_release_artifact(
    archive_path: Path,
    output_dir: Path,
    *,
    release_slug: str,
    expected_archive_sha256: str,
) -> dict[str, str]:
    """Verify and atomically extract the exact three-file API artifact ZIP.

    This runs from the pinned host-tools checkout before deployment secrets are
    validated.  It never consumes an ``actions/download-artifact`` directory;
    the raw API ZIP bytes that are digested here are the bytes later copied to
    the production host.
    """

    _require_install_primitives()
    _safe_leaf(output_dir.name, "release extraction directory name")
    if (
        not isinstance(output_dir, Path)
        or not output_dir.is_absolute()
        or ".." in output_dir.parts
        or output_dir == Path("/")
    ):
        raise HostToolsBundleError("release extraction directory path is invalid")
    expected_archive = _digest_argument(
        expected_archive_sha256, "expected release artifact digest"
    )
    artifact_name = _release_archive_member_name(release_slug, ".tar.gz")
    checksum_name = _release_archive_member_name(release_slug, ".tar.gz.sha256")
    provenance_name = "RELEASE.provenance.json"
    expected_names = {artifact_name, checksum_name, provenance_name}
    archive_snapshot_fd: int | None = None
    archive_stream = None
    output_parent_fd: int | None = None
    stage_fd: int | None = None
    stage_name: str | None = None
    stage_identity: os.stat_result | None = None
    owned_members: dict[str, _OwnedFile] = {}
    published = False
    try:
        archive_snapshot_fd, raw_archive_digest, _ = _snapshot_input_archive(archive_path)
        if raw_archive_digest != expected_archive:
            raise HostToolsBundleError("release artifact digest does not match API bytes")
        # From this point onward the source pathname is irrelevant.  The
        # original API ZIP may be replaced or rewritten, but the parser and
        # extractor consume only the fsynced anonymous snapshot.
        _injection_point("release_extract_after_snapshot")
        archive_stream = os.fdopen(os.dup(archive_snapshot_fd), "rb", closefd=True)
        try:
            archive = zipfile.ZipFile(archive_stream, mode="r", allowZip64=False)
        except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise HostToolsBundleError("release artifact ZIP is invalid") from exc
        with archive:
            infos = archive.infolist()
            if len(infos) != MAX_RELEASE_MEMBER_COUNT:
                raise HostToolsBundleError("release artifact member count is invalid")
            seen: set[str] = set()
            total_uncompressed = 0
            for info in infos:
                _bounded_zip_info(
                    info,
                    expected_name=None,
                    maximum_member_bytes=MAX_RELEASE_MEMBER_BYTES,
                    maximum_compressed_bytes=MAX_RELEASE_ARTIFACT_BYTES,
                )
                if info.filename not in expected_names or info.filename in seen:
                    raise HostToolsBundleError("release artifact member allowlist is invalid")
                seen.add(info.filename)
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_RELEASE_ARTIFACT_BYTES:
                    raise HostToolsBundleError("release artifact expanded size exceeds its bound")
            if seen != expected_names:
                raise HostToolsBundleError("release artifact member allowlist is incomplete")
            output_parent_fd, output_parent_metadata = _open_no_symlink_directory(
                output_dir.parent, require_trusted_owner=True
            )
            for _ in range(16):
                candidate = f".release-stage-{secrets.token_hex(8)}"
                try:
                    os.mkdir(candidate, STAGE_MODE, dir_fd=output_parent_fd)
                except FileExistsError:
                    continue
                stage_name = candidate
                break
            if stage_name is None:
                raise HostToolsBundleError("release extraction stage name collision")
            stage_identity = _lstat_at(output_parent_fd, stage_name)
            owner_uid, owner_gid = _trusted_owner()
            if (
                not stat.S_ISDIR(stage_identity.st_mode)
                or stage_identity.st_dev != output_parent_metadata.st_dev
                or stage_identity.st_nlink != 2
                or stage_identity.st_uid != owner_uid
                or stage_identity.st_gid != owner_gid
                or stat.S_IMODE(stage_identity.st_mode) != STAGE_MODE
            ):
                raise HostToolsBundleError("release extraction stage metadata is unsafe")
            _injection_point("release_extract_after_mkdir")
            current_stage = _lstat_at(output_parent_fd, stage_name)
            if not _same_identity(current_stage, stage_identity):
                raise HostToolsBundleError("release extraction stage identity changed")
            stage_fd = os.open(
                stage_name,
                os.O_RDONLY
                | _require_os_flag("O_DIRECTORY")
                | _require_os_flag("O_CLOEXEC")
                | _require_no_follow(),
                dir_fd=output_parent_fd,
            )
            opened_stage = os.fstat(stage_fd)
            if not _same_identity(opened_stage, stage_identity):
                raise HostToolsBundleError("release extraction stage identity changed")
            by_name = {info.filename: info for info in infos}
            for name in sorted(expected_names):
                owned_members[name] = _write_release_member(
                    archive,
                    by_name[name],
                    stage_fd,
                    expected_device=output_parent_metadata.st_dev,
                )
            if set(os.listdir(stage_fd)) != expected_names:
                raise HostToolsBundleError("release extraction inventory is not closed")
            os.fsync(stage_fd)
            _injection_point("release_extract_before_rename")
            _rename_noreplace(
                output_parent_fd,
                stage_name,
                output_dir.name,
                stage_identity,
            )
            _injection_point("release_extract_after_rename")
            output_identity = _reconcile_renamed_directory(
                output_parent_fd,
                output_dir.name,
                stage_identity,
                expected_device=output_parent_metadata.st_dev,
            )
            if output_identity is None:
                raise HostToolsBundleError("release extraction publication identity changed")
            published = True
            _injection_point("release_extract_before_parent_fsync")
            os.fsync(output_parent_fd)
            _injection_point("release_extract_after_parent_fsync")
    except HostToolsBundleError:
        raise
    except OSError as exc:
        raise HostToolsBundleError("release artifact extraction failed") from exc
    except (ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise HostToolsBundleError("release artifact extraction failed") from exc
    except BaseException:
        raise
    finally:
        active_failure = sys.exc_info()[1]
        if archive_stream is not None:
            try:
                archive_stream.close()
            except BaseException as exc:
                _record_cleanup_failure("release archive stream close", exc)
        _close_quietly(archive_snapshot_fd, scope="release snapshot close")
        _close_quietly(stage_fd)
        if active_failure is not None and not published and output_parent_fd is not None:
            if stage_name is not None and stage_identity is not None:
                try:
                    reconciled = _reconcile_renamed_directory(
                        output_parent_fd,
                        output_dir.name,
                        stage_identity,
                    )
                except BaseException as exc:
                    _record_cleanup_failure("release rename reconciliation", exc)
                    reconciled = None
                if reconciled is not None:
                    # The kernel move may have completed before Python
                    # observed an interrupt from the rename helper.  Keep
                    # the exact published directory for operator
                    # reconciliation; never attempt to remove it as a stage.
                    published = True
                    stage_name = None
                if not published:
                    try:
                        _cleanup_owned_directory(
                            output_parent_fd,
                            stage_name,
                            stage_identity,
                            owned_members,
                            expected_device=os.fstat(output_parent_fd).st_dev,
                        )
                    except BaseException as exc:
                        _record_cleanup_failure("release stage cleanup", exc)
        _close_quietly(output_parent_fd)
    return {
        "archive": str(output_dir / artifact_name),
        "checksum": str(output_dir / checksum_name),
        "provenance": str(output_dir / provenance_name),
        "release_slug": release_slug,
        "archive_sha256": expected_archive,
    }


def _open_no_symlink_directory(
    path: Path,
    *,
    require_root_owned: bool = False,
    require_trusted_owner: bool = False,
) -> tuple[int, os.stat_result]:
    """Open every directory component with ``openat`` and ``O_NOFOLLOW``."""

    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise HostToolsBundleError("host-tools directory path is invalid")
    _require_install_primitives()
    no_follow = _require_no_follow()
    directory_flag = _require_os_flag("O_DIRECTORY")
    close_flag = _require_os_flag("O_CLOEXEC")
    descriptor = os.open(
        "/",
        os.O_RDONLY
        | directory_flag
        | close_flag
        | no_follow,
    )
    try:
        for component in path.parts[1:]:
            try:
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY
                    | directory_flag
                    | close_flag
                    | no_follow,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise HostToolsBundleError("host-tools directory is unavailable") from exc
            _close_quietly(descriptor)
            descriptor = next_descriptor
            if require_root_owned or require_trusted_owner:
                component_metadata = os.fstat(descriptor)
                final_component = component == path.parts[-1]
                mode = stat.S_IMODE(component_metadata.st_mode)
                trusted_uid, trusted_gid = _trusted_owner()
                if require_root_owned:
                    owner_invalid = (
                        component_metadata.st_uid != 0
                        or component_metadata.st_gid != 0
                    )
                else:
                    owner_invalid = final_component and (
                        (
                            component_metadata.st_uid != trusted_uid
                            or component_metadata.st_gid != trusted_gid
                        )
                        and not (
                            mode & 0o002
                            and mode & stat.S_ISVTX
                            and not mode & 0o020
                        )
                    )
                writable_invalid = bool(mode & 0o022) and (
                    final_component
                    or not (mode & 0o002 and mode & stat.S_ISVTX)
                )
                if owner_invalid or writable_invalid:
                    raise HostToolsBundleError("host-tools handoff parent metadata is unsafe")
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise HostToolsBundleError("host-tools directory is unsafe")
        return descriptor, metadata
    except HostToolsBundleError:
        _close_quietly(descriptor)
        raise
    except OSError as exc:
        _close_quietly(descriptor)
        raise HostToolsBundleError("host-tools directory cannot be opened") from exc


def extract_outer_bundle(
    outer: Path,
    output: Path,
    *,
    expected_outer_sha256: str | None = None,
    expected_inner_sha256: str | None = None,
) -> dict[str, object]:
    """Write the verified inner ZIP exactly once using an identity-safe file."""

    _require_install_primitives()
    if expected_outer_sha256 is None or expected_inner_sha256 is None:
        raise HostToolsBundleError(
            "outer extraction requires expected outer and inner digests"
        )
    _safe_leaf(output.name, "host-tools extracted member name")
    summary = verify_outer_bundle(
        outer,
        expected_outer_sha256=expected_outer_sha256,
        expected_inner_sha256=expected_inner_sha256,
    )
    parent_fd, parent_before = _open_no_symlink_directory(
        output.parent, require_trusted_owner=True
    )
    descriptor: int | None = None
    temporary_identity: os.stat_result | None = None
    published_identity: os.stat_result | None = None
    published = False
    try:
        owner_uid, owner_gid = _trusted_owner()
        try:
            descriptor = os.open(
                ".",
                os.O_RDWR
                | _require_os_flag("O_TMPFILE")
                | _require_os_flag("O_CLOEXEC")
                | _require_no_follow(),
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise HostToolsBundleError("host-tools extracted member could not be opened") from exc
        os.fchown(descriptor, owner_uid, owner_gid)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, bytes(summary["inner_bytes"]))
        os.fsync(descriptor)
        temporary_identity = os.fstat(descriptor)
        if (
            not stat.S_ISREG(temporary_identity.st_mode)
            or temporary_identity.st_dev != parent_before.st_dev
            or temporary_identity.st_nlink not in {0, 1}
            or temporary_identity.st_uid != owner_uid
            or temporary_identity.st_gid != owner_gid
            or stat.S_IMODE(temporary_identity.st_mode) != 0o600
            or temporary_identity.st_size != len(summary["inner_bytes"])
            or _read_fd_digest(descriptor, temporary_identity.st_size)
            != summary["bundle_sha256"]
        ):
            raise HostToolsBundleError("host-tools extracted member metadata is unsafe")
        _injection_point("outer_extract_after_write")
        _link_tmpfile_noreplace(descriptor, parent_fd, output.name)
        _injection_point("outer_extract_after_link")
        published_identity = _reconcile_linked_file(
            parent_fd,
            output.name,
            descriptor,
            temporary_identity,
            expected_device=parent_before.st_dev,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            mode=0o600,
            size=temporary_identity.st_size,
            digest=summary["bundle_sha256"],
        )
        if published_identity is None:
            raise HostToolsBundleError("host-tools extracted member identity changed")
        published = True
    except HostToolsBundleError:
        raise
    except OSError as exc:
        raise HostToolsBundleError("host-tools extracted member could not be written") from exc
    except BaseException:
        # Keep the original exception.  In particular, a signal must not be
        # rewritten as an OSError or hidden by cleanup failures.
        raise
    finally:
        active_failure = sys.exc_info()[1]
        if (
            active_failure is not None
            and not published
            and descriptor is not None
            and temporary_identity is not None
        ):
            try:
                reconciled = _reconcile_linked_file(
                    parent_fd,
                    output.name,
                    descriptor,
                    temporary_identity,
                    expected_device=parent_before.st_dev,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                    mode=0o600,
                    size=temporary_identity.st_size,
                    digest=summary["bundle_sha256"],
                )
            except BaseException as exc:
                _record_cleanup_failure("outer file reconciliation", exc)
                reconciled = None
            if reconciled is not None:
                # linkat may have completed before an injected signal or
                # interpreter exit reached Python.  Retain the exact output
                # and let the caller/retry reconcile it; never unlink a
                # pathname that may have crossed the publication boundary.
                published = True
                published_identity = reconciled
        _close_quietly(descriptor)
        if active_failure is not None:
            _close_quietly(parent_fd, scope="outer parent close")
    if not published:
        _close_quietly(parent_fd)
        raise HostToolsBundleError("host-tools extracted member was not published")
    try:
        _injection_point("outer_extract_before_parent_fsync")
        os.fsync(parent_fd)
        _injection_point("outer_extract_after_parent_fsync")
    except BaseException as exc:
        # Publishing is complete only after the containing directory is
        # durable.  The linked inode is retained when this durability step
        # is interrupted or fails; a retry adopts/reconciles the exact final
        # commit marker instead of guessing at pathname ownership.
        _close_quietly(parent_fd)
        if not isinstance(exc, OSError):
            raise
        raise HostToolsBundleError("host-tools output parent could not be synced") from exc
    _close_quietly(parent_fd)
    return {key: value for key, value in summary.items() if key != "inner_bytes"}


def _safe_leaf(value: str, description: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", value) is None:
        raise HostToolsBundleError(f"{description} is invalid")
    return value


def _write_exclusive_file_at(
    parent_fd: int,
    name: str,
    data: bytes,
    mode: int,
    *,
    expected_device: int | None = None,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
    description: str,
) -> os.stat_result:
    """Write one bounded file and publish no pathname other than its inode.

    The helper is used for generated handoff sidecars as well as the
    installer-facing files.  It deliberately has no replacement mode: a
    stale destination is an operator-visible failure, not an overwrite.
    """

    _safe_leaf(name, f"{description} name")
    if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
        raise HostToolsBundleError(f"{description} exceeds its bound")
    _require_install_primitives()
    descriptor: int | None = None
    created = False
    identity: os.stat_result | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDWR
            | os.O_CREAT
            | _require_os_flag("O_EXCL")
            | _require_os_flag("O_CLOEXEC")
            | _require_no_follow(),
            0o600,
            dir_fd=parent_fd,
        )
        created = True
        _write_all(descriptor, data)
        if owner_uid is not None or owner_gid is not None:
            os.fchown(
                descriptor,
                0 if owner_uid is None else owner_uid,
                0 if owner_gid is None else owner_gid,
            )
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        identity = os.fstat(descriptor)
        if (
            not stat.S_ISREG(identity.st_mode)
            or identity.st_nlink != 1
            or identity.st_size != len(data)
            or stat.S_IMODE(identity.st_mode) != mode
            or (expected_device is not None and identity.st_dev != expected_device)
            or (owner_uid is not None and identity.st_uid != owner_uid)
            or (owner_gid is not None and identity.st_gid != owner_gid)
            or _sha256_bytes(os.pread(descriptor, identity.st_size, 0)) != _sha256_bytes(data)
        ):
            raise HostToolsBundleError(f"{description} metadata is unsafe")
        pathname = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not _same_identity(pathname, identity)
            or not stat.S_ISREG(pathname.st_mode)
            or pathname.st_size != len(data)
            or stat.S_IMODE(pathname.st_mode) != mode
            or (expected_device is not None and pathname.st_dev != expected_device)
            or (owner_uid is not None and pathname.st_uid != owner_uid)
            or (owner_gid is not None and pathname.st_gid != owner_gid)
        ):
            raise HostToolsBundleError(f"{description} identity changed")
        os.fsync(parent_fd)
        return identity
    except HostToolsBundleError:
        raise
    except OSError as exc:
        raise HostToolsBundleError(f"{description} could not be written") from exc
    except BaseException:
        raise
    finally:
        active_failure = sys.exc_info()[1]
        if active_failure is not None and descriptor is not None and created and identity is None:
            try:
                identity = os.fstat(descriptor)
            except BaseException as cleanup_failure:
                _record_cleanup_failure(f"{description} identity cleanup", cleanup_failure)
        _close_quietly(descriptor, scope=f"{description} descriptor close")
        if active_failure is not None and created and identity is not None:
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if _same_identity(current, identity):
                    os.unlink(name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            except BaseException as cleanup_failure:
                _record_cleanup_failure(f"{description} cleanup", cleanup_failure)
    raise AssertionError("exclusive file helper completed without a result")


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
    artifact_name: str,
    trusted_source_sha: str,
    tested_merge_sha: str,
    security_run_id: str,
    security_run_attempt: str,
    expected_receipt_sha256: str,
) -> dict[str, object]:
    """Require the closed v2 receipt from an independently trusted verifier.

    This helper does not perform cryptographic attestation.  It binds the raw
    receipt bytes to a digest supplied through an independent trusted channel
    and checks every allowlisted claim.  The digest is not derived from an
    untrusted receipt field and no ad-hoc signature/crypto scheme is invented
    here.
    """

    host = _safe_generation_sha(host_tools_sha)
    source = _safe_generation_sha(source_head_sha, "source head SHA")
    packaging = _safe_generation_sha(packaging_commit, "packaging commit")
    trusted = _safe_generation_sha(trusted_source_sha, "trusted source SHA")
    tested = _safe_generation_sha(tested_merge_sha, "tested merge SHA")
    if not isinstance(artifact_id, str) or re.fullmatch(r"[1-9][0-9]{0,31}", artifact_id) is None:
        raise HostToolsBundleError("artifact ID is invalid")
    if not isinstance(artifact_name, str) or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,240}", artifact_name
    ) is None:
        raise HostToolsBundleError("artifact name is invalid")
    artifact_match = HOST_ARTIFACT_NAME_RE.fullmatch(artifact_name)
    if artifact_match is None:
        raise HostToolsBundleError("artifact name is not the exact host-tools artifact")
    for label, value in (
        ("security run ID", security_run_id),
        ("security run attempt", security_run_attempt),
    ):
        if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,31}", value) is None:
            raise HostToolsBundleError(f"{label} is invalid")
    expected_receipt = _digest_argument(
        expected_receipt_sha256, "expected provenance receipt digest"
    )
    payload, _ = _read_regular_bytes(
        evidence_path, PROVENANCE_RECEIPT_MAX_BYTES, "external attestation evidence"
    )
    if _sha256_bytes(payload) != expected_receipt:
        raise HostToolsBundleError("external attestation receipt digest does not match")
    try:
        parsed = json.loads(payload.decode("ascii"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, HostToolsBundleError) as exc:
        raise HostToolsBundleError("external attestation evidence is invalid") from exc
    if not isinstance(parsed, dict):
        raise HostToolsBundleError("external attestation evidence is invalid")
    if set(parsed) != PROVENANCE_RECEIPT_KEYS:
        raise HostToolsBundleError("external attestation evidence schema is not closed")
    verifier = parsed.get("verifier")
    if not isinstance(verifier, str):
        raise HostToolsBundleError("external attestation verifier ID is invalid")
    if (
        type(parsed.get("schema")) is not int
        or not isinstance(parsed.get("status"), str)
        or parsed.get("schema") != PROVENANCE_SCHEMA
        or parsed.get("status") != "satisfied"
        or verifier not in ATTESTATION_VERIFIER_ALLOWLIST
        or parsed.get("issuer") != ATTESTATION_ISSUER
        or parsed.get("repository") != ATTESTATION_REPOSITORY
        or parsed.get("workflow_name") != ATTESTATION_WORKFLOW_NAME
        or parsed.get("workflow_path") != ATTESTATION_WORKFLOW_PATH
        or parsed.get("ref") != ATTESTATION_REF
        or parsed.get("event") != ATTESTATION_EVENT
        or parsed.get("artifact_name") != artifact_name
        or parsed.get("subject_name") != OUTER_MEMBER_NAME
        or parsed.get("run_id") is None
        or parsed.get("run_attempt") is None
        or not isinstance(parsed.get("run_id"), str)
        or not isinstance(parsed.get("run_attempt"), str)
        or re.fullmatch(r"[1-9][0-9]{0,31}", parsed.get("run_id")) is None
        or re.fullmatch(r"[1-9][0-9]{0,31}", parsed.get("run_attempt")) is None
        or parsed.get("run_id") != artifact_match.group("run_id")
        or parsed.get("run_attempt") != artifact_match.group("run_attempt")
        or parsed.get("security_run_id") != security_run_id
        or parsed.get("security_run_attempt") != security_run_attempt
        or parsed.get("host_tools_sha") != host
        or parsed.get("source_head_sha") != source
        or parsed.get("trusted_source_sha") != trusted
        or parsed.get("tested_merge_sha") != tested
        or parsed.get("packaging_commit") != packaging
        or parsed.get("artifact_id") != artifact_id
        or _digest_argument(str(parsed.get("outer_sha256")), "attestation outer digest")
        != outer_sha256
        or _digest_argument(str(parsed.get("inner_sha256")), "attestation inner digest")
        != inner_sha256
    ):
        raise HostToolsBundleError("external attestation evidence does not bind the handoff")
    return {
        "schema": PROVENANCE_SCHEMA,
        "status": "satisfied",
        "verifier": verifier,
        "issuer": ATTESTATION_ISSUER,
        "repository": ATTESTATION_REPOSITORY,
        "workflow_name": ATTESTATION_WORKFLOW_NAME,
        "workflow_path": ATTESTATION_WORKFLOW_PATH,
        "ref": ATTESTATION_REF,
        "event": ATTESTATION_EVENT,
        "run_id": parsed["run_id"],
        "run_attempt": parsed["run_attempt"],
        "host_tools_sha": host,
        "source_head_sha": source,
        "trusted_source_sha": trusted,
        "tested_merge_sha": tested,
        "packaging_commit": packaging,
        "artifact_id": artifact_id,
        "artifact_name": artifact_name,
        "subject_name": OUTER_MEMBER_NAME,
        "security_run_id": security_run_id,
        "security_run_attempt": security_run_attempt,
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
    _require_install_primitives()
    no_follow = _require_no_follow()
    directory_flag = _require_os_flag("O_DIRECTORY")
    close_flag = _require_os_flag("O_CLOEXEC")
    descriptor = os.open(
        "/",
        os.O_RDONLY
        | directory_flag
        | close_flag
        | no_follow,
    )
    try:
        components = path.parts[1:]
        for index, component in enumerate(components):
            try:
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY
                    | directory_flag
                    | close_flag
                    | no_follow,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise HostToolsBundleError("host-tools root parent is unavailable") from exc
            previous_descriptor = descriptor
            descriptor = next_descriptor
            _close_quietly(previous_descriptor)
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
        _close_quietly(descriptor)
        raise
    except OSError as exc:
        _close_quietly(descriptor)
        raise HostToolsBundleError("host-tools root cannot be opened") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink < 2
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        _close_quietly(descriptor)
        raise HostToolsBundleError("host-tools root metadata is unsafe")
    return descriptor, metadata


def _open_install_lock(host_tools_root: Path) -> tuple[int, int]:
    """Take the root-side host-tools installation lock.

    The lock lives beside (rather than inside) the closed generation
    inventory.  Its parent is walked with the same root-owned/no-follow
    policy as the host-tools root, and the lock inode is never replaced or
    deleted by this helper.  A concurrent installer therefore fails before it
    can inspect, stage, publish, or adopt a generation.
    """

    parent_fd, parent_metadata = _open_no_symlink_directory(
        host_tools_root.parent, require_root_owned=True
    )
    lock_fd: int | None = None
    try:
        lock_fd = os.open(
            INSTALL_LOCK_NAME,
            os.O_RDWR
            | os.O_CREAT
            | _require_os_flag("O_CLOEXEC")
            | _require_no_follow(),
            INSTALL_LOCK_MODE,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != parent_metadata.st_dev
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != INSTALL_LOCK_MODE
        ):
            raise HostToolsBundleError("host-tools install lock metadata is unsafe")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise HostToolsBundleError("host-tools install lock is held") from exc
            raise HostToolsBundleError("host-tools install lock cannot be acquired") from exc
        return parent_fd, lock_fd
    except HostToolsBundleError:
        _close_quietly(lock_fd, scope="install lock close")
        _close_quietly(parent_fd, scope="install lock parent close")
        raise
    except OSError as exc:
        _close_quietly(lock_fd, scope="install lock close")
        _close_quietly(parent_fd, scope="install lock parent close")
        raise HostToolsBundleError("host-tools install lock cannot be opened") from exc


def _lstat_at(parent_fd: int, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise HostToolsBundleError("host-tools path metadata is unavailable") from exc


def _reconcile_linked_file(
    parent_fd: int,
    target: str,
    descriptor: int,
    temporary_identity: os.stat_result,
    *,
    expected_device: int,
    owner_uid: int,
    owner_gid: int,
    mode: int,
    size: int,
    digest: str,
) -> os.stat_result | None:
    """Prove that a pathname is the exact inode just linked by this process.

    ``O_TMPFILE`` starts with link count zero and becomes link count one after
    ``linkat(AT_EMPTY_PATH)``.  Reconciliation therefore compares the stable
    device/inode pair and then checks the published pathname, open descriptor,
    metadata and bytes independently.  A missing or foreign pathname is never
    repaired by this helper.
    """

    try:
        current = _lstat_at(parent_fd, target)
        opened = os.fstat(descriptor)
    except (HostToolsBundleError, OSError):
        return None
    if (
        current.st_dev != temporary_identity.st_dev
        or current.st_ino != temporary_identity.st_ino
        or opened.st_dev != current.st_dev
        or opened.st_ino != current.st_ino
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or current.st_dev != expected_device
        or current.st_uid != owner_uid
        or current.st_gid != owner_gid
        or stat.S_IMODE(current.st_mode) != mode
        or current.st_size != size
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_size != size
        or opened.st_uid != owner_uid
        or opened.st_gid != owner_gid
        or stat.S_IMODE(opened.st_mode) != mode
    ):
        return None
    try:
        return current if _read_fd_digest(descriptor, size) == digest else None
    except (HostToolsBundleError, OSError):
        return None


def _reconcile_renamed_directory(
    parent_fd: int,
    target: str,
    expected_identity: os.stat_result,
    *,
    expected_device: int | None = None,
) -> os.stat_result | None:
    """Reconcile a directory after ``renameat2`` may have completed.

    The stage and final generation names are different pathnames for the same
    directory inode.  Only an exact device/inode/type/link-count match is
    considered a successful move; a foreign target is left untouched.
    """

    try:
        current = _lstat_at(parent_fd, target)
    except (HostToolsBundleError, OSError):
        return None
    if (
        current.st_dev != expected_identity.st_dev
        or current.st_ino != expected_identity.st_ino
        or stat.S_IFMT(current.st_mode) != stat.S_IFMT(expected_identity.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or current.st_nlink != expected_identity.st_nlink
        or (expected_device is not None and current.st_dev != expected_device)
    ):
        return None
    return current


def _link_tmpfile_noreplace(descriptor: int, parent_fd: int, target: str) -> None:
    """Atomically publish an unnamed O_TMPFILE inode without replacement."""

    _safe_leaf(target, "host-tools output name")
    linkat = _linkat_function()
    destination = os.fsencode(target)
    if linkat(descriptor, b"", parent_fd, destination, AT_EMPTY_PATH) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise HostToolsBundleError("host-tools output already exists")
        if error in {errno.EACCES, errno.EPERM, errno.ENOENT}:
            # Linux restricts AT_EMPTY_PATH to callers with
            # CAP_DAC_READ_SEARCH even when the O_TMPFILE inode is owned by
            # the caller.  The procfd spelling resolves the same unnamed
            # inode through the kernel's fd link and remains a linkat-only,
            # no-overwrite publication; it never falls back to a named
            # temporary file or pathname replacement.
            procfd = os.fsencode(f"/proc/self/fd/{descriptor}")
            if linkat(AT_FDCWD, procfd, parent_fd, destination, AT_SYMLINK_FOLLOW) == 0:
                _injection_point("linkat_after_success")
                return
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise HostToolsBundleError("host-tools output already exists")
        if error in {errno.EXDEV, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP}:
            raise HostToolsBundleError("host-tools atomic file publish cannot be proven")
        raise HostToolsBundleError("host-tools atomic file publish failed")
    _injection_point("linkat_after_success")


def _rename_noreplace(
    parent_fd: int,
    source: str,
    target: str,
    expected_source_identity: os.stat_result | None = None,
) -> None:
    """Publish within one directory using Linux ``RENAME_NOREPLACE`` only."""

    _require_install_primitives()
    _safe_leaf(source, "host-tools stage name")
    _safe_leaf(target, "host-tools generation name")
    if expected_source_identity is not None:
        current_source = _lstat_at(parent_fd, source)
        if (
            current_source.st_dev != expected_source_identity.st_dev
            or current_source.st_ino != expected_source_identity.st_ino
            or stat.S_IFMT(current_source.st_mode)
            != stat.S_IFMT(expected_source_identity.st_mode)
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
    except (AttributeError, OSError, TypeError) as exc:
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
    # The kernel move has completed at this point, but the caller has not yet
    # updated its publication state.  Keep this explicit interruption window
    # so reconciliation tests (and the real handler) cover that gap.
    _injection_point("rename_noreplace_after_success")


def _new_stage(
    host_fd: int,
    source_sha: str,
    *,
    expected_device: int,
) -> tuple[str, int, os.stat_result]:
    """Create and retain the private stage directory descriptor.

    The descriptor is opened before returning and is the only directory handle
    used for member writes.  The pathname is merely a publication name; a
    replacement of that name cannot redirect writes through the retained
    descriptor and is rejected at the pre-rename reconciliation.
    """

    _require_install_primitives()
    for _ in range(16):
        name = f".host-tools-stage-{source_sha}-{secrets.token_hex(8)}"
        metadata: os.stat_result | None = None
        stage_fd: int | None = None
        try:
            os.mkdir(name, STAGE_MODE, dir_fd=host_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise HostToolsBundleError("host-tools private stage could not be created") from exc
        try:
            metadata = _lstat_at(host_fd, name)
            _injection_point("new_stage_after_mkdir")
            # The hook models an attacker replacing the public stage name
            # after mkdir.  Reconcile the name before opening it so the
            # retained descriptor can never be directed at a replacement
            # inode.  Once opened, all member writes are fd-relative.
            current_name = _lstat_at(host_fd, name)
            if not _same_identity(current_name, metadata):
                raise HostToolsBundleError("host-tools private stage identity changed")
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_gid != 0
                or metadata.st_nlink != 2
                or stat.S_IMODE(metadata.st_mode) != STAGE_MODE
                or metadata.st_dev != expected_device
            ):
                raise HostToolsBundleError("host-tools private stage metadata is unsafe")
            stage_fd = os.open(
                name,
                os.O_RDONLY
                | _require_os_flag("O_DIRECTORY")
                | _require_os_flag("O_CLOEXEC")
                | _require_no_follow(),
                dir_fd=host_fd,
            )
            opened = os.fstat(stage_fd)
            if (
                not _same_identity(opened, metadata)
                or not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != 0
                or opened.st_gid != 0
                or opened.st_nlink != 2
                or stat.S_IMODE(opened.st_mode) != STAGE_MODE
                or opened.st_dev != expected_device
            ):
                raise HostToolsBundleError("host-tools private stage identity changed")
            return name, stage_fd, metadata
        except BaseException:
            # ``mkdirat`` and the subsequent metadata assignment are not one
            # atomic Python operation.  If the identity was captured, remove
            # only that exact empty directory; otherwise leave it for an
            # operator quarantine rather than guessing at a raced pathname.
            _close_quietly(stage_fd, scope="private stage close")
            if metadata is not None:
                try:
                    _cleanup_owned_directory(
                        host_fd,
                        name,
                        metadata,
                        {},
                        expected_device=expected_device,
                    )
                except BaseException as cleanup_failure:
                    _record_cleanup_failure("private stage cleanup", cleanup_failure)
            raise
    raise HostToolsBundleError("host-tools private stage name collision")


def _write_stage_member(
    stage_fd: int,
    name: str,
    data: bytes,
    mode: int,
    *,
    expected_device: int,
    owned_members: dict[str, _OwnedFile] | None = None,
) -> os.stat_result:
    _safe_leaf(name, "host-tools member name")
    if (
        name not in HOST_TOOLS_INVENTORY
        or mode not in {EXECUTABLE_MODE, DATA_MODE}
        or not isinstance(data, bytes)
        or len(data) > MAX_FILE_BYTES
    ):
        raise HostToolsBundleError("host-tools stage member is not allowlisted")
    _require_install_primitives()
    descriptor: int | None = None
    created = False
    identity: os.stat_result | None = None
    completed = False
    try:
        descriptor = os.open(
            name,
            os.O_RDWR
            | os.O_CREAT
            | _require_os_flag("O_EXCL")
            | _require_os_flag("O_CLOEXEC")
            | _require_no_follow(),
            0o600,
            dir_fd=stage_fd,
        )
        created = True
        _write_all(descriptor, data)
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        identity = metadata
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
            or metadata.st_dev != expected_device
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size != len(data)
        ):
            raise HostToolsBundleError("host-tools staged member metadata is unsafe")
        if _sha256_bytes(os.pread(descriptor, metadata.st_size, 0)) != _sha256_bytes(data):
            raise HostToolsBundleError("host-tools staged member digest is invalid")
        pathname = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
        if (
            not _same_identity(pathname, metadata)
            or not stat.S_ISREG(pathname.st_mode)
            or pathname.st_uid != 0
            or pathname.st_gid != 0
            or stat.S_IMODE(pathname.st_mode) != mode
        ):
            raise HostToolsBundleError("host-tools staged member identity changed")
        owned = _owned_file(
            metadata,
            data=data,
            mode=mode,
            uid=0,
            gid=0,
        )
        if owned_members is not None:
            # Record ownership before returning to the caller.  This closes
            # the otherwise untracked gap between a successful helper return
            # and the caller's dictionary assignment.
            owned_members[name] = owned
        _injection_point("stage_member_after_write")
        completed = True
        return metadata
    except HostToolsBundleError:
        raise
    except OSError as exc:
        raise HostToolsBundleError("host-tools staged member could not be written") from exc
    except BaseException:
        # Signals and interpreter exits are deliberately allowed to retain
        # their original type.  The identity-scoped unlink below runs only
        # when the opened inode can be proven to be ours.
        raise
    finally:
        if descriptor is not None and created and identity is None:
            try:
                identity = os.fstat(descriptor)
            except BaseException as cleanup_failure:
                _record_cleanup_failure("stage member identity cleanup", cleanup_failure)
        _close_quietly(descriptor, scope="stage member close")
        if created and not completed and identity is not None:
            try:
                current = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
                if owned_members is not None and name in owned_members:
                    _remove_owned_file(stage_fd, name, owned_members[name])
                    owned_members.pop(name, None)
                elif _same_identity(current, identity):
                    os.unlink(name, dir_fd=stage_fd)
                    os.fsync(stage_fd)
            except BaseException as cleanup_failure:
                # An identity mismatch is an operator quarantine, never a
                # reason to recursively remove an unknown pathname.
                _record_cleanup_failure("stage member cleanup", cleanup_failure)


def _verify_generation_dir(
    generation: Path,
    *,
    expected_source_sha: str,
    expected_manifest_sha256: str,
    expected_capabilities_sha256: str,
    expected_bundle_sha256: str | None = None,
    expected_device: int | None = None,
) -> dict[str, object]:
    """Verify exact installed metadata and all 15 flat members."""

    _require_install_primitives()
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
        or (expected_device is not None and root_metadata.st_dev != expected_device)
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
            or (expected_device is not None and metadata.st_dev != expected_device)
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
            or (expected_device is not None and opened.st_dev != expected_device)
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
    child_identities: dict[str, os.stat_result | _OwnedFile],
    *,
    expected_device: int | None = None,
) -> bool:
    """Remove only the exact stage inode and its exact created children."""

    try:
        current = _lstat_at(parent_fd, name)
        if (
            current.st_dev != identity.st_dev
            or current.st_ino != identity.st_ino
            or not stat.S_ISDIR(current.st_mode)
            or current.st_nlink != identity.st_nlink
            or (expected_device is not None and current.st_dev != expected_device)
        ):
            return False
        stage_fd = os.open(
            name,
            os.O_RDONLY
            | _require_os_flag("O_DIRECTORY")
            | _require_os_flag("O_CLOEXEC")
            | _require_no_follow(),
            dir_fd=parent_fd,
        )
        try:
            if {entry for entry in os.listdir(stage_fd)} != set(child_identities):
                return False
            for child, child_identity in child_identities.items():
                current_child = _lstat_at(stage_fd, child)
                expected = (
                    child_identity.metadata
                    if isinstance(child_identity, _OwnedFile)
                    else child_identity
                )
                if (
                    not _same_identity(current_child, expected)
                    or not stat.S_ISREG(current_child.st_mode)
                    or (expected_device is not None and current_child.st_dev != expected_device)
                ):
                    return False
            for child in child_identities:
                child_identity = child_identities[child]
                if isinstance(child_identity, _OwnedFile):
                    _remove_owned_file(stage_fd, child, child_identity)
                else:
                    os.unlink(child, dir_fd=stage_fd)
            os.fsync(stage_fd)
        finally:
            try:
                os.close(stage_fd)
            except BaseException as exc:
                _record_cleanup_failure("owned stage close", exc)
        # Re-check before rmdir.  If the pathname was replaced, leave it for
        # an operator; never recursively delete an unowned object.
        current = _lstat_at(parent_fd, name)
        if current.st_dev != identity.st_dev or current.st_ino != identity.st_ino:
            return False
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    except BaseException as exc:
        _record_cleanup_failure("owned directory cleanup", exc)
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
    identity: os.stat_result | _OwnedFile | None,
) -> None:
    """Unlink one created file only while identity and bytes are unchanged."""

    if identity is None:
        return
    if isinstance(identity, _OwnedFile):
        owned = identity
        expected_identity = identity.metadata
    else:
        owned = None
        expected_identity = identity
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if owned is not None:
            descriptor = os.open(
                name,
                os.O_RDONLY | _require_os_flag("O_CLOEXEC") | _require_no_follow(),
                dir_fd=parent_fd,
            )
            try:
                if not _owned_file_matches(current, owned, descriptor=descriptor):
                    return
            finally:
                _close_quietly(descriptor)
        elif not _same_identity(current, expected_identity):
            return
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException as cleanup_failure:
        _record_cleanup_failure("owned file cleanup", cleanup_failure)


def _open_secure_handoff_directory(path: Path, *, host_tools_root: Path | None = None) -> tuple[int, os.stat_result]:
    """Open the closed, root-owned handoff directory used for evidence.

    Evidence is a handoff artifact, never a generation member.  The lexical
    containment check is paired with a no-follow descriptor walk so a
    symlink/race cannot redirect it under ``host_tools_root``.
    """

    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise HostToolsBundleError("host-tools evidence directory path is invalid")
    if host_tools_root is not None:
        root = host_tools_root if host_tools_root.is_absolute() else host_tools_root.absolute()
        if path == root or root in path.parents:
            raise HostToolsBundleError("host-tools evidence must not be under host-tools root")
    descriptor, metadata = _open_no_symlink_directory(path, require_root_owned=True)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink < 2
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        _close_quietly(descriptor)
        raise HostToolsBundleError("host-tools evidence directory metadata is unsafe")
    return descriptor, metadata


def _validate_evidence_output(path: Path, host_tools_root: Path) -> None:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise HostToolsBundleError("host-tools evidence output path is invalid")
    _safe_leaf(path.name, "host-tools evidence filename")
    parent_fd, _ = _open_secure_handoff_directory(path.parent, host_tools_root=host_tools_root)
    _close_quietly(parent_fd)


def _evidence_exists(path: Path, host_tools_root: Path) -> bool:
    """Inspect the receipt name without following or changing it."""

    parent_fd, _ = _open_secure_handoff_directory(
        path.parent, host_tools_root=host_tools_root
    )
    try:
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise HostToolsBundleError(
                "host-tools install evidence cannot be inspected"
            ) from exc
        return True
    finally:
        _close_quietly(parent_fd, scope="evidence inspect parent close")


def _write_evidence(
    path: Path,
    payload: dict[str, object],
    *,
    host_tools_root: Path,
) -> _OwnedFile:
    encoded = _canonical_json(payload)
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise HostToolsBundleError("host-tools install evidence exceeds its bound")
    _require_install_primitives()
    _safe_leaf(path.name, "host-tools evidence filename")
    parent_fd, parent_metadata = _open_secure_handoff_directory(
        path.parent, host_tools_root=host_tools_root
    )
    descriptor: int | None = None
    temporary_identity: os.stat_result | None = None
    written_owned: _OwnedFile | None = None
    published = False
    owner_uid = 0
    owner_gid = 0

    def adopt_existing() -> _OwnedFile:
        """Adopt an exact prior receipt during an idempotent retry."""

        existing_descriptor: int | None = None
        try:
            existing_descriptor = os.open(
                path.name,
                os.O_RDONLY
                | _require_os_flag("O_CLOEXEC")
                | _require_no_follow(),
                dir_fd=parent_fd,
            )
            existing = os.fstat(existing_descriptor)
            if (
                not stat.S_ISREG(existing.st_mode)
                or existing.st_dev != parent_metadata.st_dev
                or existing.st_uid != owner_uid
                or existing.st_gid != owner_gid
                or existing.st_nlink != 1
                or stat.S_IMODE(existing.st_mode) != EVIDENCE_MODE
                or existing.st_size != len(encoded)
                or _read_fd_digest(existing_descriptor, existing.st_size)
                != _sha256_bytes(encoded)
            ):
                raise HostToolsBundleError("host-tools install evidence already exists")
            return _owned_file(
                existing,
                data=encoded,
                mode=EVIDENCE_MODE,
                uid=owner_uid,
                gid=owner_gid,
            )
        except FileNotFoundError as exc:
            raise HostToolsBundleError("host-tools install evidence disappeared") from exc
        except OSError as exc:
            raise HostToolsBundleError("host-tools install evidence cannot be opened") from exc
        finally:
            _close_quietly(existing_descriptor)

    try:
        owner_uid, owner_gid = 0, 0
        try:
            descriptor = os.open(
                ".",
                os.O_RDWR
                | _require_os_flag("O_TMPFILE")
                | _require_os_flag("O_CLOEXEC")
                | _require_no_follow(),
                EVIDENCE_MODE,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            # A destination may have been linked by an earlier interrupted
            # attempt.  Only an exact root-owned receipt is adoptable; every
            # mismatch remains a fail-closed operator condition.
            if exc.errno != errno.EEXIST:
                raise HostToolsBundleError(
                    "host-tools install evidence temporary file could not be opened"
                ) from exc
            written_owned = adopt_existing()
            published = True
            _injection_point("evidence_existing_adopt")
            os.fsync(parent_fd)
        else:
            os.fchown(descriptor, owner_uid, owner_gid)
            os.fchmod(descriptor, EVIDENCE_MODE)
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
            temporary_identity = os.fstat(descriptor)
            if (
                not stat.S_ISREG(temporary_identity.st_mode)
                or temporary_identity.st_dev != parent_metadata.st_dev
                or temporary_identity.st_nlink not in {0, 1}
                or temporary_identity.st_uid != owner_uid
                or temporary_identity.st_gid != owner_gid
                or stat.S_IMODE(temporary_identity.st_mode) != EVIDENCE_MODE
                or temporary_identity.st_size != len(encoded)
                or _read_fd_digest(descriptor, temporary_identity.st_size)
                != _sha256_bytes(encoded)
            ):
                raise HostToolsBundleError("host-tools install evidence metadata is unsafe")
            _injection_point("evidence_after_write")
            try:
                _link_tmpfile_noreplace(descriptor, parent_fd, path.name)
            except HostToolsBundleError as exc:
                if str(exc) != "host-tools output already exists":
                    raise
                _close_quietly(descriptor)
                descriptor = None
                written_owned = adopt_existing()
                published = True
                _injection_point("evidence_existing_adopt")
            if not published:
                published_identity = _reconcile_linked_file(
                    parent_fd,
                    path.name,
                    descriptor,
                    temporary_identity,
                    expected_device=parent_metadata.st_dev,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                    mode=EVIDENCE_MODE,
                    size=temporary_identity.st_size,
                    digest=_sha256_bytes(encoded),
                )
                if published_identity is None:
                    raise HostToolsBundleError("host-tools install evidence identity changed")
                written_owned = _owned_file(
                    published_identity,
                    data=encoded,
                    mode=EVIDENCE_MODE,
                    uid=owner_uid,
                    gid=owner_gid,
                )
                published = True
                _injection_point("evidence_after_link")
    except HostToolsBundleError:
        raise
    except OSError as exc:
        raise HostToolsBundleError("host-tools install evidence could not be written") from exc
    except BaseException:
        raise
    finally:
        active_failure = sys.exc_info()[1]
        if (
            active_failure is not None
            and not published
            and descriptor is not None
            and temporary_identity is not None
        ):
            try:
                reconciled = _reconcile_linked_file(
                    parent_fd,
                    path.name,
                    descriptor,
                    temporary_identity,
                    expected_device=parent_metadata.st_dev,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                    mode=EVIDENCE_MODE,
                    size=temporary_identity.st_size,
                    digest=_sha256_bytes(encoded),
                )
            except BaseException as cleanup_failure:
                _record_cleanup_failure("evidence reconciliation", cleanup_failure)
                reconciled = None
            if reconciled is not None:
                written_owned = _owned_file(
                    reconciled,
                    data=encoded,
                    mode=EVIDENCE_MODE,
                    uid=owner_uid,
                    gid=owner_gid,
                )
                published = True
        _close_quietly(descriptor)
        if active_failure is not None:
            # Evidence is the final commit marker.  If linkat completed before
            # an interrupt, retain the exact marker and let an idempotent retry
            # adopt it; a foreign/mismatched pathname is never removed.
            _close_quietly(parent_fd, scope="evidence parent close")
    try:
        _injection_point("evidence_before_parent_fsync")
        os.fsync(parent_fd)
        _injection_point("evidence_after_parent_fsync")
    except BaseException as exc:
        _close_quietly(parent_fd)
        if not isinstance(exc, OSError):
            raise
        raise HostToolsBundleError("host-tools install evidence parent could not be synced") from exc
    _close_quietly(parent_fd)
    if written_owned is None:
        raise HostToolsBundleError("host-tools install evidence identity is unavailable")
    return written_owned


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
    artifact_name: str,
    trusted_source_sha: str,
    tested_merge_sha: str,
    security_run_id: str,
    security_run_attempt: str,
    expected_receipt_sha256: str,
    evidence_output: Path | None = None,
) -> dict[str, object]:
    """Install one verified generation from an outer artifact envelope.

    This is an operator/host-image helper.  It never mutates release
    pointers, databases, systemd units, ``current`` or ``previous``.
    """

    _require_install_primitives()
    if os.geteuid() != 0 or os.getuid() != 0:
        raise HostToolsBundleError("host-tools installer requires root")
    source_sha = _safe_generation_sha(expected_source_sha)
    if evidence_output is not None:
        _validate_evidence_output(evidence_output, host_tools_root)
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
        artifact_name=artifact_name,
        trusted_source_sha=trusted_source_sha,
        tested_merge_sha=tested_merge_sha,
        security_run_id=security_run_id,
        security_run_attempt=security_run_attempt,
        expected_receipt_sha256=expected_receipt_sha256,
    )
    lock_parent_fd: int | None = None
    lock_fd: int | None = None
    host_fd: int | None = None
    host_metadata: os.stat_result | None = None
    stage_name: str | None = None
    stage_identity: os.stat_result | None = None
    stage_fd: int | None = None
    child_identities: dict[str, os.stat_result | _OwnedFile] = {}
    target_identity: os.stat_result | None = None
    try:
        # Hold the root-owned lock from generation/receipt inspection through
        # the final evidence parent fsync.  This serializes two cooperating
        # installers and makes EEXIST a deterministic exact-winner rescan,
        # never an overwrite opportunity.
        lock_parent_fd, lock_fd = _open_install_lock(host_tools_root)
        host_fd, host_metadata = _open_host_tools_root(host_tools_root)
        if evidence_output is not None:
            _validate_evidence_output(evidence_output, host_tools_root)
        target_name = source_sha
        try:
            existing_target = os.stat(target_name, dir_fd=host_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing_target = None
        except OSError as exc:
            raise HostToolsBundleError("host-tools generation target is unavailable") from exc
        if existing_target is None:
            # A receipt without its exact generation is an orphaned commit
            # marker.  Never let a fresh install adopt or overwrite it.
            if evidence_output is not None and _evidence_exists(
                evidence_output, host_tools_root
            ):
                raise HostToolsBundleError(
                    "host-tools install evidence exists without its generation"
                )
            stage_name, stage_fd, stage_identity = _new_stage(
                host_fd, source_sha, expected_device=host_metadata.st_dev
            )
            members = outer_summary["members"]
            if not isinstance(members, dict):
                raise HostToolsBundleError("host-tools bundle inventory is invalid")
            for name in HOST_TOOL_FILES:
                _write_stage_member(
                    stage_fd,
                    name,
                    bytes(members[name]),
                    EXECUTABLE_MODE,
                    expected_device=host_metadata.st_dev,
                    owned_members=child_identities,
                )
            _write_stage_member(
                stage_fd,
                "capabilities.txt",
                bytes(members["capabilities.txt"]),
                DATA_MODE,
                expected_device=host_metadata.st_dev,
                owned_members=child_identities,
            )
            _write_stage_member(
                stage_fd,
                "manifest.json",
                bytes(members["manifest.json"]),
                DATA_MODE,
                expected_device=host_metadata.st_dev,
                owned_members=child_identities,
            )
            if set(os.listdir(stage_fd)) != HOST_TOOLS_INVENTORY:
                raise HostToolsBundleError("host-tools private stage inventory is not closed")
            os.fchown(stage_fd, 0, 0)
            os.fchmod(stage_fd, HOST_GENERATION_MODE)
            os.fsync(stage_fd)
            if _reconcile_renamed_directory(
                host_fd,
                stage_name,
                stage_identity,
                expected_device=host_metadata.st_dev,
            ) is None:
                raise HostToolsBundleError("host-tools private stage identity changed")
            os.fsync(host_fd)
            _rename_noreplace(host_fd, stage_name, target_name, stage_identity)
            target_identity = _reconcile_renamed_directory(
                host_fd,
                target_name,
                stage_identity,
                expected_device=host_metadata.st_dev,
            )
            if target_identity is None:
                raise HostToolsBundleError(
                    "host-tools generation publication identity changed"
                )
            # The name is now the public generation; cleanup must never treat
            # it as a private stage even if evidence/self-tests fail.
            stage_name = None
            os.fsync(host_fd)
        else:
            # Existing target adoption is allowed only through the complete
            # verification below.  A foreign generation, symlink, or partial
            # inventory fails closed without touching it.
            target_identity = existing_target

        installed = _verify_generation_dir(
            host_tools_root / target_name,
            expected_source_sha=source_sha,
            expected_manifest_sha256=manifest_sha,
            expected_capabilities_sha256=capabilities_sha,
            expected_bundle_sha256=inner_sha,
            expected_device=host_metadata.st_dev,
        )
        if target_identity is None or _reconcile_renamed_directory(
            host_fd,
            target_name,
            target_identity,
            expected_device=host_metadata.st_dev,
        ) is None:
            raise HostToolsBundleError("host-tools generation identity changed")
        self_tests = _run_post_install_self_tests(
            host_tools_root / target_name,
            source_sha=source_sha,
            manifest_sha256=manifest_sha,
            capabilities_sha256=capabilities_sha,
        )
        # Repeat inventory, byte, mode, owner and no-extra-member checks after
        # both self-tests.  This is the retry proof as well as the fresh
        # publication proof.
        if target_identity is None or _reconcile_renamed_directory(
            host_fd,
            target_name,
            target_identity,
            expected_device=host_metadata.st_dev,
        ) is None:
            raise HostToolsBundleError("host-tools generation identity changed")
        installed = _verify_generation_dir(
            host_tools_root / target_name,
            expected_source_sha=source_sha,
            expected_manifest_sha256=manifest_sha,
            expected_capabilities_sha256=capabilities_sha,
            expected_bundle_sha256=inner_sha,
            expected_device=host_metadata.st_dev,
        )
        evidence = {
            "schema": PROVENANCE_SCHEMA,
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
            _write_evidence(evidence_output, evidence, host_tools_root=host_tools_root)
            _injection_point("install_after_evidence_write")
        print(
            f"HOST_TOOLS_INSTALL schema={PROVENANCE_SCHEMA} status=installed "
            f"artifact_id={artifact_id} source_sha={source_sha} outer_sha256={outer_sha} inner_sha256={inner_sha} "
            f"manifest_sha256={manifest_sha} capabilities_sha256={capabilities_sha}"
        )
        return evidence
    except BaseException:
        try:
            # A successful rename may have happened before Python observed it
            # (including an injected KeyboardInterrupt).  Reconcile only the
            # exact inode; a foreign target/stage is retained untouched.
            if host_fd is not None and stage_name is not None and stage_identity is not None:
                reconciled = _reconcile_renamed_directory(
                    host_fd,
                    source_sha,
                    stage_identity,
                    expected_device=host_metadata.st_dev,
                )
                if reconciled is not None:
                    target_identity = reconciled
                    stage_name = None
            if host_fd is not None and stage_name is not None and stage_identity is not None:
                _cleanup_owned_directory(
                    host_fd,
                    stage_name,
                    stage_identity,
                    child_identities,
                    expected_device=host_metadata.st_dev,
                )
        except BaseException as cleanup_failure:
            _record_cleanup_failure("installer reconciliation", cleanup_failure)
        raise
    finally:
        _close_quietly(stage_fd, scope="installer stage close")
        _close_quietly(host_fd, scope="installer root close")
        _close_quietly(lock_fd, scope="installer lock close")
        _close_quietly(lock_parent_fd, scope="installer lock parent close")


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
    if (
        not isinstance(output_dir, Path)
        or not output_dir.is_absolute()
        or ".." in output_dir.parts
        or output_dir == Path("/")
    ):
        raise HostToolsBundleError("host-tools contract directory path is invalid")
    _require_install_primitives()
    records = manifest["files"]
    if not isinstance(records, list):
        raise HostToolsBundleError("host-tools file summary is invalid")
    parent_fd, parent_metadata = _open_no_symlink_directory(output_dir.parent)
    contract_fd: int | None = None
    contract_metadata: os.stat_result | None = None
    created_files: dict[str, os.stat_result] = {}
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
    try:
        try:
            contract_fd = os.open(
                output_dir.name,
                os.O_RDONLY
                | _require_os_flag("O_DIRECTORY")
                | _require_os_flag("O_CLOEXEC")
                | _require_no_follow(),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            os.mkdir(output_dir.name, 0o700, dir_fd=parent_fd)
            contract_fd = os.open(
                output_dir.name,
                os.O_RDONLY
                | _require_os_flag("O_DIRECTORY")
                | _require_os_flag("O_CLOEXEC")
                | _require_no_follow(),
                dir_fd=parent_fd,
            )
        contract_metadata = os.fstat(contract_fd)
        if (
            not stat.S_ISDIR(contract_metadata.st_mode)
            or contract_metadata.st_dev != parent_metadata.st_dev
            or contract_metadata.st_nlink < 2
            or contract_metadata.st_uid != os.getuid()
            or contract_metadata.st_gid != os.getgid()
            or stat.S_IMODE(contract_metadata.st_mode) & 0o077
        ):
            raise HostToolsBundleError("host-tools contract directory metadata is unsafe")
        for name, value in values.items():
            created_files[name] = _write_exclusive_file_at(
                contract_fd,
                name,
                value.encode("ascii"),
                0o600,
                expected_device=contract_metadata.st_dev,
                owner_uid=os.getuid(),
                owner_gid=os.getgid(),
                description="host-tools contract file",
            )
        os.fsync(contract_fd)
        os.fsync(parent_fd)
    except BaseException:
        for name, identity in created_files.items():
            _remove_owned_file(contract_fd, name, identity)
        _close_quietly(contract_fd)
        _close_quietly(parent_fd)
        # The directory itself is task-owned when created here.  It is left
        # in place as a secure quarantine if identity cannot be proven.
        raise
    finally:
        _close_quietly(contract_fd)
        _close_quietly(parent_fd)


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
    outer.add_argument("--contract-dir")
    extract = subparsers.add_parser("extract-outer")
    extract.add_argument("--outer-bundle", required=True)
    extract.add_argument("--output", required=True)
    extract.add_argument("--expected-outer-sha256", required=True)
    extract.add_argument("--expected-inner-sha256", required=True)
    release = subparsers.add_parser("extract-release-artifact")
    release.add_argument("--archive", required=True)
    release.add_argument("--output-dir", required=True)
    release.add_argument("--release-slug", required=True)
    release.add_argument("--expected-archive-sha256", required=True)
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
    install.add_argument("--artifact-name", required=True)
    install.add_argument("--trusted-source-sha", required=True)
    install.add_argument("--tested-merge-sha", required=True)
    install.add_argument("--security-run-id", required=True)
    install.add_argument("--security-run-attempt", required=True)
    install.add_argument("--expected-receipt-sha256", required=True)
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
            if arguments.contract_dir is not None:
                summary = verify_outer_contract(
                    Path(arguments.outer_bundle),
                    Path(arguments.contract_dir),
                    expected_outer_sha256=arguments.expected_outer_sha256,
                    expected_inner_sha256=arguments.expected_inner_sha256,
                    expected_source_sha=arguments.expected_source_sha,
                    expected_manifest_sha256=arguments.expected_manifest_sha256,
                    expected_capabilities_sha256=arguments.expected_capabilities_sha256,
                )
            else:
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
        if arguments.command == "extract-release-artifact":
            summary = extract_release_artifact(
                Path(arguments.archive),
                Path(arguments.output_dir),
                release_slug=arguments.release_slug,
                expected_archive_sha256=arguments.expected_archive_sha256,
            )
            print(
                "RELEASE_ARTIFACT schema=1 status=extracted "
                f"release_slug={summary['release_slug']} "
                f"archive={summary['archive']} checksum={summary['checksum']} "
                f"provenance={summary['provenance']} archive_sha256={summary['archive_sha256']}"
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
                artifact_id=arguments.artifact_id,
                attestation_evidence=Path(arguments.attestation_evidence),
                source_head_sha=arguments.source_head_sha,
                packaging_commit=arguments.packaging_commit,
                artifact_name=arguments.artifact_name,
                trusted_source_sha=arguments.trusted_source_sha,
                tested_merge_sha=arguments.tested_merge_sha,
                security_run_id=arguments.security_run_id,
                security_run_attempt=arguments.security_run_attempt,
                expected_receipt_sha256=arguments.expected_receipt_sha256,
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
