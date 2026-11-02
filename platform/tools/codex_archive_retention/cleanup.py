#!/usr/bin/env python3
"""Delete archived Codex sessions through the pinned, guarded native CLI."""

from __future__ import annotations

import argparse
from bisect import bisect_right
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import Iterator
from urllib.parse import quote


PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_LOCK = PACKAGE_DIR / "codex-source.lock.json"
STATE_DB_NAME = "state_5.sqlite"
ARCHIVE_DIR_NAME = "archived_sessions"
ACTIVE_DIR_NAME = "sessions"
RETENTION_LOCK_NAME = ".codex-archive-retention.lock"
RETENTION_CURSOR_NAME = ".codex-archive-retention.cursor"
LEGACY_QUARANTINE_NAME = ".codex-archive-retention"
DEFAULT_CLI = Path("/usr/local/libexec/codex-archive-retention/codex")
DEFAULT_MANIFEST = Path("/usr/local/libexec/codex-archive-retention/codex.manifest.json")
DEFAULT_ACTIVATION = Path("/usr/local/libexec/codex-archive-retention/codex.activation.json")
MAX_DELETES_PER_RUN = 16
DELETE_TIMEOUT_SECONDS = 120
RELEASE_PROFILE = "release"
RELEASE_LTO = False
RELEASE_CODEGEN_UNITS = 4
ROLLOUT_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"(?P<thread>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:_(?P<rollout>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))?"
    r"\.jsonl(?:\.zst)?$"
)
THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class UnsafeState(RuntimeError):
    """A state or identity check failed; retention must stop without guessing."""


def log(message: str) -> None:
    print(f"codex-archive-retention: {message}", file=sys.stderr, flush=True)


def require_real_file(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise UnsafeState("required file is not a regular file")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UnsafeState("required file is group or world writable")
    if info.st_nlink != 1:
        raise UnsafeState("required file has unexpected hard links")
    if os.geteuid() == 0 and info.st_uid != 0:
        raise UnsafeState("required file is not root-owned")
    return info


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_companion(
    cli: Path, manifest_path: Path, activation_path: Path, home: Path
) -> None:
    lock = json.loads(SOURCE_LOCK.read_text(encoding="utf-8"))
    patch_file = PACKAGE_DIR / lock["patch_file"]
    if sha256_file(patch_file) != lock["patch_sha256"]:
        raise UnsafeState("local Codex source patch does not match the source lock")
    require_real_file(cli)
    require_real_file(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise UnsafeState("private Codex companion provenance is malformed")
    expected = {
        "schema_version": 1,
        "source_repository": lock["source_repository"],
        "source_commit": lock["source_commit"],
        "source_tag": lock["source_tag"],
        "patch_sha256": lock["patch_sha256"],
        "rust_toolchain": lock["rust_toolchain"],
        "target": lock["target"],
        "cli_version": lock["cli_version"],
        "release_profile": RELEASE_PROFILE,
        "release_lto": RELEASE_LTO,
        "release_codegen_units": RELEASE_CODEGEN_UNITS,
    }
    if (
        type(manifest.get("release_lto")) is not bool
        or type(manifest.get("release_codegen_units")) is not int
        or any(manifest.get(key) != value for key, value in expected.items())
    ):
        raise UnsafeState("private Codex companion provenance does not match the source lock")
    for field in ("platform_source_sha", "platform_source_head_sha"):
        if not isinstance(manifest.get(field), str) or not re.fullmatch(
            r"[0-9a-f]{40}", manifest[field]
        ):
            raise UnsafeState("private Codex companion platform source binding is malformed")
    if any(
        (
            manifest.get("platform_repository") != "StrayForest/old_sparky",
            manifest.get("platform_event") != "pull_request",
            manifest.get("platform_base_ref") != "dev",
            manifest.get("platform_head_repository") != "StrayForest/old_sparky",
            manifest.get("platform_source_sha") != manifest.get("platform_source_head_sha"),
            not isinstance(manifest.get("platform_pr_number"), str)
            or re.fullmatch(r"[1-9][0-9]{0,8}", manifest["platform_pr_number"]) is None,
            manifest.get("platform_ref")
            != f"refs/pull/{manifest.get('platform_pr_number')}/merge",
            not isinstance(manifest.get("platform_tested_merge_sha"), str)
            or re.fullmatch(r"[0-9a-f]{40}", manifest["platform_tested_merge_sha"]) is None,
            not isinstance(manifest.get("workflow_run_id"), str)
            or re.fullmatch(r"[1-9][0-9]{0,31}", manifest["workflow_run_id"]) is None,
            not isinstance(manifest.get("workflow_run_attempt"), str)
            or re.fullmatch(r"[1-9][0-9]{0,31}", manifest["workflow_run_attempt"]) is None,
        )
    ):
        raise UnsafeState("private Codex companion is not bound to a same-repository dev pull request")
    require_real_file(activation_path)
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    activation_fields = {
        "schema_version",
        "platform_repository",
        "pull_request",
        "platform_source_sha",
        "tested_merge_sha",
        "build_run_id",
        "build_run_attempt",
        "build_artifact_id",
        "build_artifact_sha256",
        "security_workflow_name",
        "security_workflow_path",
        "security_run_id",
        "security_run_attempt",
        "security_conclusion",
        "security_target_sha",
        "security_route_artifact_id",
        "security_route_sha256",
        "classifier_class",
        "classifier_reason",
        "classifier_runtime_sensitive",
        "classifier_fallback",
        "classifier_deployable",
    }
    if not isinstance(activation, dict) or set(activation) != activation_fields:
        raise UnsafeState("Codex companion activation receipt is malformed")
    if any(
        (
            activation.get("schema_version") != 1,
            activation.get("platform_repository") != manifest["platform_repository"],
            activation.get("pull_request") != manifest["platform_pr_number"],
            activation.get("platform_source_sha") != manifest["platform_source_sha"],
            activation.get("tested_merge_sha") != manifest["platform_tested_merge_sha"],
            activation.get("build_run_id") != manifest["workflow_run_id"],
            activation.get("build_run_attempt") != manifest["workflow_run_attempt"],
            activation.get("security_workflow_name") != "Platform security and build",
            activation.get("security_workflow_path") != ".github/workflows/platform-security.yml",
            activation.get("security_conclusion") != "success",
            activation.get("security_target_sha") != manifest["platform_tested_merge_sha"],
            activation.get("classifier_class") != "full",
            activation.get("classifier_reason") not in {
                "trusted candidate-packaging change requires full verification and is non-deployable",
                "platform or workflow change requires the full deterministic suite",
            },
            type(activation.get("classifier_runtime_sensitive")) is not bool,
            activation.get("classifier_reason")
            == "trusted candidate-packaging change requires full verification and is non-deployable"
            and activation.get("classifier_runtime_sensitive") is not False,
            activation.get("classifier_fallback") is not False,
            activation.get("classifier_deployable") is not False,
        )
    ):
        raise UnsafeState("Codex companion activation receipt does not match verified CI provenance")
    for field in ("security_run_id", "security_run_attempt"):
        value = activation.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,31}", value) is None:
            raise UnsafeState("Codex companion activation receipt run identity is malformed")
    for field in ("build_artifact_id", "security_route_artifact_id"):
        value = activation.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,31}", value) is None:
            raise UnsafeState("Codex companion activation receipt artifact identity is malformed")
    for field in ("build_artifact_sha256", "security_route_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", activation.get(field, "")) is None:
            raise UnsafeState("Codex companion activation receipt artifact digest is malformed")
    if manifest.get("binary_sha256") != sha256_file(cli):
        raise UnsafeState("private Codex companion digest does not match its provenance")
    binary_bytes = cli.stat().st_size
    if (
        binary_bytes > lock["max_binary_bytes"]
        or manifest.get("binary_bytes") != binary_bytes
    ):
        raise UnsafeState("private Codex companion size does not match its reviewed provenance")
    result = subprocess.run(
        [str(cli), "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
        check=False,
        env={**os.environ, "CODEX_HOME": str(home)},
    )
    if result.returncode != 0 or result.stdout.strip() != lock["cli_version"]:
        raise UnsafeState("private Codex companion version does not match the source lock")


def open_state_db(home: Path) -> sqlite3.Connection:
    path = home / STATE_DB_NAME
    require_real_file(path)
    connection = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    thread_columns = {
        row[1] for row in connection.execute('PRAGMA table_info("threads")')
    }
    edge_columns = {
        row[1] for row in connection.execute('PRAGMA table_info("thread_spawn_edges")')
    }
    if not {"id", "rollout_path", "archived", "archived_at"} <= thread_columns:
        connection.close()
        raise UnsafeState("Codex thread metadata schema is unsupported")
    if not {"parent_thread_id", "child_thread_id"} <= edge_columns:
        connection.close()
        raise UnsafeState("Codex spawn graph schema is unsupported")
    return connection


def thread_rows(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        row["id"]: row
        for row in connection.execute(
            "SELECT id, rollout_path, archived, archived_at FROM threads"
        )
    }


def spawn_graph(connection: sqlite3.Connection) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for row in connection.execute(
        "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"
    ):
        graph.setdefault(row["parent_thread_id"], set()).add(row["child_thread_id"])
    return graph


def walk_regular_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise UnsafeState("Codex rollout collection is not a real directory")
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if stat.S_ISDIR((current_path / name).lstat().st_mode)
            and not stat.S_ISLNK((current_path / name).lstat().st_mode)
        ]
        for name in files:
            path = current_path / name
            file_info = path.lstat()
            if stat.S_ISLNK(file_info.st_mode):
                if ROLLOUT_RE.fullmatch(name):
                    raise UnsafeState("symlink found where a Codex rollout is expected")
                continue
            if stat.S_ISREG(file_info.st_mode) and ROLLOUT_RE.fullmatch(name):
                yield path


def rollout_thread_id(path: Path) -> str | None:
    match = ROLLOUT_RE.fullmatch(path.name)
    return match.group("thread") if match else None


def canonical_archived_path(home: Path, stored_path: str, thread_id: str) -> Path | None:
    candidate = Path(stored_path)
    if not candidate.is_absolute():
        candidate = home / candidate
    archive_root = home / ARCHIVE_DIR_NAME
    try:
        candidate.relative_to(archive_root)
    except ValueError as error:
        raise UnsafeState("archived metadata points outside archived_sessions") from error
    if rollout_thread_id(candidate) != thread_id:
        raise UnsafeState("archived metadata filename does not match its thread ID")
    possibilities = [candidate]
    if candidate.suffix == ".jsonl":
        possibilities.append(Path(str(candidate) + ".zst"))
    for path in possibilities:
        try:
            file_info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(file_info.st_mode) or stat.S_ISLNK(file_info.st_mode):
            raise UnsafeState("archived metadata points to a non-regular rollout")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(archive_root.resolve(strict=True)):
            raise UnsafeState("archived rollout resolved outside archived_sessions")
        return resolved
    parent = candidate.parent.resolve(strict=True)
    if not parent.is_relative_to(archive_root.resolve(strict=True)):
        raise UnsafeState("missing archived rollout parent resolved outside archived_sessions")
    return None


def archived_candidates(home: Path) -> list[str]:
    archive = home / ARCHIVE_DIR_NAME
    if not archive.exists():
        return []
    archived_files: dict[str, list[Path]] = {}
    for path in walk_regular_files(archive):
        thread_id = rollout_thread_id(path)
        if thread_id:
            archived_files.setdefault(thread_id, []).append(path)
    active_ids = {
        thread_id
        for path in walk_regular_files(home / ACTIVE_DIR_NAME)
        if (thread_id := rollout_thread_id(path)) is not None
    }
    with open_state_db(home) as connection:
        rows = thread_rows(connection)
        graph = spawn_graph(connection)

    result: list[str] = []
    for thread_id, row in rows.items():
        if (
            not THREAD_ID_RE.fullmatch(thread_id)
            or row["archived"] != 1
            or not row["archived_at"]
            or thread_id in active_ids
            or graph.get(thread_id)
        ):
            continue
        try:
            expected_path = canonical_archived_path(home, row["rollout_path"], thread_id)
        except (OSError, UnsafeState):
            continue
        paths = archived_files.get(thread_id, [])
        if expected_path is None:
            if paths:
                continue
        elif not any(path.resolve(strict=True) == expected_path for path in paths):
            continue
        result.append(thread_id)
    return sorted(result)


def row_and_files(home: Path, thread_id: str) -> tuple[tuple[object, ...] | None, tuple[str, ...]]:
    with open_state_db(home) as connection:
        row = thread_rows(connection).get(thread_id)
    files = tuple(
        sorted(
            path.name
            for path in walk_regular_files(home / ARCHIVE_DIR_NAME)
            if rollout_thread_id(path) == thread_id
        )
    )
    if row is None:
        row_value = None
    else:
        row_value = (row["archived"], row["archived_at"], row["rollout_path"])
    return row_value, files


def archive_summary(home: Path) -> tuple[int, int]:
    with open_state_db(home) as connection:
        archived_rows = connection.execute(
            "SELECT COUNT(*) FROM threads WHERE archived = 1 AND archived_at IS NOT NULL"
        ).fetchone()[0]
    archived_files = sum(1 for _ in walk_regular_files(home / ARCHIVE_DIR_NAME))
    return int(archived_rows), archived_files


def ensure_no_legacy_quarantine(home: Path) -> None:
    legacy = home / LEGACY_QUARANTINE_NAME
    try:
        info = legacy.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise UnsafeState("legacy quarantine path is not a real directory")
    if any(legacy.iterdir()):
        raise UnsafeState("legacy quarantine has pending entries requiring operator review")


def load_cursor(home: Path) -> str | None:
    path = home / RETENTION_CURSOR_NAME
    try:
        path_info = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(path_info.st_mode)
        or stat.S_ISLNK(path_info.st_mode)
        or stat.S_IMODE(path_info.st_mode) != 0o600
        or path_info.st_nlink != 1
        or path_info.st_size > 256
        or (os.geteuid() == 0 and path_info.st_uid != 0)
    ):
        raise UnsafeState("retention cursor has unsafe identity or mode")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (path_info.st_dev, path_info.st_ino):
            raise UnsafeState("retention cursor identity changed while opening")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(257)
    finally:
        os.close(descriptor)
    if len(raw) > 256:
        raise UnsafeState("retention cursor exceeds its size bound")
    try:
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise UnsafeState("retention cursor is malformed") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "after_thread_id"}
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or not isinstance(payload.get("after_thread_id"), str)
        or not THREAD_ID_RE.fullmatch(payload["after_thread_id"])
    ):
        raise UnsafeState("retention cursor schema is unsupported")
    return payload["after_thread_id"]


def save_cursor(home: Path, thread_id: str) -> None:
    path = home / RETENTION_CURSOR_NAME
    payload = json.dumps(
        {"schema_version": 1, "after_thread_id": thread_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    temporary = home / f"{RETENTION_CURSOR_NAME}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def next_candidate(
    candidates: list[str], after_thread_id: str | None, visited: set[str]
) -> str | None:
    if not candidates:
        return None
    offset = bisect_right(candidates, after_thread_id) if after_thread_id else 0
    ordered = candidates[offset:] + candidates[:offset]
    return next((candidate for candidate in ordered if candidate not in visited), None)


def run_delete(cli: Path, home: Path, thread_id: str) -> int:
    result = subprocess.run(
        [str(cli), "delete", "--force", "--archived-only", thread_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "CODEX_HOME": str(home)},
        timeout=DELETE_TIMEOUT_SECONDS,
        check=False,
    )
    return result.returncode


def run_maintenance(home: Path, cli: Path, manifest: Path, activation: Path) -> int:
    verify_companion(cli, manifest, activation, home)
    ensure_no_legacy_quarantine(home)
    lock_path = home / RETENTION_LOCK_NAME
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        lock_info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_mode & 0o077
            or lock_info.st_nlink != 1
            or (os.geteuid() == 0 and lock_info.st_uid != 0)
        ):
            raise UnsafeState("retention lock file has unsafe identity or mode")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        attempts = 0
        deleted = 0
        blocked: set[str] = set()
        native_refusals = 0
        metadata_races = 0
        visited: set[str] = set()
        cursor = load_cursor(home)
        considered = 0
        while considered < MAX_DELETES_PER_RUN and attempts < MAX_DELETES_PER_RUN:
            candidate = next_candidate(
                archived_candidates(home), cursor, visited
            )
            if candidate is None:
                break
            considered += 1
            visited.add(candidate)
            cursor = candidate
            save_cursor(home, cursor)
            before = row_and_files(home, candidate)
            if before[0] is None:
                blocked.add(candidate)
                metadata_races += 1
                continue
            attempts += 1
            result = run_delete(cli, home, candidate)
            after = row_and_files(home, candidate)
            if result != 0:
                if after != before:
                    raise UnsafeState("native delete failed after changing archived state")
                blocked.add(candidate)
                native_refusals += 1
                continue
            if after[0] is not None or after[1]:
                raise UnsafeState("native delete returned success with archived state remaining")
            deleted += 1
        archived_rows, archived_files = archive_summary(home)
        log(
            f"native attempts {attempts}/{MAX_DELETES_PER_RUN}; deleted {deleted} "
            f"archived session(s); deferred {len(blocked)} candidate(s) "
            f"(native refusals {native_refusals}, metadata races {metadata_races}); "
            f"{archived_rows} archived database row(s) "
            f"and {archived_files} archived rollout file(s) remain"
        )
        return 0
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser(),
    )
    parser.add_argument("--codex-cli", type=Path, default=DEFAULT_CLI)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--activation-manifest", type=Path, default=DEFAULT_ACTIVATION)
    args = parser.parse_args()
    home_info = args.codex_home.lstat()
    if not stat.S_ISDIR(home_info.st_mode) or stat.S_ISLNK(home_info.st_mode):
        raise UnsafeState("Codex home is not a real directory")
    home = args.codex_home.resolve(strict=True)
    return run_maintenance(home, args.codex_cli, args.manifest, args.activation_manifest)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (UnsafeState, OSError, sqlite3.Error, subprocess.SubprocessError, json.JSONDecodeError) as error:
        log(f"maintenance stopped safely: {error}")
        raise SystemExit(1)
