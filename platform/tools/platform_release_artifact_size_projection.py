#!/usr/bin/env python3
"""Write a closed, evidence-only size projection for a validated release.

The projection measures the exact builder archive and the full/bootstrap
release trees on the temporary CI filesystem. It is not a production install
peak estimate and does not grant deployment authority.
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
from typing import Any


SCHEMA = 1
EVIDENCE_TYPE = "release_artifact_size_projection_v1"
MAX_OUTPUT_BYTES = 8192
MAX_CHECKSUM_BYTES = 256
MAX_TREE_ENTRIES = 200_000
MAX_TREE_ALLOCATED_BYTES = 8 * 1024 * 1024 * 1024
MAX_TREE_REGULAR_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA64 = re.compile(r"[0-9a-f]{64}\Z")
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
DECIMAL = re.compile(r"[1-9][0-9]{0,31}\Z")
EVENT_TYPES = frozenset({"pull_request", "push", "workflow_dispatch"})
EVENT_RESULT_FIELDS = frozenset(
    {
        "event_type",
        "workflow_run_id",
        "workflow_run_attempt",
        "tested_sha",
        "tested_tree_sha",
        "pr_head_sha",
        "pr_base_sha",
        "pr_base_ref",
        "pr_same_repository",
    }
)
FAILURE_REASONS = frozenset(
    {
        "arguments",
        "binding",
        "metadata",
        "checksum",
        "validator",
        "timeout",
        "filesystem",
        "projection",
    }
)


class ProjectionError(ValueError):
    """A closed size projection could not be safely produced."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in FAILURE_REASONS else "projection"
        super().__init__(self.reason)


def _stable_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _stable_directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Bind a directory object while allowing its expected child entries to change."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
    )


def _directory(path: Path, *, private: bool = False) -> os.stat_result:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectionError("metadata") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        resolved != path
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or mode & 0o022
        or (private and mode != 0o700)
    ):
        raise ProjectionError("metadata")
    return metadata


def _regular_file(path: Path, *, max_bytes: int) -> tuple[int, os.stat_result]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ProjectionError("metadata") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or before.st_gid != 0
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o022
        or before.st_size < 0
        or before.st_size > max_bytes
    ):
        raise ProjectionError("metadata")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise ProjectionError("metadata") from exc
    if _stable_identity(opened) != _stable_identity(before):
        os.close(descriptor)
        raise ProjectionError("metadata")
    return descriptor, opened


def _read_small_file(path: Path, *, max_bytes: int) -> tuple[bytes, os.stat_result]:
    descriptor, opened = _regular_file(path, max_bytes=max_bytes)
    try:
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            current = path.lstat()
        except OSError as exc:
            raise ProjectionError("metadata") from exc
        if (
            len(payload) > max_bytes
            or len(payload) != opened.st_size
            or _stable_identity(opened) != _stable_identity(after)
            or _stable_identity(opened) != _stable_identity(current)
        ):
            raise ProjectionError("metadata")
        return payload, opened
    finally:
        os.close(descriptor)


def _write_projection(path: Path, payload: bytes, *, root: Path) -> None:
    if len(payload) > MAX_OUTPUT_BYTES or path != root / "size-projection.json":
        raise ProjectionError("projection")
    descriptor, opened = _regular_file(path, max_bytes=0)
    try:
        if stat.S_IMODE(opened.st_mode) != 0o600:
            raise ProjectionError("metadata")
        flags = (
            os.O_WRONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        os.close(descriptor)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if _stable_identity(metadata) != _stable_identity(opened) or metadata.st_size != 0:
            raise ProjectionError("metadata")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ProjectionError("projection")
            view = view[written:]
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            after.st_size != len(payload)
            or _stable_identity(after)[:6] != _stable_identity(opened)[:6]
            or _stable_identity(current) != _stable_identity(after)
        ):
            raise ProjectionError("metadata")
    except OSError as exc:
        raise ProjectionError("projection") from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _free_bytes(path: Path) -> int:
    try:
        result = os.statvfs(path)
    except OSError as exc:
        raise ProjectionError("filesystem") from exc
    value = result.f_bavail * result.f_frsize
    if type(value) is not int or value < 0:
        raise ProjectionError("filesystem")
    return value


def _tree_usage(root: Path) -> dict[str, int]:
    _directory(root)
    stack = [root]
    entries = 0
    allocated = 0
    regular_bytes = 0
    while stack:
        directory = stack.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ProjectionError("metadata") from exc
        for child in children:
            entries += 1
            if entries > MAX_TREE_ENTRIES:
                raise ProjectionError("projection")
            path = Path(child.path)
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise ProjectionError("metadata") from exc
            if metadata.st_uid != 0 or metadata.st_gid != 0:
                raise ProjectionError("metadata")
            allocated += getattr(metadata, "st_blocks", 0) * 512
            if allocated > MAX_TREE_ALLOCATED_BYTES:
                raise ProjectionError("projection")
            if stat.S_ISDIR(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) & 0o022:
                    raise ProjectionError("metadata")
                stack.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                if (
                    metadata.st_nlink != 1
                    or metadata.st_size < 0
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                ):
                    raise ProjectionError("metadata")
                regular_bytes += metadata.st_size
                if regular_bytes > MAX_TREE_REGULAR_BYTES:
                    raise ProjectionError("projection")
            elif stat.S_ISLNK(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise ProjectionError("metadata")
                try:
                    link = os.readlink(path)
                except OSError as exc:
                    raise ProjectionError("metadata") from exc
                if not link or link.startswith("/") or "\\" in link:
                    raise ProjectionError("metadata")
                resolved = list(path.relative_to(root).parent.parts)
                for component in link.split("/"):
                    if component in {"", "."}:
                        raise ProjectionError("metadata")
                    if component == "..":
                        if not resolved:
                            raise ProjectionError("metadata")
                        resolved.pop()
                    else:
                        resolved.append(component)
            else:
                raise ProjectionError("metadata")
    return {"regular_bytes": regular_bytes, "allocated_bytes": allocated}


def _git_value(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(root / "source"), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="ascii",
            timeout=10,
            env={
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProjectionError("binding") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or not value or len(value) > 512:
        raise ProjectionError("binding")
    return value


def _event_binding(
    *,
    event_type: str,
    repository: str,
    tested_sha: str,
    run_id: str,
    run_attempt: str,
    pr_head_sha: str | None,
    pr_base_sha: str | None,
    pr_base_ref: str | None,
    pr_head_repository: str | None,
    pr_base_repository: str | None,
    git_root: Path,
) -> dict[str, Any]:
    if (
        event_type not in EVENT_TYPES
        or not REPOSITORY.fullmatch(repository)
        or not SHA40.fullmatch(tested_sha)
        or not DECIMAL.fullmatch(run_id)
        or not DECIMAL.fullmatch(run_attempt)
    ):
        raise ProjectionError("arguments")
    if event_type == "pull_request":
        if (
            pr_base_ref != "dev"
            or not isinstance(pr_base_sha, str)
            or not SHA40.fullmatch(pr_base_sha)
            or not isinstance(pr_head_sha, str)
            or not SHA40.fullmatch(pr_head_sha)
            or not isinstance(pr_head_repository, str)
            or not REPOSITORY.fullmatch(pr_head_repository)
            or not isinstance(pr_base_repository, str)
            or not REPOSITORY.fullmatch(pr_base_repository)
            or pr_head_repository != repository
            or pr_base_repository != repository
        ):
            raise ProjectionError("binding")
        parents = _git_value(git_root, "rev-list", "--parents", "-n", "1", tested_sha).split()
        if parents != [tested_sha, pr_base_sha, pr_head_sha]:
            raise ProjectionError("binding")
    elif any(
        value is not None
        for value in (
            pr_head_sha,
            pr_base_sha,
            pr_base_ref,
            pr_head_repository,
            pr_base_repository,
        )
    ):
        raise ProjectionError("binding")
    head = _git_value(git_root, "rev-parse", "--verify", "HEAD")
    tree = _git_value(git_root, "rev-parse", "--verify", f"{tested_sha}^{{tree}}")
    if head != tested_sha or not SHA40.fullmatch(tree):
        raise ProjectionError("binding")
    return {
        "event_type": event_type,
        "workflow_run_id": int(run_id),
        "workflow_run_attempt": int(run_attempt),
        "tested_sha": tested_sha,
        "tested_tree_sha": tree,
        "pr_head_sha": pr_head_sha,
        "pr_base_sha": pr_base_sha,
        "pr_base_ref": pr_base_ref,
        "pr_same_repository": True if event_type == "pull_request" else None,
    }


def _projection_document(
    *,
    source_sha: str,
    event: dict[str, Any],
    artifact_sha256: str,
    archive_file_bytes: int,
    archive_allocated_bytes: int,
    release_usage: dict[str, int],
    bootstrap_usage: dict[str, int],
    release_liveqa_usage: dict[str, int],
    bootstrap_liveqa_usage: dict[str, int],
    free_before: int,
    free_after: int,
) -> dict[str, Any]:
    measurements = [
        archive_file_bytes,
        archive_allocated_bytes,
        free_before,
        free_after,
        *release_usage.values(),
        *bootstrap_usage.values(),
        *release_liveqa_usage.values(),
        *bootstrap_liveqa_usage.values(),
    ]
    if any(type(value) is not int or value < 0 for value in measurements):
        raise ProjectionError("projection")
    expected_usage_fields = {"regular_bytes", "allocated_bytes"}
    if any(
        set(usage) != expected_usage_fields
        for usage in (
            release_usage,
            bootstrap_usage,
            release_liveqa_usage,
            bootstrap_liveqa_usage,
        )
    ):
        raise ProjectionError("projection")
    if (
        set(event) != EVENT_RESULT_FIELDS
        or event.get("event_type") not in EVENT_TYPES
        or type(event.get("workflow_run_id")) is not int
        or event["workflow_run_id"] < 1
        or type(event.get("workflow_run_attempt")) is not int
        or event["workflow_run_attempt"] < 1
        or not isinstance(event.get("tested_sha"), str)
        or not SHA40.fullmatch(event["tested_sha"])
        or not isinstance(event.get("tested_tree_sha"), str)
        or not SHA40.fullmatch(event["tested_tree_sha"])
        or source_sha != event["tested_sha"]
        or not SHA40.fullmatch(source_sha)
        or not SHA64.fullmatch(artifact_sha256)
    ):
        raise ProjectionError("projection")
    pr_fields = (event.get("pr_head_sha"), event.get("pr_base_sha"), event.get("pr_base_ref"))
    if event["event_type"] == "pull_request":
        if (
            not isinstance(pr_fields[0], str)
            or not SHA40.fullmatch(pr_fields[0])
            or not isinstance(pr_fields[1], str)
            or not SHA40.fullmatch(pr_fields[1])
            or pr_fields[2] != "dev"
            or event.get("pr_same_repository") is not True
        ):
            raise ProjectionError("projection")
    elif (
        any(value is not None for value in pr_fields)
        or event.get("pr_same_repository") is not None
    ):
        raise ProjectionError("projection")
    coexist = (
        archive_allocated_bytes
        + release_usage["allocated_bytes"]
        + bootstrap_usage["allocated_bytes"]
    )
    if coexist > MAX_TREE_ALLOCATED_BYTES:
        raise ProjectionError("projection")
    return {
        "schema": SCHEMA,
        "evidence_type": EVIDENCE_TYPE,
        "measurement_scope": "builder_snapshot",
        "evidence_only": True,
        "deployable": False,
        "source_sha": source_sha,
        **event,
        "artifact_sha256": artifact_sha256,
        "archive_file_bytes": archive_file_bytes,
        "archive_allocated_bytes": archive_allocated_bytes,
        "release_regular_bytes": release_usage["regular_bytes"],
        "release_allocated_bytes": release_usage["allocated_bytes"],
        "bootstrap_regular_bytes": bootstrap_usage["regular_bytes"],
        "bootstrap_allocated_bytes": bootstrap_usage["allocated_bytes"],
        "release_liveqa_regular_bytes": release_liveqa_usage["regular_bytes"],
        "release_liveqa_allocated_bytes": release_liveqa_usage["allocated_bytes"],
        "bootstrap_liveqa_regular_bytes": bootstrap_liveqa_usage["regular_bytes"],
        "bootstrap_liveqa_allocated_bytes": bootstrap_liveqa_usage["allocated_bytes"],
        "measurement_coexist_allocated_bytes": coexist,
        "free_before_bootstrap_bytes": free_before,
        "free_after_bootstrap_bytes": free_after,
    }


def project(
    *,
    measurement_root: Path,
    release_slug: str,
    source_sha: str,
    artifact_sha256: str,
    event_type: str,
    repository: str,
    tested_sha: str,
    run_id: str,
    run_attempt: str,
    pr_head_sha: str | None = None,
    pr_base_sha: str | None = None,
    pr_base_ref: str | None = None,
    pr_head_repository: str | None = None,
    pr_base_repository: str | None = None,
    output: Path | None = None,
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise ProjectionError("metadata")
    if (
        not SLUG.fullmatch(release_slug)
        or not SHA40.fullmatch(source_sha)
        or not SHA64.fullmatch(artifact_sha256)
        or source_sha != tested_sha
        or output is None
    ):
        raise ProjectionError("arguments")
    measurement_root = Path(os.path.abspath(measurement_root))
    _directory(measurement_root, private=True)
    event = _event_binding(
        event_type=event_type,
        repository=repository,
        tested_sha=tested_sha,
        run_id=run_id,
        run_attempt=run_attempt,
        pr_head_sha=pr_head_sha,
        pr_base_sha=pr_base_sha,
        pr_base_ref=pr_base_ref,
        pr_head_repository=pr_head_repository,
        pr_base_repository=pr_base_repository,
        git_root=measurement_root,
    )

    source_root = measurement_root / "source"
    _directory(source_root)
    release_output = source_root / "platform/dist/releases"
    current = source_root
    for part in PurePosixPath("platform/dist/releases").parts:
        current = current / part
        _directory(current)
    artifact = release_output / f"{release_slug}.tar.gz"
    checksum = release_output / f"{release_slug}.tar.gz.sha256"
    release_dir = release_output / release_slug
    archive_fd, archive_metadata = _regular_file(artifact, max_bytes=MAX_ARCHIVE_BYTES)
    try:
        archive_allocated = getattr(archive_metadata, "st_blocks", 0) * 512
        digest = hashlib.sha256()
        while chunk := os.read(archive_fd, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(archive_fd)
        if _stable_identity(after) != _stable_identity(archive_metadata):
            raise ProjectionError("metadata")
    finally:
        os.close(archive_fd)
    if digest.hexdigest() != artifact_sha256:
        raise ProjectionError("checksum")
    checksum_payload, checksum_metadata = _read_small_file(
        checksum, max_bytes=MAX_CHECKSUM_BYTES
    )
    expected_checksum = f"{artifact_sha256}  {artifact.name}\n".encode("ascii")
    if checksum_payload not in {expected_checksum, expected_checksum[:-1]}:
        raise ProjectionError("checksum")
    _directory(release_dir)
    release_identity = release_dir.lstat()

    bootstrap_root = measurement_root / "bootstrap"
    if os.path.lexists(bootstrap_root):
        raise ProjectionError("metadata")
    try:
        bootstrap_root.mkdir(mode=0o700)
    except OSError as exc:
        raise ProjectionError("metadata") from exc
    bootstrap_identity = _directory(bootstrap_root, private=True)
    free_before = _free_bytes(measurement_root)
    validator = Path(__file__).absolute().with_name("platform_validate_release_artifact.py")
    command = [
        "/usr/bin/python3",
        "-I",
        str(validator),
        "--artifact",
        str(artifact),
        "--checksum",
        str(checksum),
        "--release-slug",
        release_slug,
        "--expected-source-commit",
        source_sha,
        "--extract-bootstrap-to",
        str(bootstrap_root),
    ]
    try:
        validation = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProjectionError("timeout") from exc
    except OSError as exc:
        raise ProjectionError("validator") from exc
    if validation.returncode != 0:
        raise ProjectionError("validator")
    if (
        _stable_identity(artifact.lstat()) != _stable_identity(archive_metadata)
        or _stable_identity(checksum.lstat()) != _stable_identity(checksum_metadata)
        or _stable_identity(release_dir.lstat()) != _stable_identity(release_identity)
        or _stable_directory_identity(bootstrap_root.lstat())
        != _stable_directory_identity(bootstrap_identity)
    ):
        raise ProjectionError("metadata")

    release_usage = _tree_usage(release_dir)
    bootstrap_release = bootstrap_root / release_slug
    bootstrap_usage = _tree_usage(bootstrap_release)
    release_liveqa_usage = _tree_usage(release_dir / "liveqa-runtime")
    bootstrap_liveqa_usage = _tree_usage(bootstrap_release / "liveqa-runtime")
    free_after = _free_bytes(measurement_root)
    result = _projection_document(
        source_sha=source_sha,
        event=event,
        artifact_sha256=artifact_sha256,
        archive_file_bytes=archive_metadata.st_size,
        archive_allocated_bytes=archive_allocated,
        release_usage=release_usage,
        bootstrap_usage=bootstrap_usage,
        release_liveqa_usage=release_liveqa_usage,
        bootstrap_liveqa_usage=bootstrap_liveqa_usage,
        free_before=free_before,
        free_after=free_after,
    )
    encoded = (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    _write_projection(output, encoded, root=measurement_root)
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--measurement-root", required=True)
    parser.add_argument("--release-slug", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--event-type", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tested-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--pr-head-sha")
    parser.add_argument("--pr-base-sha")
    parser.add_argument("--pr-base-ref")
    parser.add_argument("--pr-head-repository")
    parser.add_argument("--pr-base-repository")
    parser.add_argument("--output", required=True)
    try:
        args = parser.parse_args(argv)
        project(
            measurement_root=Path(args.measurement_root),
            release_slug=args.release_slug,
            source_sha=args.source_sha,
            artifact_sha256=args.artifact_sha256,
            event_type=args.event_type,
            repository=args.repository,
            tested_sha=args.tested_sha,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            pr_head_sha=args.pr_head_sha,
            pr_base_sha=args.pr_base_sha,
            pr_base_ref=args.pr_base_ref,
            pr_head_repository=args.pr_head_repository,
            pr_base_repository=args.pr_base_repository,
            output=Path(args.output),
        )
    except (ProjectionError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        reason = exc.reason if isinstance(exc, ProjectionError) else "projection"
        print(f"RELEASE_ARTIFACT_SIZE_PROJECTION schema=1 status=failed reason={reason}")
        return 1
    print("RELEASE_ARTIFACT_SIZE_PROJECTION schema=1 status=complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
