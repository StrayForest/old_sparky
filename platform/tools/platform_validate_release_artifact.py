#!/usr/bin/env python3
"""Verify and safely extract an immutable platform release artifact."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
from typing import BinaryIO


SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
RELEASE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
TIMESTAMP_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
WEB_BUILD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
PINNED_NODE_VERSION = "26.3.1"
PINNED_NPM_VERSION = "11.16.0"
MAX_RELEASE_JSON_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_MEMBERS = 200_000
MAX_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255

RELEASE_KEYS = {
    "artifact_format_version",
    "release_slug",
    "built_at_utc",
    "release_ref",
    "source_git_commit",
    "python_requirements_file",
    "python_lock_file",
    "python_freeze_file",
    "python_wheelhouse_dir",
    "python_wheelhouse_manifest_file",
    "web_package_lock_file",
    "web_build_id",
    "node_version",
    "npm_version",
    "runtime_layout",
}
RUNTIME_LAYOUT = {
    "app_dir": "/opt/oldsparky/platform",
    "current_symlink": "/opt/oldsparky/platform/current",
    "previous_symlink": "/opt/oldsparky/platform/previous",
    "shared_dir": "/opt/oldsparky/platform/shared",
    "shared_env_file": "/opt/oldsparky/platform/shared/.env.platform",
    "shared_venv_dir": "/opt/oldsparky/platform/shared/venv",
}


class ArtifactError(RuntimeError):
    """A safe release artifact validation refusal."""


def _open_regular_root_file(path: Path, *, label: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        current = path.lstat()
    except OSError as exc:
        try:
            os.close(descriptor)
        except UnboundLocalError:
            pass
        raise ArtifactError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)
    ):
        os.close(descriptor)
        raise ArtifactError(f"{label} metadata is unsafe")
    return descriptor, metadata


def _safe_root_directory(path: Path, *, label: str, writable: bool) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ArtifactError(f"{label} is unavailable") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or mode & 0o022
        or (writable and mode & 0o700 != 0o700)
    ):
        raise ArtifactError(f"{label} metadata is unsafe")
    return resolved


def _checksum_contract(artifact: Path, checksum: Path) -> str:
    expected_checksum = artifact.with_name(f"{artifact.name}.sha256")
    try:
        if checksum.resolve(strict=True) != expected_checksum.resolve(strict=True):
            raise ArtifactError("release checksum is not adjacent to the artifact")
        if artifact.parent.resolve(strict=True) != checksum.parent.resolve(strict=True):
            raise ArtifactError("release checksum is not adjacent to the artifact")
    except OSError as exc:
        raise ArtifactError("release artifact parent is unavailable") from exc
    _safe_root_directory(
        artifact.parent, label="release artifact parent", writable=False
    )

    artifact_fd, artifact_metadata = _open_regular_root_file(
        artifact, label="release artifact"
    )
    checksum_fd, checksum_metadata = _open_regular_root_file(
        checksum, label="release checksum"
    )
    try:
        if artifact_metadata.st_size > MAX_ARCHIVE_BYTES:
            raise ArtifactError("release artifact exceeds its size bound")
        if checksum_metadata.st_size > 256:
            raise ArtifactError("release checksum file is too large")
        with os.fdopen(checksum_fd, "rb", closefd=False) as checksum_file:
            raw = checksum_file.read(257)
        try:
            checksum_text = raw.decode("ascii")
        except UnicodeError as exc:
            raise ArtifactError("release checksum file is invalid") from exc
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)\n?", checksum_text)
        if match is None or match.group(2) != artifact.name:
            raise ArtifactError(
                "release checksum must contain one adjacent sha256sum record"
            )
        expected = match.group(1)
        digest = hashlib.sha256()
        while chunk := os.read(artifact_fd, 1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ArtifactError("release artifact checksum mismatch")
        return expected
    finally:
        os.close(artifact_fd)
        os.close(checksum_fd)


def _safe_member_path(name: str, *, release_slug: str) -> PurePosixPath:
    try:
        raw = name.encode("utf-8")
    except UnicodeError as exc:
        raise ArtifactError("release archive contains an invalid path") from exc
    components = name.split("/")
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or len(raw) > MAX_PATH_BYTES
        or any(
            part in {"", ".", ".."}
            or len(part.encode("utf-8")) > MAX_COMPONENT_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            for part in components
        )
        or components[0] != release_slug
    ):
        raise ArtifactError("release archive contains a non-canonical path")
    path = PurePosixPath(*components)
    if str(path) != name:
        raise ArtifactError("release archive contains a non-canonical path")
    return path


def _contained_link_target(
    member: tarfile.TarInfo, *, release_slug: str
) -> PurePosixPath:
    linkname = member.linkname
    try:
        raw = linkname.encode("utf-8")
    except UnicodeError as exc:
        raise ArtifactError("release archive contains an invalid symlink") from exc
    components = linkname.split("/")
    if (
        not linkname
        or linkname.startswith("/")
        or "\\" in linkname
        or len(raw) > MAX_PATH_BYTES
        or any(
            part in {"", "."}
            or len(part.encode("utf-8")) > MAX_COMPONENT_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            for part in components
        )
    ):
        raise ArtifactError("release archive contains a non-canonical symlink")

    resolved = list(PurePosixPath(member.name).parent.parts)
    for component in components:
        if component == "..":
            if len(resolved) <= 1:
                raise ArtifactError("release archive symlink escapes its top level")
            resolved.pop()
        else:
            resolved.append(component)
    if not resolved or resolved[0] != release_slug:
        raise ArtifactError("release archive symlink escapes its top level")
    return PurePosixPath(*resolved)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactError("RELEASE.json contains duplicate keys")
        result[key] = value
    return result


def _parse_release_json(raw: bytes, *, release_slug: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except ArtifactError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactError("RELEASE.json is invalid") from exc
    if not isinstance(parsed, dict) or set(parsed) != RELEASE_KEYS:
        raise ArtifactError("RELEASE.json schema is invalid")
    if (
        type(parsed["artifact_format_version"]) is not int
        or parsed["artifact_format_version"] != 1
    ):
        raise ArtifactError("RELEASE.json artifact format is unsupported")

    release_ref = parsed["release_ref"]
    built_at = parsed["built_at_utc"]
    if (
        not isinstance(release_ref, str)
        or RELEASE_REF_PATTERN.fullmatch(release_ref) is None
    ):
        raise ArtifactError("RELEASE.json release_ref is invalid")
    if not isinstance(built_at, str) or TIMESTAMP_PATTERN.fullmatch(built_at) is None:
        raise ArtifactError("RELEASE.json built_at_utc is invalid")
    try:
        datetime.strptime(built_at, "%Y%m%dT%H%M%SZ")  # noqa: DTZ007
    except ValueError as exc:
        raise ArtifactError("RELEASE.json built_at_utc is invalid") from exc
    if (
        parsed["release_slug"] != release_slug
        or release_slug != f"{release_ref}-{built_at}"
    ):
        raise ArtifactError("RELEASE.json release identity does not match the archive")

    commit = parsed["source_git_commit"]
    if not isinstance(commit, str) or COMMIT_PATTERN.fullmatch(commit) is None:
        raise ArtifactError("RELEASE.json source_git_commit is invalid")
    expected_paths = {
        "python_requirements_file": "requirements-platform.txt",
        "python_lock_file": "requirements-platform.lock.txt",
        "python_freeze_file": "requirements-platform.freeze.txt",
        "python_wheelhouse_dir": "wheelhouse",
        "python_wheelhouse_manifest_file": "wheelhouse/WHEELHOUSE.sha256",
        "web_package_lock_file": "apps/platform_web/package-lock.json",
    }
    if any(parsed[key] != value for key, value in expected_paths.items()):
        raise ArtifactError("RELEASE.json artifact paths are invalid")
    build_id = parsed["web_build_id"]
    if (
        not isinstance(build_id, str)
        or WEB_BUILD_ID_PATTERN.fullmatch(build_id) is None
    ):
        raise ArtifactError("RELEASE.json web_build_id is invalid")
    if parsed["node_version"] != PINNED_NODE_VERSION:
        raise ArtifactError("RELEASE.json Node runtime version is invalid")
    if parsed["npm_version"] != PINNED_NPM_VERSION:
        raise ArtifactError("RELEASE.json npm runtime version is invalid")
    if parsed["runtime_layout"] != RUNTIME_LAYOUT:
        raise ArtifactError("RELEASE.json runtime layout is invalid")
    return parsed


def _validate_member_mode(member: tarfile.TarInfo) -> None:
    mode = member.mode
    if mode < 0 or mode > 0o7777 or mode & 0o7000:
        raise ArtifactError("release archive contains unsafe permissions")
    if not member.issym() and mode & 0o022:
        raise ArtifactError("release archive contains unsafe permissions")
    if member.isdir() and mode & 0o500 != 0o500:
        raise ArtifactError("release archive contains an inaccessible directory")
    if member.isfile() and mode & 0o400 == 0:
        raise ArtifactError("release archive contains an unreadable file")
    if member.issym() and mode != 0o777:
        raise ArtifactError("release archive contains an unsafe symlink mode")


def _validate_structure(
    members: list[tarfile.TarInfo],
    *,
    release_slug: str,
) -> tuple[dict[str, tarfile.TarInfo], dict[str, PurePosixPath]]:
    by_name: dict[str, tarfile.TarInfo] = {}
    symlink_targets: dict[str, PurePosixPath] = {}
    expanded_bytes = 0
    for member in members:
        path = _safe_member_path(member.name, release_slug=release_slug)
        if member.name in by_name:
            raise ArtifactError("release archive contains duplicate paths")
        if not (member.isdir() or member.isfile() or member.issym()):
            raise ArtifactError("release archive contains a forbidden member type")
        if member.uid != 0 or member.gid != 0:
            raise ArtifactError("release archive contains unsafe ownership")
        if getattr(member, "sparse", None):
            raise ArtifactError("release archive contains a sparse member")
        _validate_member_mode(member)
        if member.isfile():
            if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                raise ArtifactError("release archive member exceeds its size bound")
            expanded_bytes += member.size
            if expanded_bytes > MAX_EXPANDED_BYTES:
                raise ArtifactError("release archive exceeds its expanded size bound")
        elif member.size != 0:
            raise ArtifactError("release archive contains invalid non-file data")
        if member.issym():
            symlink_targets[member.name] = _contained_link_target(
                member, release_slug=release_slug
            )
        by_name[member.name] = member

        if len(path.parts) > 1:
            parent = str(path.parent)
            parent_member = by_name.get(parent)
            if parent_member is not None and not parent_member.isdir():
                raise ArtifactError("release archive path traverses a non-directory")

    root = by_name.get(release_slug)
    if root is None or not root.isdir():
        raise ArtifactError("release archive is missing its top-level directory")
    for name, member in by_name.items():
        path = PurePosixPath(name)
        if name != release_slug:
            parent = by_name.get(str(path.parent))
            if parent is None or not parent.isdir():
                raise ArtifactError("release archive omits a parent directory")
        for depth in range(1, len(path.parts)):
            ancestor = by_name.get(str(PurePosixPath(*path.parts[:depth])))
            if ancestor is not None and ancestor.issym():
                raise ArtifactError("release archive path traverses a symlink")
        if member.issym() and str(symlink_targets[name]) == name:
            raise ArtifactError("release archive contains a self-referential symlink")
    for name, target in symlink_targets.items():
        visited = {name}
        current = str(target)
        while True:
            target_member = by_name.get(current)
            if target_member is None:
                raise ArtifactError("release archive contains a dangling symlink")
            if not target_member.issym():
                break
            if current in visited:
                raise ArtifactError("release archive contains a symlink cycle")
            visited.add(current)
            current = str(symlink_targets[current])
    return by_name, symlink_targets


def _required_members(
    by_name: dict[str, tarfile.TarInfo], *, release_slug: str
) -> None:
    required_files = (
        "RELEASE.json",
        ".env.platform.example",
        "requirements-platform.txt",
        "requirements-platform.lock.txt",
        "requirements-platform.freeze.txt",
        "wheelhouse/WHEELHOUSE.sha256",
        "apps/platform_web/package-lock.json",
        "apps/platform_web/.next/standalone/server.js",
    )
    required_directories = (
        "wheelhouse",
        "apps/platform_web/.next/standalone/.next/static",
    )
    for relative in required_files:
        member = by_name.get(f"{release_slug}/{relative}")
        if member is None or not member.isfile():
            raise ArtifactError(f"release archive is missing required file: {relative}")
    for relative in required_directories:
        member = by_name.get(f"{release_slug}/{relative}")
        if member is None or not member.isdir():
            raise ArtifactError(
                f"release archive is missing required directory: {relative}"
            )
    wheel_prefix = f"{release_slug}/wheelhouse/"
    if not any(
        name.startswith(wheel_prefix) and name.endswith(".whl") for name in by_name
    ):
        raise ArtifactError("release archive wheelhouse contains no wheels")
    rollback_prefix = f"{release_slug}/.rollback"
    if any(
        name == rollback_prefix or name.startswith(f"{rollback_prefix}/")
        for name in by_name
    ):
        raise ArtifactError("release archive contains reserved rollback state")


def _copy_regular_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    destination: Path,
) -> None:
    source: BinaryIO | None = archive.extractfile(member)
    if source is None:
        raise ArtifactError("release archive member data is unavailable")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as exc:
        raise ArtifactError("release archive destination is unsafe") from exc
    remaining = member.size
    try:
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ArtifactError("release archive member is truncated")
            view = memoryview(chunk)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ArtifactError("release archive member could not be written")
                view = view[written:]
            remaining -= len(chunk)
        if source.read(1):
            raise ArtifactError("release archive member exceeds its declared size")
        os.fchmod(descriptor, member.mode)
        os.fsync(descriptor)
    except OSError as exc:
        raise ArtifactError("release archive member could not be extracted") from exc
    finally:
        os.close(descriptor)
        source.close()


def _extract_validated_archive(
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    *,
    release_slug: str,
    extract_to: Path,
) -> None:
    destination_root = _safe_root_directory(
        extract_to, label="release extraction directory", writable=True
    )
    release_root = destination_root / release_slug
    if os.path.lexists(release_root):
        raise ArtifactError("release extraction target already exists")

    directories = sorted(
        (member for member in members if member.isdir()),
        key=lambda member: len(PurePosixPath(member.name).parts),
    )
    regular_files = sorted(member.name for member in members if member.isfile())
    symlinks = sorted(member.name for member in members if member.issym())
    by_name = {member.name: member for member in members}
    try:
        for member in directories:
            (destination_root / member.name).mkdir(mode=0o700)
        for name in regular_files:
            member = by_name[name]
            _copy_regular_member(archive, member, destination_root / name)
        # Symlinks are deliberately created after all directories and regular
        # files, so no extracted write can ever traverse an archive symlink.
        for name in symlinks:
            member = by_name[name]
            os.symlink(member.linkname, destination_root / name)
        for member in reversed(directories):
            os.chmod(destination_root / member.name, member.mode)  # nosec B103
    except (ArtifactError, OSError) as exc:
        if (
            release_root.exists()
            and release_root.is_dir()
            and not release_root.is_symlink()
        ):
            shutil.rmtree(release_root)
        if isinstance(exc, ArtifactError):
            raise
        raise ArtifactError("release archive extraction failed") from exc


def validate_archive(
    artifact: Path,
    *,
    release_slug: str,
    extract_to: Path | None = None,
) -> dict[str, object]:
    if SLUG_PATTERN.fullmatch(release_slug) is None:
        raise ArtifactError("release slug is invalid")
    try:
        with tarfile.open(artifact, mode="r:gz") as archive:
            members: list[tarfile.TarInfo] = []
            for member in archive:
                members.append(member)
                if len(members) > MAX_MEMBERS:
                    raise ArtifactError(
                        "release archive exceeds its member-count bound"
                    )
            by_name, _ = _validate_structure(members, release_slug=release_slug)
            _required_members(by_name, release_slug=release_slug)

            release_member = by_name[f"{release_slug}/RELEASE.json"]
            if release_member.size > MAX_RELEASE_JSON_BYTES:
                raise ArtifactError("RELEASE.json is too large")
            extracted = archive.extractfile(release_member)
            if extracted is None:
                raise ArtifactError("RELEASE.json is unavailable")
            raw = extracted.read(MAX_RELEASE_JSON_BYTES + 1)
            extracted.close()
            if len(raw) > MAX_RELEASE_JSON_BYTES:
                raise ArtifactError("RELEASE.json is too large")
            release_payload = _parse_release_json(raw, release_slug=release_slug)
            if extract_to is not None:
                _extract_validated_archive(
                    archive,
                    members,
                    release_slug=release_slug,
                    extract_to=extract_to,
                )
            return release_payload
    except ArtifactError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise ArtifactError("release archive is invalid") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and optionally extract an immutable platform release archive."
    )
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--checksum", type=Path, required=True)
    parser.add_argument("--release-slug", required=True)
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--extract-to", type=Path)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ArtifactError("release artifact validation requires root")
        _checksum_contract(args.artifact, args.checksum)
        release_payload = validate_archive(
            args.artifact,
            release_slug=args.release_slug,
            extract_to=args.extract_to,
        )
        if args.expected_source_commit is not None:
            if not COMMIT_PATTERN.fullmatch(args.expected_source_commit):
                raise ArtifactError("expected source commit is invalid")
            if release_payload.get("source_git_commit") != args.expected_source_commit:
                raise ArtifactError("release source commit does not match expected commit")
        action = "extracted" if args.extract_to is not None else "validated"
        print(
            f"Release artifact checksum, layout, and metadata are valid; {action} safely."
        )
        return 0
    except ArtifactError as exc:
        print(f"Release artifact refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
