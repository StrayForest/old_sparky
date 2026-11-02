#!/usr/bin/env python3
"""Verify exact GitHub run artifacts before local Codex companion activation."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import zipfile
from typing import Any


REPOSITORY = "StrayForest/old_sparky"
BUILD_WORKFLOW_NAME = "Platform Codex archive retention companion build"
BUILD_WORKFLOW_PATH = ".github/workflows/platform-codex-archive-retention-build.yml"
SECURITY_WORKFLOW_NAME = "Platform security and build"
SECURITY_WORKFLOW_PATH = ".github/workflows/platform-security.yml"
CODEX_WORKFLOW_PATH = ".github/workflows/platform-codex-archive-retention-build.yml"
CODEX_PACKAGE_PREFIX = "platform/tools/codex_archive_retention/"
CODEX_TEST_PATH = "platform/tests/test_platform_codex_archive_retention.py"
CANDIDATE_REASON = "trusted candidate-packaging change requires full verification and is non-deployable"
STANDARD_FULL_REASON = "platform or workflow change requires the full deterministic suite"
GATES = [
    "backend",
    "python-quality",
    "security",
    "migration",
    "docs",
    "web-quality",
    "web-hermetic",
    "verification-contract",
]
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 262_144_000
MAX_ROUTE_MANIFEST_BYTES = 1_048_576
MAX_ZIP_ENTRIES = 8
MINIMUM_FREE_BYTES = 5 * 1024**3
MINIMUM_FREE_PERCENT = 10
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[1-9][0-9]{0,31}\Z")


class VerificationError(ValueError):
    """An artifact or run identity failed its closed contract."""


def _fail(reason: str) -> VerificationError:
    return VerificationError(reason)


def _read_json(path: Path, *, maximum: int = MAX_JSON_BYTES) -> Any:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > maximum:
        raise _fail("input JSON file identity or size is invalid")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("input JSON file is malformed") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_identity(
    payload: Any,
    *,
    run_id: str,
    attempt: str,
    workflow_name: str,
    workflow_path: str,
    expected_head: str,
    pull_request: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _fail("workflow attempt response is malformed")
    repository = payload.get("repository")
    head_repository = payload.get("head_repository")
    if any(
        (
            type(payload.get("id")) is not int or str(payload["id"]) != run_id,
            type(payload.get("run_attempt")) is not int
            or str(payload["run_attempt"]) != attempt,
            payload.get("name") != workflow_name,
            payload.get("path") != workflow_path,
            payload.get("event") != "pull_request",
            payload.get("status") != "completed",
            payload.get("conclusion") != "success",
            payload.get("head_sha") != expected_head,
            not isinstance(repository, dict) or repository.get("full_name") != REPOSITORY,
            not isinstance(head_repository, dict)
            or head_repository.get("full_name") != REPOSITORY,
        )
    ):
        raise _fail("workflow attempt is not an exact successful same-repository run")
    pull_requests = payload.get("pull_requests")
    if not isinstance(pull_requests, list) or len(pull_requests) != 1:
        raise _fail("workflow attempt PR association is not singular")
    pr = pull_requests[0]
    if not isinstance(pr, dict):
        raise _fail("workflow attempt PR association is malformed")
    pr_head = pr.get("head")
    pr_base = pr.get("base")
    if not isinstance(pr_head, dict) or not isinstance(pr_base, dict):
        raise _fail("workflow attempt PR ref snapshot is malformed")
    if (
        str(pr.get("number")) != pull_request
        or pr_head.get("sha") != expected_head
        or pr_base.get("ref") != "dev"
    ):
        raise _fail("workflow attempt PR association does not match the reviewed source")
    return payload


def _artifact_record(
    payload: Any,
    *,
    name: str,
    run_id: str,
    run_attempt: str,
    head_sha: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _fail("artifact API response is malformed")
    rows = payload.get("artifacts")
    if (
        type(payload.get("total_count")) is not int
        or payload["total_count"] != 1
        or not isinstance(rows, list)
        or len(rows) != 1
    ):
        raise _fail("exact workflow artifact is missing or ambiguous")
    row = rows[0]
    if not isinstance(row, dict):
        raise _fail("workflow artifact metadata is malformed")
    workflow = row.get("workflow_run")
    if not isinstance(workflow, dict):
        raise _fail("workflow artifact run binding is missing")
    digest = row.get("digest")
    if any(
        (
            type(row.get("id")) is not int or row["id"] <= 0,
            row.get("name") != name,
            row.get("expired") is not False,
            not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None,
            type(workflow.get("id")) is not int or str(workflow["id"]) != run_id,
            workflow.get("head_sha") != head_sha,
            "run_attempt" in workflow
            and (type(workflow.get("run_attempt")) is not int
                 or str(workflow["run_attempt"]) != run_attempt),
        )
    ):
        raise _fail("workflow artifact metadata does not bind to the exact run")
    return row


def _closed_zip(
    archive_path: Path,
    *,
    expected_members: set[str],
    max_archive_bytes: int,
    max_total_bytes: int,
) -> dict[str, bytes]:
    info = archive_path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or not 1 <= info.st_size <= max_archive_bytes
    ):
        raise _fail("downloaded artifact ZIP identity or size is invalid")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [entry.filename for entry in infos]
            if (
                not infos
                or len(infos) > MAX_ZIP_ENTRIES
                or len(names) != len(set(names))
                or set(names) != expected_members
            ):
                raise _fail("artifact ZIP member set is not closed")
            total = 0
            result: dict[str, bytes] = {}
            for entry in infos:
                mode = (entry.external_attr >> 16) & 0xFFFF
                if (
                    not entry.filename.isascii()
                    or entry.is_dir()
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                    or entry.flag_bits & 0x1
                    or entry.file_size > max_total_bytes
                    or (entry.compress_size == 0 and entry.file_size > 0)
                ):
                    raise _fail("artifact ZIP member is unsafe")
                total += entry.file_size
                if total > max_total_bytes:
                    raise _fail("artifact ZIP expands beyond its size bound")
                result[entry.filename] = archive.read(entry)
            return result
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise _fail("artifact ZIP is malformed") from exc


def _remove_private_extraction_if_same(
    destination: Path,
    identity: tuple[int, int],
) -> None:
    try:
        current = destination.lstat()
    except OSError:
        return
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_uid != os.geteuid()
        or (current.st_dev, current.st_ino) != identity
    ):
        return
    for path in destination.iterdir():
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid():
            path.unlink(missing_ok=True)
    try:
        destination.rmdir()
    except OSError:
        pass


def _extract_verified_build_members(
    destination: Path,
    members: dict[str, bytes],
    *,
    manifest: dict[str, object],
) -> tuple[int, int]:
    """Materialize only the already-verified closed build artifact privately."""

    expected = {"codex", "codex.manifest.json", "version.txt"}
    if set(members) != expected:
        raise _fail("verified build artifact member set changed before extraction")
    created_directory_identity: tuple[int, int] | None = None

    try:
        parent_info = destination.parent.lstat()
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid != os.geteuid()
            or stat.S_IMODE(parent_info.st_mode) & 0o077
        ):
            raise _fail("extraction parent is not a private owned directory")
        filesystem = os.statvfs(destination.parent)
        required_bytes = sum(len(data) for data in members.values())
        required_bytes += (len(expected) + 1) * filesystem.f_frsize
        available_bytes = filesystem.f_bavail * filesystem.f_frsize
        if available_bytes < required_bytes:
            raise _fail("private extraction directory lacks bounded artifact space")
        available_memory = None
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                available_memory = int(line.split()[1]) * 1024
                break
        if available_memory is None or available_memory < required_bytes + 64 * 1024 * 1024:
            raise _fail("available memory is insufficient for bounded artifact extraction")
        destination.mkdir(mode=0o700)
        destination_info = destination.lstat()
        created_directory_identity = (destination_info.st_dev, destination_info.st_ino)
        if (
            not stat.S_ISDIR(destination_info.st_mode)
            or stat.S_IMODE(destination_info.st_mode) != 0o700
            or destination_info.st_uid != os.geteuid()
        ):
            raise _fail("extraction directory identity or mode is unsafe")
        for name in sorted(expected):
            path = destination / name
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(members[name])
                stream.flush()
                os.fsync(stream.fileno())
        extracted_binary_path = destination / "codex"
        extracted_binary_size = 0
        extracted_binary_hash = hashlib.sha256()
        with extracted_binary_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                extracted_binary_size += len(chunk)
                extracted_binary_hash.update(chunk)
        if (
            extracted_binary_size != manifest.get("binary_bytes")
            or extracted_binary_hash.hexdigest() != manifest.get("binary_sha256")
            or json.loads((destination / "codex.manifest.json").read_text(encoding="utf-8")) != manifest
        ):
            raise _fail("extracted companion does not match its verified identity")
        if (destination / "version.txt").read_bytes() != (
            str(manifest.get("cli_version", "")).encode("ascii") + b"\n"
        ):
            raise _fail("extracted companion version does not match its verified identity")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if created_directory_identity is not None:
            _remove_private_extraction_if_same(destination, created_directory_identity)
        raise _fail("private artifact extraction failed") from exc
    except VerificationError:
        if created_directory_identity is not None:
            _remove_private_extraction_if_same(destination, created_directory_identity)
        raise
    assert created_directory_identity is not None
    return created_directory_identity


def check_persistent_install_space(install_root: Path, payload_paths: list[Path]) -> None:
    """Require space for block-rounded install inputs above the shared disk floor."""

    try:
        root_info = install_root.lstat()
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or root_info.st_uid != os.geteuid()
            or stat.S_IMODE(root_info.st_mode) & 0o022
        ):
            raise _fail("persistent install root identity or permissions are unsafe")
        filesystem = os.statvfs(install_root)
        block_size = filesystem.f_frsize
        if block_size <= 0:
            raise _fail("persistent install filesystem block size is invalid")
        total_bytes = filesystem.f_blocks * block_size
        available_bytes = filesystem.f_bavail * block_size
        floor_bytes = max(
            MINIMUM_FREE_BYTES,
            (total_bytes * MINIMUM_FREE_PERCENT + 99) // 100,
        )
        allocation_bytes = 0
        for path in payload_paths:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise _fail("persistent install input is not a regular file")
            allocation_bytes += ((info.st_size + block_size - 1) // block_size) * block_size
        allocation_bytes += (len(payload_paths) + 1) * block_size
        if available_bytes < floor_bytes + allocation_bytes:
            raise _fail("persistent install would cross the shared free-space floor")
    except OSError as exc:
        raise _fail("persistent install capacity could not be verified") from exc


def _classifier_candidates(source_root: Path) -> set[str]:
    path = source_root / "platform/tools/platform_ci_classifier.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "CANDIDATE_PACKAGING_FILES" for target in node.targets):
            continue
        value = node.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and len(value.args) == 1
        ):
            break
        items = ast.literal_eval(value.args[0])
        if isinstance(items, set) and all(isinstance(item, str) for item in items):
            return items
        break
    raise _fail("reviewed classifier candidate list is malformed")


def _validate_classifier_manifest(
    raw: bytes, *, source_root: Path, head: str, merge: str
) -> dict[str, Any]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("Security classifier manifest is malformed") from exc
    fields = {
        "schema", "version", "target_sha", "event", "class", "expected_gates",
        "runtime_sensitive", "deployable", "fallback", "reason", "files", "digest",
    }
    if not isinstance(manifest, dict) or set(manifest) != fields:
        raise _fail("Security classifier manifest schema is not closed")
    files = manifest.get("files")
    candidates = _classifier_candidates(source_root)
    codex_paths = {
        CODEX_WORKFLOW_PATH,
        CODEX_TEST_PATH,
        "platform/tools/codex_archive_retention/__init__.py",
        "platform/tools/codex_archive_retention/cleanup.py",
        "platform/tools/codex_archive_retention/verify_artifact.py",
        "platform/tools/codex_archive_retention/codex-source.lock.json",
        "platform/tools/codex_archive_retention/codex-archive-retention.service",
        "platform/tools/codex_archive_retention/codex-archive-retention.timer",
        "platform/tools/codex_archive_retention/patches/codex-0.166.0.patch",
        "platform/tools/platform_ci_classifier.py",
        "platform/tests/test_platform_ci_classifier.py",
        "platform/tools/platform_test_catalog.py",
    }
    digest_payload = {
        field: manifest[field]
        for field in (
            "schema", "version", "target_sha", "event", "class", "expected_gates",
            "runtime_sensitive", "deployable", "fallback", "reason", "files",
        )
    }
    expected_digest = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    file_list_valid = (
        isinstance(files, list)
        and bool(files)
        and all(isinstance(path, str) and path for path in files)
        and files == sorted(set(files))
    )
    full_paths_valid = file_list_valid and all(
        path.startswith((".github/", "platform/"))
        or path == ".agents/skills/platform-storage-retention/SKILL.md"
        for path in files
    )
    codex_paths_present = file_list_valid and any(path in codex_paths for path in files)
    reason = manifest.get("reason")
    if reason == CANDIDATE_REASON:
        route_reason_valid = (
            file_list_valid
            and all(path in candidates or path.startswith("platform/docs/") for path in files)
            and manifest.get("runtime_sensitive") is False
        )
    else:
        route_reason_valid = reason == STANDARD_FULL_REASON and full_paths_valid
    if any(
        (
            type(manifest.get("schema")) is not int or manifest["schema"] != 1,
            type(manifest.get("version")) is not int or manifest["version"] != 1,
            manifest.get("target_sha") != merge,
            manifest.get("event") != "pull_request",
            manifest.get("class") != "full",
            manifest.get("expected_gates") != GATES,
            type(manifest.get("runtime_sensitive")) is not bool,
            manifest.get("deployable") is not False,
            manifest.get("fallback") is not False,
            not codex_paths_present,
            not route_reason_valid,
            manifest.get("digest") != expected_digest,
        )
    ):
        raise _fail("Security classifier artifact is not the exact full non-deployable companion route")
    if not HEX40.fullmatch(head):
        raise _fail("reviewed PR head SHA is malformed")
    return manifest


def _verify_source_checkout(source_root: Path, *, expected_head: str) -> dict[str, Any]:
    head = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "--verify", "HEAD^{commit}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, timeout=10, check=False,
    )
    status = subprocess.run(
        ["git", "-C", str(source_root), "status", "--porcelain=v1", "--untracked-files=all"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, timeout=10, check=False,
    )
    if head.returncode != 0 or head.stdout.strip() != expected_head or status.returncode != 0 or status.stdout:
        raise _fail("reviewed platform source checkout is not exact and clean")
    lock_path = source_root / "platform/tools/codex_archive_retention/codex-source.lock.json"
    lock = _read_json(lock_path, maximum=64 * 1024)
    expected = {
        "schema_version": 1,
        "source_repository": "https://github.com/openai/codex.git",
        "source_tag": "rust-v0.166.0",
        "source_commit": "6ff1e1ca3b30fc8b914720681fcf5fe62e95f856",
        "cli_version": "codex-cli 0.166.0",
        "rust_toolchain": "1.95.0",
        "target": "x86_64-unknown-linux-gnu",
        "patch_file": "patches/codex-0.166.0.patch",
        "max_binary_bytes": MAX_ARTIFACT_BYTES,
    }
    if not isinstance(lock, dict) or any(lock.get(key) != value for key, value in expected.items()):
        raise _fail("reviewed Codex source lock differs from the supported pin")
    patch = source_root / "platform/tools/codex_archive_retention" / lock["patch_file"]
    if not HEX64.fullmatch(lock.get("patch_sha256", "")) or _sha256(patch) != lock["patch_sha256"]:
        raise _fail("reviewed Codex patch digest does not match the source lock")
    return lock


def _validate_build_artifact(
    members: dict[str, bytes],
    *,
    source_root: Path,
    lock: dict[str, Any],
    pr: str,
    head: str,
    merge: str,
    build_run_id: str,
    build_attempt: str,
) -> dict[str, Any]:
    if (
        len(members.get("codex.manifest.json", b"")) > 64 * 1024
        or len(members.get("version.txt", b"")) > 256
        or len(members.get("codex", b"")) > lock["max_binary_bytes"]
    ):
        raise _fail("companion artifact member exceeds its reviewed bound")
    try:
        manifest = json.loads(members["codex.manifest.json"].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("companion manifest is malformed") from exc
    expected_keys = {
        "schema_version", "platform_repository", "platform_event", "platform_ref",
        "platform_base_ref", "platform_head_repository", "platform_pr_number",
        "platform_source_sha", "platform_source_head_sha", "platform_tested_merge_sha",
        "workflow_run_id", "workflow_run_attempt", "source_repository", "source_tag",
        "source_commit", "patch_sha256", "rust_toolchain", "target", "cli_version",
        "binary_bytes", "binary_sha256",
    }
    binary = members["codex"]
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        raise _fail("companion manifest schema is not closed")
    expected = {
        "schema_version": 1,
        "platform_repository": REPOSITORY,
        "platform_event": "pull_request",
        "platform_ref": f"refs/pull/{pr}/merge",
        "platform_base_ref": "dev",
        "platform_head_repository": REPOSITORY,
        "platform_pr_number": pr,
        "platform_source_sha": head,
        "platform_source_head_sha": head,
        "platform_tested_merge_sha": merge,
        "workflow_run_id": build_run_id,
        "workflow_run_attempt": build_attempt,
        "source_repository": lock["source_repository"],
        "source_tag": lock["source_tag"],
        "source_commit": lock["source_commit"],
        "patch_sha256": lock["patch_sha256"],
        "rust_toolchain": lock["rust_toolchain"],
        "target": lock["target"],
        "cli_version": lock["cli_version"],
        "binary_bytes": len(binary),
        "binary_sha256": hashlib.sha256(binary).hexdigest(),
    }
    if (
        manifest != expected
        or members["version.txt"].decode("ascii", errors="replace").strip()
        != lock["cli_version"]
    ):
        raise _fail("companion binary or manifest provenance does not match the reviewed PR")
    return manifest


def verify(args: argparse.Namespace) -> None:
    for value in (args.build_run_id, args.build_attempt, args.security_run_id, args.security_attempt):
        if not RUN_ID.fullmatch(value):
            raise _fail("workflow run or attempt input is malformed")
    if not re.fullmatch(r"[1-9][0-9]{0,8}", args.pull_request):
        raise _fail("pull request number is malformed")
    if not HEX40.fullmatch(args.head_sha) or not HEX40.fullmatch(args.merge_sha):
        raise _fail("PR head or tested merge SHA is malformed")

    source_root = args.source_root.resolve(strict=True)
    lock = _verify_source_checkout(source_root, expected_head=args.head_sha)
    build_run = _run_identity(
        _read_json(args.build_run),
        run_id=args.build_run_id,
        attempt=args.build_attempt,
        workflow_name=BUILD_WORKFLOW_NAME,
        workflow_path=BUILD_WORKFLOW_PATH,
        expected_head=args.head_sha,
        pull_request=args.pull_request,
    )
    security_run = _run_identity(
        _read_json(args.security_run),
        run_id=args.security_run_id,
        attempt=args.security_attempt,
        workflow_name=SECURITY_WORKFLOW_NAME,
        workflow_path=SECURITY_WORKFLOW_PATH,
        expected_head=args.head_sha,
        pull_request=args.pull_request,
    )
    if build_run.get("head_sha") != security_run.get("head_sha"):
        raise _fail("native build and Security runs do not share the exact PR head")

    build_name = f"codex-archive-retention-{args.build_run_id}-{args.build_attempt}"
    build_artifact = _artifact_record(
        _read_json(args.build_artifacts),
        name=build_name,
        run_id=args.build_run_id,
        run_attempt=args.build_attempt,
        head_sha=args.head_sha,
    )
    if _sha256(args.build_zip) != build_artifact["digest"].removeprefix("sha256:"):
        raise _fail("raw native build artifact ZIP digest does not match GitHub metadata")
    build_members = _closed_zip(
        args.build_zip,
        expected_members={"codex", "codex.manifest.json", "version.txt"},
        max_archive_bytes=MAX_ARTIFACT_BYTES,
        max_total_bytes=MAX_ARTIFACT_BYTES + 64 * 1024,
    )
    build_manifest = _validate_build_artifact(
        build_members,
        source_root=source_root,
        lock=lock,
        pr=args.pull_request,
        head=args.head_sha,
        merge=args.merge_sha,
        build_run_id=args.build_run_id,
        build_attempt=args.build_attempt,
    )

    security_name = f"platform-ci-route-{args.security_run_id}-{args.security_attempt}"
    security_artifact = _artifact_record(
        _read_json(args.security_artifacts),
        name=security_name,
        run_id=args.security_run_id,
        run_attempt=args.security_attempt,
        head_sha=args.head_sha,
    )
    if _sha256(args.security_zip) != security_artifact["digest"].removeprefix("sha256:"):
        raise _fail("raw Security route ZIP digest does not match GitHub metadata")
    security_members = _closed_zip(
        args.security_zip,
        expected_members={"classifier-manifest.json"},
        max_archive_bytes=8 * 1024 * 1024,
        max_total_bytes=MAX_ROUTE_MANIFEST_BYTES,
    )
    route_manifest = _validate_classifier_manifest(
        security_members["classifier-manifest.json"],
        source_root=source_root,
        head=args.head_sha,
        merge=args.merge_sha,
    )

    activation = {
        "schema_version": 1,
        "platform_repository": REPOSITORY,
        "pull_request": args.pull_request,
        "platform_source_sha": args.head_sha,
        "tested_merge_sha": args.merge_sha,
        "build_run_id": args.build_run_id,
        "build_run_attempt": args.build_attempt,
        "build_artifact_id": str(build_artifact["id"]),
        "build_artifact_sha256": build_artifact["digest"].removeprefix("sha256:"),
        "security_workflow_name": SECURITY_WORKFLOW_NAME,
        "security_workflow_path": SECURITY_WORKFLOW_PATH,
        "security_run_id": args.security_run_id,
        "security_run_attempt": args.security_attempt,
        "security_conclusion": "success",
        "security_target_sha": args.merge_sha,
        "security_route_artifact_id": str(security_artifact["id"]),
        "security_route_sha256": security_artifact["digest"].removeprefix("sha256:"),
        "classifier_class": "full",
        "classifier_reason": route_manifest["reason"],
        "classifier_runtime_sensitive": route_manifest["runtime_sensitive"],
        "classifier_fallback": False,
        "classifier_deployable": False,
    }
    extraction_directory = getattr(args, "extract_directory", None)
    extraction_identity: tuple[int, int] | None = None
    if extraction_directory is not None:
        extraction_identity = _extract_verified_build_members(
            extraction_directory,
            build_members,
            manifest=build_manifest,
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    activation_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(args.activation_output, flags, 0o600)
        activation_info = os.fstat(descriptor)
        if not stat.S_ISREG(activation_info.st_mode) or activation_info.st_uid != os.geteuid():
            raise _fail("activation receipt identity is unsafe")
        activation_identity = (activation_info.st_dev, activation_info.st_ino)
        stream = os.fdopen(descriptor, "w", encoding="ascii")
        descriptor = None
        with stream:
            stream.write(json.dumps(activation, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if activation_identity is not None:
            try:
                current = Path(args.activation_output).lstat()
            except OSError:
                current = None
            if (
                current is not None
                and stat.S_ISREG(current.st_mode)
                and current.st_uid == os.geteuid()
                and (current.st_dev, current.st_ino) == activation_identity
            ):
                Path(args.activation_output).unlink(missing_ok=True)
        if extraction_directory is not None and extraction_identity is not None:
            _remove_private_extraction_if_same(extraction_directory, extraction_identity)
        raise
    print("Codex companion artifacts and exact CI provenance accepted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--pull-request", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--merge-sha", required=True)
    parser.add_argument("--build-run-id", required=True)
    parser.add_argument("--build-attempt", required=True)
    parser.add_argument("--build-run", type=Path, required=True)
    parser.add_argument("--build-artifacts", type=Path, required=True)
    parser.add_argument("--build-zip", type=Path, required=True)
    parser.add_argument("--security-run-id", required=True)
    parser.add_argument("--security-attempt", required=True)
    parser.add_argument("--security-run", type=Path, required=True)
    parser.add_argument("--security-artifacts", type=Path, required=True)
    parser.add_argument("--security-zip", type=Path, required=True)
    parser.add_argument("--extract-directory", type=Path)
    parser.add_argument("--activation-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify(args)
    except (OSError, subprocess.SubprocessError, VerificationError, zipfile.BadZipFile) as exc:
        print(f"Codex companion verification rejected: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
