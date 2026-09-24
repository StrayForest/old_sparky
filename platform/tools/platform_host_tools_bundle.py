#!/usr/bin/env python3
"""Build and verify the immutable production host-tools handoff.

This module is intentionally stdlib-only.  CI uses it on a secret-free
runner to create a deterministic archive for an operator/host-image
provisioning step.  The production workflow may verify the archive as data,
but it never executes an installer from it or copies it to the host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import zipfile


SCHEMA = 1
TOOLSET_VERSION = "production-host-tools-v1"
MAX_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024
MAX_FILE_COUNT = 32
MEMBER_ROOT = "platform-host-tools"
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
)
EXECUTABLE_MODE = 0o555
DATA_MODE = 0o444


class HostToolsBundleError(ValueError):
    """Bounded validation failure for the offline host-tools contract."""


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


def _read_source(path: Path) -> bytes:
    """Read one source member while rejecting replacement races."""

    before = _regular_source(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
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
    name = info.filename
    if (
        not name.startswith(f"{MEMBER_ROOT}/")
        or name.count("/") != 1
        or name.endswith("/")
        or "\\" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or info.is_dir()
        or info.compress_type != zipfile.ZIP_STORED
    ):
        raise HostToolsBundleError("host-tools archive member path is unsafe")
    mode = (info.external_attr >> 16) & 0o177777
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise HostToolsBundleError("host-tools archive member type is unsafe")
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


def verify_bundle(bundle: Path, expected_source_sha: str | None = None) -> dict[str, object]:
    """Verify a bundle without extracting or executing any member."""

    try:
        metadata = bundle.lstat()
    except OSError as exc:
        raise HostToolsBundleError("host-tools bundle is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_BUNDLE_BYTES
    ):
        raise HostToolsBundleError("host-tools bundle metadata is unsafe")

    try:
        with zipfile.ZipFile(bundle, mode="r", allowZip64=False) as archive:
            infos = archive.infolist()
            if len(infos) != len(HOST_TOOL_FILES) + 2 or len(infos) > MAX_FILE_COUNT + 1:
                raise HostToolsBundleError("host-tools archive member count is invalid")
            members: dict[str, bytes] = {}
            modes: dict[str, int] = {}
            total_uncompressed = 0
            for info in infos:
                name = _safe_member(info)
                if name in members:
                    raise HostToolsBundleError("host-tools archive contains duplicate members")
                if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
                    raise HostToolsBundleError("host-tools archive member exceeds its bound")
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_FILE_BYTES * MAX_FILE_COUNT:
                    raise HostToolsBundleError("host-tools archive exceeds its bound")
                data = archive.read(info)
                if len(data) > MAX_FILE_BYTES:
                    raise HostToolsBundleError("host-tools archive member exceeds its bound")
                members[name] = data
                modes[name] = (info.external_attr >> 16) & 0o7777
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        if isinstance(exc, HostToolsBundleError):
            raise
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
    records = {str(record["path"]): record for record in manifest["files"]}
    if members["capabilities.txt"] != _capabilities_text(str(manifest["source_sha"])):
        raise HostToolsBundleError("host-tools capabilities payload is invalid")
    for name, record in records.items():
        if _sha256_bytes(members[name]) != record["sha256"]:
            raise HostToolsBundleError("host-tools member digest is invalid")

    return {
        "manifest": manifest,
        "manifest_sha256": _sha256_bytes(members["manifest.json"]),
        "capabilities_sha256": _sha256_bytes(members["capabilities.txt"]),
        "bundle_sha256": _sha256_bytes(bundle.read_bytes()),
        "members": members,
    }


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HostToolsBundleError("host-tools manifest has duplicate keys")
        result[key] = value
    return result


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
            f"{record['mode']}  {record['path']}\n" for record in records
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
    try:
        metadata_stat = metadata_path.lstat()
        if (
            not stat.S_ISREG(metadata_stat.st_mode)
            or stat.S_ISLNK(metadata_stat.st_mode)
            or metadata_stat.st_nlink != 1
            or metadata_stat.st_size > MAX_FILE_BYTES
        ):
            raise HostToolsBundleError("host-tools artifact metadata is unsafe")
        payload = json.loads(
            metadata_path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HostToolsBundleError("host-tools artifact metadata is invalid") from exc
    if not isinstance(payload, dict):
        raise HostToolsBundleError("host-tools artifact metadata is invalid")
    if (
        type(payload.get("id")) is not int
        or payload.get("id") != int(artifact_id)
        or type(payload.get("name")) is not str
        or payload.get("name") != artifact_name
        or type(payload.get("expired")) is not bool
        or payload.get("expired") is not False
        or payload.get("digest") != artifact_digest
    ):
        raise HostToolsBundleError("host-tools artifact identity is invalid")
    workflow_run = payload.get("workflow_run")
    if not isinstance(workflow_run, dict):
        raise HostToolsBundleError("host-tools artifact workflow binding is invalid")
    if (
        type(workflow_run.get("id")) is not int
        or workflow_run.get("id") != int(run_id)
        or type(workflow_run.get("head_sha")) is not str
        or workflow_run.get("head_sha") != source_sha
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
    try:
        metadata_stat = metadata_path.lstat()
        if (
            not stat.S_ISREG(metadata_stat.st_mode)
            or stat.S_ISLNK(metadata_stat.st_mode)
            or metadata_stat.st_nlink != 1
            or metadata_stat.st_size > MAX_FILE_BYTES
        ):
            raise HostToolsBundleError("host-tools workflow attempt metadata is unsafe")
        payload = json.loads(
            metadata_path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HostToolsBundleError("host-tools workflow attempt metadata is invalid") from exc
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
        or payload.get("head_branch") != expected_branch
        or payload.get("event") != expected_event
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
    metadata = subparsers.add_parser("verify-artifact-metadata")
    metadata.add_argument("--metadata", required=True)
    metadata.add_argument("--artifact-id", required=True)
    metadata.add_argument("--artifact-name", required=True)
    metadata.add_argument("--run-id", required=True)
    metadata.add_argument("--run-attempt", required=True)
    metadata.add_argument("--source-sha", required=True)
    metadata.add_argument("--expected-branch", required=True)
    metadata.add_argument("--artifact-digest", required=True)
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
            )
            print("HOST_TOOLS_BUNDLE schema=1 artifact_metadata=verified")
        else:
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
