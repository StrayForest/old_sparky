#!/usr/bin/env python3
"""Bounded, read-only metadata census for visible /dev/shm inodes.

This diagnostic reports filesystem allocation from ``st_blocks * 512``. It
does not measure resident memory, deleted-open inodes, process ownership, or
grant cleanup or backup admission authority.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import resource
import sqlite3
import stat
import sys
import time
from datetime import datetime, timezone


TOOL_SCHEMA = 1
BYTES_PER_BLOCK = 512
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
PATH_FLAGS = getattr(os, "O_PATH", 0) | os.O_NOFOLLOW | os.O_CLOEXEC
CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
MAX_MEMINFO_BYTES = 64 * 1024
MAX_FDINFO_BYTES = 4096
JSONL_CHUNK_BYTES = 64 * 1024
SQLITE_CACHE_KIB = 1024
MAX_RECORDED_ERRORS = 256


@dataclass(frozen=True, slots=True)
class Limits:
    entries: int = 262_144
    entries_per_top_root: int = 131_072
    top_roots: int = 128
    depth: int = 32
    name_bytes: int = 16 * 1024 * 1024
    top_name_bytes: int = 16 * 1024
    report_bytes: int = 64 * 1024 * 1024
    index_bytes: int = 128 * 1024 * 1024
    wall_seconds: int = 90
    cpu_seconds: int = 85
    address_space_bytes: int = 128 * 1024 * 1024

    def as_dict(self) -> dict[str, int | str]:
        return {
            "entries": self.entries,
            "entries_per_top_root": self.entries_per_top_root,
            "top_roots": self.top_roots,
            "depth_including_top_level_zero": self.depth,
            "name_bytes": self.name_bytes,
            "top_name_bytes": self.top_name_bytes,
            "report_bytes": self.report_bytes,
            "index_bytes": self.index_bytes,
            "wall_seconds": self.wall_seconds,
            "cpu_seconds": self.cpu_seconds,
            "address_space_bytes": self.address_space_bytes,
            "sqlite_cache_kib": SQLITE_CACHE_KIB,
            "sqlite_temp_store": "MEMORY",
            "jsonl_write_chunk_bytes": JSONL_CHUNK_BYTES,
        }


DEFAULT_LIMITS = Limits()


class AuditError(Exception):
    """Closed diagnostic error with a non-sensitive category and errno."""

    def __init__(self, category: str, err: int | None = None):
        super().__init__(category)
        self.category = category
        self.errno = err


class Errors:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.suppressed = 0

    def add(self, category: str, err: int | None = None) -> None:
        if len(self.rows) < MAX_RECORDED_ERRORS:
            self.rows.append({"category": category, "errno": err})
        else:
            self.suppressed += 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _stat_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        getattr(info, "st_blocks", -1),
    )


def _stat_record(info: os.stat_result) -> dict[str, int]:
    return {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "mode": info.st_mode,
        "type": stat.S_IFMT(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "nlink": info.st_nlink,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "blocks_512": getattr(info, "st_blocks", -1),
    }


def read_memory_snapshot(raw: bytes, *, captured_at_utc: str | None = None) -> dict[str, object]:
    """Parse only MemAvailable and Shmem from one bounded proc-meminfo sample."""
    if len(raw) > MAX_MEMINFO_BYTES:
        raise AuditError("meminfo_size_limit")
    values: dict[str, int] = {}
    for line in raw.splitlines():
        if line.startswith((b"MemAvailable:", b"Shmem:")):
            fields = line.split()
            try:
                key = fields[0][:-1].decode("ascii", errors="strict") if fields else ""
            except UnicodeDecodeError as exc:
                raise AuditError("meminfo_key_invalid") from exc
            if key not in {"MemAvailable", "Shmem"} or key in values:
                raise AuditError("meminfo_duplicate_or_invalid_key")
            if len(fields) != 3 or fields[2] != b"kB" or not fields[1].isdigit():
                raise AuditError("meminfo_value_invalid")
            values[key] = int(fields[1])
    if set(values) != {"MemAvailable", "Shmem"}:
        raise AuditError("meminfo_required_field_missing")
    return {
        "captured_at_utc": captured_at_utc or utc_now(),
        "status": "OBSERVED",
        "mem_available_kib": values["MemAvailable"],
        "mem_available_bytes": values["MemAvailable"] * 1024,
        "shmem_kib": values["Shmem"],
        "shmem_bytes": values["Shmem"] * 1024,
        "source": "/proc/meminfo",
        "scope_note": "contextual counters only; no admission decision or ownership attribution",
    }


def read_memory_sample(path: str = "/proc/meminfo") -> dict[str, object]:
    fd = os.open(path, READ_FLAGS)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise AuditError("meminfo_not_regular")
        raw = bytearray()
        while len(raw) <= MAX_MEMINFO_BYTES:
            chunk = os.read(fd, min(4096, MAX_MEMINFO_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        if len(raw) > MAX_MEMINFO_BYTES:
            raise AuditError("meminfo_size_limit")
        if _stat_identity(before) != _stat_identity(after):
            raise AuditError("meminfo_identity_drift")
        return read_memory_snapshot(bytes(raw))
    finally:
        os.close(fd)


def _open_own_fdinfo() -> tuple[list[int], int]:
    chain = [os.open("/", DIR_FLAGS)]
    root = os.fstat(chain[0])
    if not stat.S_ISDIR(root.st_mode) or root.st_uid != 0 or root.st_mode & 0o022:
        raise AuditError("proc_root_identity_invalid")
    for component in ("proc", str(os.getpid()), "fdinfo"):
        fd = os.open(component, DIR_FLAGS, dir_fd=chain[-1])
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            os.close(fd)
            raise AuditError("own_fdinfo_identity_invalid")
        if component == "proc" and (info.st_uid != 0 or info.st_mode & 0o022):
            os.close(fd)
            raise AuditError("proc_directory_identity_invalid")
        if component.isdigit() and (int(component) != os.getpid() or info.st_uid != os.geteuid()):
            os.close(fd)
            raise AuditError("own_process_identity_invalid")
        chain.append(fd)
    return chain, chain[-1]


def _fd_mount_id(fd: int, fdinfo_fd: int) -> int:
    info_fd = os.open(str(fd), READ_FLAGS, dir_fd=fdinfo_fd)
    try:
        info = os.fstat(info_fd)
        if not stat.S_ISREG(info.st_mode):
            raise AuditError("fdinfo_not_regular")
        raw = bytearray()
        while len(raw) <= MAX_FDINFO_BYTES:
            chunk = os.read(info_fd, min(1024, MAX_FDINFO_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_FDINFO_BYTES:
            raise AuditError("fdinfo_size_limit")
    finally:
        os.close(info_fd)
    found: list[int] = []
    for line in raw.splitlines():
        if line.startswith(b"mnt_id:"):
            fields = line.split()
            if len(fields) != 2 or not fields[1].isdigit():
                raise AuditError("fdinfo_mount_id_invalid")
            found.append(int(fields[1]))
    if len(found) != 1:
        raise AuditError("fdinfo_mount_id_missing_or_duplicate")
    return found[0]


def open_shm_root() -> tuple[list[int], int, int]:
    chain = [os.open("/", DIR_FLAGS)]
    root = os.fstat(chain[0])
    if not stat.S_ISDIR(root.st_mode) or root.st_uid != 0 or root.st_mode & 0o022:
        raise AuditError("root_chain_identity_invalid")
    for component in ("dev", "shm"):
        fd = os.open(component, DIR_FLAGS, dir_fd=chain[-1])
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
            os.close(fd)
            raise AuditError("fixed_shm_identity_invalid")
        if component == "dev" and info.st_mode & 0o022:
            os.close(fd)
            raise AuditError("dev_directory_mode_invalid")
        if component == "shm" and (stat.S_IMODE(info.st_mode) != 0o1777 or not info.st_mode & stat.S_ISVTX):
            os.close(fd)
            raise AuditError("shm_sticky_mode_invalid")
        chain.append(fd)
    return chain, chain[-1], os.fstat(chain[-1]).st_dev


def open_private_parent(path: Path) -> tuple[list[int], int, str]:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise AuditError("output_path_invalid")
    chain = [os.open("/", DIR_FLAGS)]
    current = chain[0]
    try:
        for component in path.parts[1:-1]:
            if component in {"", ".", ".."}:
                raise AuditError("output_path_component_invalid")
            fd = os.open(component, DIR_FLAGS, dir_fd=current)
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
                os.close(fd)
                raise AuditError("output_parent_identity_invalid")
            chain.append(fd)
            current = fd
        leaf = path.name
        if "/" in leaf or leaf in {"", ".", ".."}:
            raise AuditError("output_leaf_invalid")
        return chain, current, leaf
    except BaseException:
        for fd in reversed(chain):
            os.close(fd)
        raise


def create_output_dir(path: Path) -> tuple[list[int], int]:
    chain, parent_fd, leaf = open_private_parent(path)
    try:
        os.mkdir(leaf, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
        output_fd = os.open(leaf, DIR_FLAGS, dir_fd=parent_fd)
        info = os.fstat(output_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700 or info.st_nlink < 2:
            os.close(output_fd)
            raise AuditError("output_directory_identity_invalid")
        return chain, output_fd
    except BaseException:
        for fd in reversed(chain):
            os.close(fd)
        raise


def _create_output_file(out_fd: int, name: str) -> int:
    fd = os.open(name, CREATE_FLAGS, 0o600, dir_fd=out_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        os.close(fd)
        raise AuditError("output_file_identity_invalid")
    return fd


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        count = os.write(fd, view[:JSONL_CHUNK_BYTES])
        if count <= 0:
            raise AuditError("short_output_write")
        view = view[count:]


def _entry_fd(parent_fd: int, name: bytes) -> tuple[int, os.stat_result]:
    if not PATH_FLAGS:
        raise AuditError("opath_unavailable")
    fd = os.open(name, PATH_FLAGS, dir_fd=parent_fd)
    try:
        return fd, os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise


def _record_signature(info: os.stat_result, mount_id: int) -> bytes:
    row = _stat_record(info)
    row["mount_id"] = mount_id
    return json.dumps(row, sort_keys=True, separators=(",", ":")).encode("ascii")


def _scan_pass(
    shm_fd: int,
    shm_mount_id: int,
    fdinfo_fd: int,
    db: sqlite3.Connection,
    inventory_fd: int | None,
    pass_number: int,
    deadline: float,
    limits: Limits = DEFAULT_LIMITS,
) -> dict[str, object]:
    errors = Errors()
    counts = {"entries": 0, "name_bytes": 0, "report_bytes": 0, "top_roots": 0}
    per_root: dict[bytes, int] = {}
    top_names: set[bytes] = set()

    def check_deadline() -> None:
        if time.monotonic() > deadline:
            raise AuditError("deadline_exceeded")

    def emit(path: bytes, name: bytes, parent_path: bytes, root_name: bytes, depth: int,
             fd: int, info: os.stat_result, mount_id: int) -> None:
        check_deadline()
        if getattr(info, "st_blocks", -1) < 0:
            errors.add("st_blocks_unavailable")
        counts["entries"] += 1
        counts["name_bytes"] += len(path)
        per_root[root_name] = per_root.get(root_name, 0) + 1
        if counts["entries"] > limits.entries:
            raise AuditError("entry_limit")
        if per_root[root_name] > limits.entries_per_top_root:
            raise AuditError("per_top_root_entry_limit")
        if counts["name_bytes"] > limits.name_bytes:
            raise AuditError("name_bytes_limit")
        if depth == 0:
            counts["top_roots"] += 1
            if counts["top_roots"] > limits.top_roots or sum(map(len, top_names)) + len(name) > limits.top_name_bytes:
                raise AuditError("top_root_limit")
            top_names.add(name)
        root_id = base64.b64encode(root_name).decode("ascii")
        row = {
            "pass": pass_number,
            "entry_id": hashlib.sha256(path).hexdigest(),
            "parent_id": "ROOT" if not parent_path else hashlib.sha256(parent_path).hexdigest(),
            "root_id": root_id,
            "basename_b64": base64.b64encode(name).decode("ascii"),
            "depth": depth,
            **_stat_record(info),
            "mount_id": mount_id,
        }
        record = _json_bytes(row) if pass_number == 1 else b""
        counts["report_bytes"] += len(record)
        if counts["report_bytes"] > limits.report_bytes:
            raise AuditError("report_limit")
        if inventory_fd is not None:
            _write_all(inventory_fd, record)
        signature = _record_signature(info, mount_id)
        if pass_number == 1:
            db.execute("INSERT INTO paths(path,sig,root_name) VALUES(?,?,?)", (path, signature, root_name))
            inode = db.execute("SELECT mode,blocks,nlink,root_name FROM inodes WHERE dev=? AND ino=?", (info.st_dev, info.st_ino)).fetchone()
            blocks = getattr(info, "st_blocks", -1)
            if inode is None:
                db.execute("INSERT INTO inodes(dev,ino,mode,blocks,nlink,occurrences,root_name,shared) VALUES(?,?,?,?,?,1,?,0)",
                           (info.st_dev, info.st_ino, info.st_mode, blocks, info.st_nlink, root_name))
            else:
                if (inode[0], inode[1], inode[2]) != (info.st_mode, blocks, info.st_nlink):
                    errors.add("inode_metadata_inconsistent_within_pass")
                db.execute("UPDATE inodes SET occurrences=occurrences+1,shared=CASE WHEN root_name<>? THEN 1 ELSE shared END WHERE dev=? AND ino=?",
                           (root_name, info.st_dev, info.st_ino))
        else:
            stored = db.execute("SELECT sig FROM paths WHERE path=?", (path,)).fetchone()
            if stored is None or stored[0] != signature:
                errors.add("pass_identity_drift")
            else:
                db.execute("DELETE FROM paths WHERE path=?", (path,))
            if stat.S_ISREG(info.st_mode):
                db.execute("INSERT INTO links_pass2(dev,ino,nlink,occurrences) VALUES(?,?,?,1) "
                           "ON CONFLICT(dev,ino) DO UPDATE SET occurrences=occurrences+1",
                           (info.st_dev, info.st_ino, info.st_nlink))
            inode = db.execute("SELECT mode,blocks,nlink FROM inodes WHERE dev=? AND ino=?", (info.st_dev, info.st_ino)).fetchone()
            if inode is None or (inode[0], inode[1], inode[2]) != (info.st_mode, getattr(info, "st_blocks", -1), info.st_nlink):
                errors.add("inode_metadata_changed_between_passes")

    def walk(parent_fd: int, relative: bytes, root_name: bytes, parent_mount_id: int, depth: int) -> None:
        check_deadline()
        parent_before = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_before.st_mode):
            raise AuditError("opened_directory_invalid")
        if _fd_mount_id(parent_fd, fdinfo_fd) != parent_mount_id:
            errors.add("directory_mount_id_drift")
        with os.scandir(parent_fd) as iterator:
            for entry in iterator:
                check_deadline()
                name = os.fsencode(entry.name)
                if not name or name in {b".", b".."} or b"/" in name or b"\x00" in name:
                    raise AuditError("directory_name_invalid")
                path = name if not relative else relative + b"/" + name
                child_fd, info = _entry_fd(parent_fd, name)
                try:
                    mount_id = _fd_mount_id(child_fd, fdinfo_fd)
                    emit(path, name, relative, root_name, depth, child_fd, info, mount_id)
                    if mount_id != parent_mount_id or info.st_dev != parent_before.st_dev:
                        errors.add("mount_crossing")
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if depth >= limits.depth:
                            raise AuditError("depth_limit")
                        child_dir = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
                        try:
                            opened = os.fstat(child_dir)
                            if _stat_identity(opened) != _stat_identity(info) or _fd_mount_id(child_dir, fdinfo_fd) != mount_id:
                                errors.add("directory_entry_identity_drift")
                                continue
                            walk(child_dir, path, root_name, mount_id, depth + 1)
                        finally:
                            os.close(child_dir)
                finally:
                    os.close(child_fd)
        if _stat_identity(parent_before) != _stat_identity(os.fstat(parent_fd)):
            errors.add("directory_identity_drift")

    try:
        with db:
            shm_before = os.fstat(shm_fd)
            with os.scandir(shm_fd) as iterator:
                for entry in iterator:
                    check_deadline()
                    name = os.fsencode(entry.name)
                    if not name or b"/" in name or b"\x00" in name:
                        raise AuditError("top_level_name_invalid")
                    entry_fd, info = _entry_fd(shm_fd, name)
                    try:
                        mount_id = _fd_mount_id(entry_fd, fdinfo_fd)
                        emit(name, name, b"", name, 0, entry_fd, info, mount_id)
                        if mount_id != shm_mount_id or info.st_dev != shm_before.st_dev:
                            errors.add("mount_crossing")
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            directory_fd = os.open(name, DIR_FLAGS, dir_fd=shm_fd)
                            try:
                                opened = os.fstat(directory_fd)
                                if _stat_identity(opened) != _stat_identity(info) or _fd_mount_id(directory_fd, fdinfo_fd) != mount_id:
                                    errors.add("top_level_identity_drift")
                                    continue
                                walk(directory_fd, name, name, mount_id, 1)
                            finally:
                                os.close(directory_fd)
                    finally:
                        os.close(entry_fd)
            if _stat_identity(shm_before) != _stat_identity(os.fstat(shm_fd)):
                errors.add("shm_root_identity_drift")
            if pass_number == 2 and db.execute("SELECT COUNT(*) FROM paths").fetchone()[0]:
                errors.add("paths_missing_on_second_pass")
            if pass_number == 2 and db.execute("SELECT COUNT(*) FROM links_pass2 WHERE occurrences<>nlink").fetchone()[0]:
                errors.add("regular_file_link_completeness_unknown")
    except AuditError as exc:
        errors.add(exc.category, exc.errno)
    except OSError as exc:
        errors.add("scan_io", exc.errno)
    return {
        "pass": pass_number,
        "status": "COMPLETE" if not errors.rows and not errors.suppressed else "UNKNOWN",
        **counts,
        "errors": errors.rows,
        "suppressed_error_count": errors.suppressed,
    }


def _hash_fd(fd: int, maximum: int) -> tuple[str, int]:
    before = os.fstat(fd)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
    ):
        raise AuditError("artifact_identity_invalid")
    digest = hashlib.sha256()
    total = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, min(64 * 1024, maximum + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise AuditError("artifact_size_limit")
        digest.update(chunk)
    after = os.fstat(fd)
    if _stat_identity(before) != _stat_identity(after):
        raise AuditError("artifact_identity_drift")
    return digest.hexdigest(), total


def _private_sqlite(out_fd: int, limits: Limits) -> sqlite3.Connection:
    fd = os.open("inode-index.sqlite", CREATE_FLAGS, 0o600, dir_fd=out_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise AuditError("sqlite_index_identity_invalid")
    finally:
        os.close(fd)
    db = sqlite3.connect(f"/proc/self/fd/{out_fd}/inode-index.sqlite", timeout=0.25)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=FULL")
    db.execute(f"PRAGMA cache_size=-{SQLITE_CACHE_KIB}")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute(f"PRAGMA max_page_count={limits.index_bytes // 4096}")
    if int(db.execute("PRAGMA page_size").fetchone()[0]) != 4096:
        db.close()
        raise AuditError("sqlite_page_size_unexpected")
    db.execute("CREATE TABLE paths(path BLOB PRIMARY KEY,sig BLOB NOT NULL,root_name BLOB NOT NULL) WITHOUT ROWID")
    db.execute("CREATE TABLE inodes(dev INTEGER,ino INTEGER,mode INTEGER,blocks INTEGER,nlink INTEGER,occurrences INTEGER,root_name BLOB,shared INTEGER,PRIMARY KEY(dev,ino)) WITHOUT ROWID")
    db.execute("CREATE TABLE links_pass2(dev INTEGER,ino INTEGER,nlink INTEGER,occurrences INTEGER,PRIMARY KEY(dev,ino)) WITHOUT ROWID")
    return db


def _allocation_summary(db: sqlite3.Connection, limits: Limits) -> dict[str, object]:
    page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
    allocated = page_count * page_size
    if allocated > limits.index_bytes:
        raise AuditError("sqlite_index_size_limit")
    by_root = []
    for root_name, count, blocks in db.execute(
        "SELECT root_name,COUNT(*),SUM(blocks) FROM inodes WHERE shared=0 GROUP BY root_name ORDER BY root_name"
    ):
        by_root.append({"root_id": base64.b64encode(root_name).decode("ascii"), "unique_inode_count": int(count), "allocated_bytes": int(blocks) * BYTES_PER_BLOCK})
    return {
        "unique_visible_inode_count": int(db.execute("SELECT COUNT(*) FROM inodes").fetchone()[0]),
        "unique_visible_allocated_bytes": int(db.execute("SELECT COALESCE(SUM(blocks),0)*? FROM inodes", (BYTES_PER_BLOCK,)).fetchone()[0]),
        "cross_top_root_inode_count": int(db.execute("SELECT COUNT(*) FROM inodes WHERE shared=1").fetchone()[0]),
        "exclusive_allocated_bytes_by_top_root": by_root,
        "index_bytes": allocated,
        "hardlink_completeness": "regular files only; directories excluded; links outside /dev/shm can make the observed count incomplete",
        "allocation_unit": "st_blocks * 512; filesystem allocation, not resident RAM",
    }


def _inventory_names(out_fd: int) -> tuple[list[str], bool]:
    names: list[str] = []
    unexpected = False
    with os.scandir(out_fd) as iterator:
        for entry in iterator:
            if len(names) >= 8:
                unexpected = True
                break
            names.append(entry.name)
            if entry.name not in {"inventory.jsonl", "inode-index.sqlite", "summary.json"}:
                unexpected = True
    return names, unexpected


def _run_scan(shm_fd: int, shm_mount_id: int, fdinfo_fd: int, out_fd: int,
              limits: Limits = DEFAULT_LIMITS, meminfo_path: str = "/proc/meminfo") -> dict[str, object]:
    started_at = utc_now()
    started_mono = time.monotonic()
    deadline = started_mono + limits.wall_seconds
    errors = Errors()
    samples: dict[str, object] = {}
    passes: list[dict[str, object]] = []
    inventory_fd = _create_output_file(out_fd, "inventory.jsonl")
    db: sqlite3.Connection | None = None
    allocation: dict[str, object] = {}
    try:
        try:
            samples["before"] = read_memory_sample(meminfo_path)
        except (AuditError, OSError) as exc:
            errors.add(exc.category if isinstance(exc, AuditError) else "meminfo_read_error", getattr(exc, "errno", None))
        db = _private_sqlite(out_fd, limits)
        pass1 = _scan_pass(shm_fd, shm_mount_id, fdinfo_fd, db, inventory_fd, 1, deadline, limits)
        passes.append(pass1)
        os.fsync(inventory_fd)
        pass2 = _scan_pass(shm_fd, shm_mount_id, fdinfo_fd, db, None, 2, deadline, limits)
        passes.append(pass2)
        allocation = _allocation_summary(db, limits)
        db.commit()
    except (AuditError, OSError, sqlite3.Error, OverflowError) as exc:
        errors.add(exc.category if isinstance(exc, AuditError) else type(exc).__name__, getattr(exc, "errno", None))
    finally:
        if db is not None:
            try:
                db.close()
            except (sqlite3.Error, OSError) as exc:
                errors.add("sqlite_close_error", getattr(exc, "errno", None))
        os.close(inventory_fd)
    try:
        samples["after"] = read_memory_sample(meminfo_path)
    except (AuditError, OSError) as exc:
        errors.add(exc.category if isinstance(exc, AuditError) else "meminfo_read_error", getattr(exc, "errno", None))
    names, unexpected = _inventory_names(out_fd)
    if unexpected:
        errors.add("unexpected_output_entry")
    try:
        inventory_fd = os.open("inventory.jsonl", READ_FLAGS, dir_fd=out_fd)
        try:
            inv_sha, inv_size = _hash_fd(inventory_fd, limits.report_bytes)
        finally:
            os.close(inventory_fd)
        index_fd = os.open("inode-index.sqlite", READ_FLAGS, dir_fd=out_fd)
        try:
            index_sha, index_size = _hash_fd(index_fd, limits.index_bytes)
        finally:
            os.close(index_fd)
    except (AuditError, OSError) as exc:
        errors.add(exc.category if isinstance(exc, AuditError) else "artifact_readback_error", getattr(exc, "errno", None))
        inv_sha, inv_size, index_sha, index_size = None, None, None, None
    if len(passes) != 2 or any(item.get("status") != "COMPLETE" for item in passes):
        errors.add("scan_incomplete")
    status = "COMPLETE_METADATA" if not errors.rows and not errors.suppressed else "UNKNOWN"
    return {
        "schema": TOOL_SCHEMA,
        "status": status,
        "deletion_authority": False,
        "started_at_utc": started_at,
        "ended_at_utc": utc_now(),
        "elapsed_seconds": round(time.monotonic() - started_mono, 3),
        "limits": limits.as_dict(),
        "memory_counters": samples,
        "memory_counters_note": "bounded MemAvailable/Shmem observations before and after scan; not an admission decision or allocation ownership proof",
        "passes": passes,
        "allocation": allocation,
        "artifacts": {
            "inventory": {"name": "inventory.jsonl", "size": inv_size, "sha256": inv_sha},
            "index": {"name": "inode-index.sqlite", "size": index_size, "sha256": index_sha},
            "output_names": names,
        },
        "errors": errors.rows,
        "suppressed_error_count": errors.suppressed,
        "scope": "metadata-only visible /dev/shm inodes; no contents, symlink targets, foreign processes, or mutations",
        "limitations": [
            "filesystem allocation is st_blocks * 512, not resident RAM or total tmpfs pages",
            "deleted-open inodes and links outside /dev/shm are not fully represented",
            "names and metadata do not establish owner, lease, or deletion authority",
        ],
    }


def _write_summary(out_fd: int, summary: dict[str, object]) -> tuple[int, str, str]:
    payload = _json_bytes(summary)
    effective_status = str(summary.get("status", "UNKNOWN"))
    if len(payload) > 256 * 1024:
        payload = _json_bytes({
            "schema": TOOL_SCHEMA,
            "status": "UNKNOWN",
            "deletion_authority": False,
            "errors": [{"category": "summary_size_limit", "errno": None}],
            "source_summary_sha256": hashlib.sha256(payload).hexdigest(),
            "summary_limit_bytes": 256 * 1024,
        })
        effective_status = "UNKNOWN"
    fd = _create_output_file(out_fd, "summary.json")
    try:
        _write_all(fd, payload)
        os.fsync(fd)
        written = os.fstat(fd)
    finally:
        os.close(fd)
    read_fd = os.open("summary.json", READ_FLAGS, dir_fd=out_fd)
    try:
        before = os.fstat(read_fd)
        if _stat_identity(before) != _stat_identity(written):
            raise AuditError("summary_readback_identity_mismatch")
        readback = bytearray()
        while len(readback) <= 256 * 1024:
            chunk = os.read(read_fd, min(64 * 1024, 256 * 1024 + 1 - len(readback)))
            if not chunk:
                break
            readback.extend(chunk)
        if bytes(readback) != payload or _stat_identity(before) != _stat_identity(os.fstat(read_fd)):
            raise AuditError("summary_readback_mismatch")
    finally:
        os.close(read_fd)
    return len(payload), hashlib.sha256(payload).hexdigest(), effective_status


def _write_terminal(out_fd: int, status: str, summary_sha256: str | None,
                    chain_identity: list[tuple[int, ...]], errors: list[dict[str, object]]) -> str:
    terminal = {
        "schema": TOOL_SCHEMA,
        "status": status,
        "summary_sha256": summary_sha256,
        "captured_at_utc": utc_now(),
        "output_parent_chain_identity": [
            {"dev": row[0], "ino": row[1], "mode": row[2], "uid": row[3], "gid": row[4], "nlink": row[5]}
            for row in chain_identity
        ],
        "errors": errors[:MAX_RECORDED_ERRORS],
        "deletion_authority": False,
    }
    payload = _json_bytes(terminal)
    fd = _create_output_file(out_fd, "terminal.json")
    try:
        _write_all(fd, payload)
        os.fsync(fd)
        written = os.fstat(fd)
    finally:
        os.close(fd)
    read_fd = os.open("terminal.json", READ_FLAGS, dir_fd=out_fd)
    try:
        read = bytearray()
        while len(read) <= 256 * 1024:
            chunk = os.read(read_fd, min(64 * 1024, 256 * 1024 + 1 - len(read)))
            if not chunk:
                break
            read.extend(chunk)
        if bytes(read) != payload or _stat_identity(written) != _stat_identity(os.fstat(read_fd)):
            raise AuditError("terminal_readback_mismatch")
    finally:
        os.close(read_fd)
    return hashlib.sha256(payload).hexdigest()


def _write_failure(out_fd: int, stage: str, exc: BaseException) -> str:
    """Best-effort immutable UNKNOWN projection for a late process failure."""
    payload = _json_bytes({
        "schema": TOOL_SCHEMA,
        "status": "UNKNOWN",
        "stage": stage,
        "category": type(exc).__name__,
        "errno": getattr(exc, "errno", None),
        "deletion_authority": False,
    })
    fd = _create_output_file(out_fd, "failure.json")
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.fsync(out_fd)
    except OSError:
        # The nonzero process exit remains authoritative if directory
        # durability cannot be established for this secondary projection.
        pass
    return hashlib.sha256(payload).hexdigest()


def execution_disposition(exit_code: int, terminal_status: object, failure_present: bool) -> str:
    """A positive terminal alone is insufficient without successful exit."""
    if exit_code == 0 and terminal_status == "COMPLETE_METADATA" and not failure_present:
        return "COMPLETE_METADATA"
    return "UNKNOWN"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="new private directory; parent must be trusted and non-writable by group/other")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not sys.platform.startswith("linux") or not PATH_FLAGS or not hasattr(os, "O_NOFOLLOW"):
        print("tmpfs audit unavailable: required Linux no-follow primitives are missing", file=sys.stderr)
        return 2
    output_path = Path(args.output_dir)
    try:
        parent_chain, out_fd = create_output_dir(output_path)
    except (OSError, AuditError) as exc:
        print(f"tmpfs audit output setup failed: {getattr(exc, 'category', type(exc).__name__)}", file=sys.stderr)
        return 2
    try:
        stage = "resource_limits"
        resource.setrlimit(resource.RLIMIT_CPU, (DEFAULT_LIMITS.cpu_seconds, DEFAULT_LIMITS.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (DEFAULT_LIMITS.address_space_bytes, DEFAULT_LIMITS.address_space_bytes))
        summary: dict[str, object] = {
            "schema": TOOL_SCHEMA,
            "status": "UNKNOWN",
            "deletion_authority": False,
            "errors": [],
            "scope": "metadata-only visible /dev/shm inodes",
        }
        try:
            stage = "open_fixed_sources"
            fdinfo_chain, fdinfo_fd = _open_own_fdinfo()
            shm_chain, shm_fd, _ = open_shm_root()
            fdinfo_before = [_stat_identity(os.fstat(fd)) for fd in fdinfo_chain]
            shm_before = [_stat_identity(os.fstat(fd)) for fd in shm_chain]
            shm_mount_id = _fd_mount_id(shm_fd, fdinfo_fd)
            stage = "scan_and_artifact_validation"
            summary = _run_scan(shm_fd, shm_mount_id, fdinfo_fd, out_fd)
            if [_stat_identity(os.fstat(fd)) for fd in fdinfo_chain] != fdinfo_before:
                summary["status"] = "UNKNOWN"
                summary.setdefault("errors", []).append({"category": "own_fdinfo_chain_identity_drift", "errno": None})
            if [_stat_identity(os.fstat(fd)) for fd in shm_chain] != shm_before:
                summary["status"] = "UNKNOWN"
                summary.setdefault("errors", []).append({"category": "shm_chain_identity_drift", "errno": None})
        except (AuditError, OSError, sqlite3.Error) as exc:
            summary["errors"] = [{"category": getattr(exc, "category", type(exc).__name__), "errno": getattr(exc, "errno", None)}]
        parent_identity_before = [_stat_identity(os.fstat(fd)) for fd in parent_chain]
        summary["output_parent_chain_identity"] = [
            {"dev": row[0], "ino": row[1], "mode": row[2], "uid": row[3], "gid": row[4], "nlink": row[5]}
            for row in parent_identity_before
        ]
        stage = "summary_write_and_readback"
        if [_stat_identity(os.fstat(fd)) for fd in parent_chain] != parent_identity_before:
            summary["status"] = "UNKNOWN"
            summary.setdefault("errors", []).append({"category": "output_parent_chain_identity_drift", "errno": None})
        summary_size, summary_sha, summary_status = _write_summary(out_fd, summary)
        if summary_status != summary.get("status"):
            summary["status"] = "UNKNOWN"
        os.fsync(out_fd)
        chain_stable = [_stat_identity(os.fstat(fd)) for fd in parent_chain] == parent_identity_before
        terminal_status = summary_status if chain_stable else "UNKNOWN"
        stage = "terminal_write_and_readback"
        terminal_hash = _write_terminal(
            out_fd,
            terminal_status,
            summary_sha,
            parent_identity_before,
            list(summary.get("errors", [])),
        )
        stage = "final_directory_fsync"
        os.fsync(out_fd)
        print(f"TMPFS_ALLOCATION_AUDIT status={terminal_status} summary_bytes={summary_size} summary_sha256={summary_sha} terminal_sha256={terminal_hash}")
        return 0 if execution_disposition(0, terminal_status, False) == "COMPLETE_METADATA" else 2
    except (AuditError, OSError, sqlite3.Error, OverflowError) as exc:
        print(f"tmpfs audit incomplete: {getattr(exc, 'category', type(exc).__name__)}", file=sys.stderr)
        try:
            _write_failure(out_fd, stage, exc)
        except (AuditError, OSError):
            pass
        return 2
    finally:
        for name in ("fdinfo_chain", "shm_chain"):
            for fd in reversed(locals().get(name, [])):
                try:
                    os.close(fd)
                except OSError:
                    pass
        os.close(out_fd)
        for fd in reversed(parent_chain):
            os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
