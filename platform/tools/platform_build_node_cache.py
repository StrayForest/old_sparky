#!/usr/bin/env python3
"""Remove only the exact pinned build-Node cache while maintenance locks are held.

This module deliberately has no CLI and does not acquire locks. Its sole
caller must hold the canonical release, retained-load, build-output and
live-QA locks in that order for the whole call. The caller owns that lock
boundary; this helper revalidates the cache and process-reference state before
removing anything.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
from typing import Any, Callable, Mapping

try:
    from . import platform_live_qa_guard as live_qa_guard
except ImportError:  # Direct execution/import from the tools directory.
    import platform_live_qa_guard as live_qa_guard


PROC_ROOT = Path("/proc")
MAX_MANIFEST_BYTES = 8 * 1024
MAX_CACHE_ENTRIES = 100_000
MAX_CACHE_BYTES = 1_000_000_000
MAX_PROC_MAP_LINE_BYTES = 1024 * 1024
_MISSING = object()


class BuildNodeCacheError(RuntimeError):
    """Closed failure for unsafe or changing pinned build-cache state."""


@dataclass(frozen=True, slots=True)
class _CacheSnapshot:
    parent_identity: tuple[int, ...]
    root_identity: tuple[int, ...]
    manifest_bytes: bytes
    tree_sha256: str
    entries: tuple[tuple[str, tuple[int, ...]], ...]
    total_bytes: int

    @property
    def inode_identities(self) -> frozenset[tuple[int, int]]:
        return frozenset(
            (identity[0], identity[1]) for _relative, identity in self.entries
        ) | frozenset({(self.root_identity[0], self.root_identity[1])})


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _read_manifest(path: Path) -> tuple[bytes, dict[str, Any]]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_gid != 0
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o444
            or before.st_size > MAX_MANIFEST_BYTES
        ):
            raise BuildNodeCacheError("pinned build Node manifest metadata is unsafe")
        descriptor = os.open(path, flags)
    except BuildNodeCacheError:
        raise
    except OSError as exc:
        raise BuildNodeCacheError("pinned build Node manifest is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise BuildNodeCacheError("pinned build Node manifest changed while opening")
        raw = bytearray()
        while len(raw) <= MAX_MANIFEST_BYTES:
            chunk = os.read(descriptor, min(4096, MAX_MANIFEST_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if len(raw) > MAX_MANIFEST_BYTES or _identity(after) != _identity(opened):
            raise BuildNodeCacheError("pinned build Node manifest changed while reading")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(
            bytes(raw).decode("utf-8"), object_pairs_hook=_strict_json_object
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise BuildNodeCacheError("pinned build Node manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise BuildNodeCacheError("pinned build Node manifest schema is invalid")
    return bytes(raw), payload


def _directory_identity(path: Path, *, mode: int) -> tuple[int, ...]:
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BuildNodeCacheError("pinned build Node cache path is unavailable") from exc
    if (
        resolved != path
        or not stat.S_ISDIR(before.st_mode)
        or before.st_uid != 0
        or before.st_gid != 0
        or before.st_nlink < 2
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_mode & 0o7000
    ):
        raise BuildNodeCacheError("pinned build Node cache directory is unsafe")
    return _identity(before)


def _cache_snapshot(build_root: Path) -> _CacheSnapshot | None:
    parent_identity = _directory_identity(build_root, mode=0o755)
    target = build_root / f"node-v{live_qa_guard.NODE_VERSION}"
    try:
        target_metadata = target.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BuildNodeCacheError("pinned build Node cache cannot be inspected") from exc
    if (
        not stat.S_ISDIR(target_metadata.st_mode)
        or target_metadata.st_uid != 0
        or target_metadata.st_gid != 0
        or target_metadata.st_nlink < 2
        or stat.S_IMODE(target_metadata.st_mode) != 0o555
        or target_metadata.st_mode & 0o7000
    ):
        raise BuildNodeCacheError("pinned build Node cache root is unsafe")
    try:
        live_qa_guard._validate_cache_tree_permissions(target)
        manifest_bytes, manifest = _read_manifest(target / ".manifest.json")
        if (
            set(manifest)
            != {"version", "node_archive_sha256", "tree_sha256"}
            or type(manifest.get("version")) is not int
            or manifest.get("version") != 1
            or manifest.get("node_archive_sha256")
            != live_qa_guard.NODE_ARCHIVE_SHA256
            or not isinstance(manifest.get("tree_sha256"), str)
            or len(manifest["tree_sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in manifest["tree_sha256"])
        ):
            raise BuildNodeCacheError("pinned build Node manifest schema is invalid")
        tree_sha256 = live_qa_guard._tree_digest(
            target, ignored_relatives=frozenset({Path(".manifest.json")})
        )
        if manifest["tree_sha256"] != tree_sha256:
            raise BuildNodeCacheError("pinned build Node tree does not match its manifest")
        paths = live_qa_guard._walk_nofollow(target)
        if len(paths) > MAX_CACHE_ENTRIES:
            raise BuildNodeCacheError("pinned build Node cache has too many entries")
        entries: list[tuple[str, tuple[int, ...]]] = []
        total_bytes = target_metadata.st_blocks * 512
        logical_bytes = 0
        for path in paths:
            metadata = path.lstat()
            if metadata.st_dev != target_metadata.st_dev:
                raise BuildNodeCacheError("pinned build Node cache crosses devices")
            total_bytes += metadata.st_blocks * 512
            logical_bytes += metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0
            entries.append((path.relative_to(target).as_posix(), _identity(metadata)))
        if total_bytes > MAX_CACHE_BYTES or logical_bytes > MAX_CACHE_BYTES:
            raise BuildNodeCacheError("pinned build Node cache exceeds its size bound")
    except BuildNodeCacheError:
        raise
    except (OSError, live_qa_guard.GuardError) as exc:
        raise BuildNodeCacheError("pinned build Node cache failed validation") from exc
    return _CacheSnapshot(
        parent_identity=parent_identity,
        root_identity=_identity(target_metadata),
        manifest_bytes=manifest_bytes,
        tree_sha256=tree_sha256,
        entries=tuple(entries),
        total_bytes=total_bytes,
    )


def _path_is_under_cache(value: str, cache_path: str) -> bool:
    normalized = value.removesuffix(" (deleted)")
    return normalized == cache_path or normalized.startswith(cache_path + "/")


def _process_references(
    snapshot: _CacheSnapshot, *, build_root: Path, proc_root: Path
) -> bool:
    """Return true on a live process path/inode reference; uncertain reads fail closed."""

    cache_path = str(build_root / f"node-v{live_qa_guard.NODE_VERSION}")
    identities = snapshot.inode_identities
    try:
        pids = tuple(os.scandir(proc_root))
    except OSError as exc:
        raise BuildNodeCacheError("process references cannot be checked") from exc
    for entry in pids:
        if not entry.name.isdecimal():
            continue
        pid_root = proc_root / entry.name
        candidates = [pid_root / name for name in ("exe", "cwd", "root")]
        fd_root = pid_root / "fd"
        try:
            candidates.extend(fd_root.iterdir())
        except FileNotFoundError:
            # A process can have no fd directory while still having mapped
            # files. Keep checking exe/cwd/root and, crucially, maps below.
            pass
        except PermissionError as exc:
            raise BuildNodeCacheError("process references cannot be checked") from exc
        except OSError as exc:
            if exc.errno not in {2, 3}:
                raise BuildNodeCacheError("process references cannot be checked") from exc
        for candidate in candidates:
            try:
                link_value = os.readlink(candidate)
                metadata = candidate.stat()
            except FileNotFoundError:
                # A single fd can close during enumeration while the process
                # remains alive. That does not establish that its mappings are
                # gone, so continue checking the remaining references and maps.
                continue
            except PermissionError as exc:
                raise BuildNodeCacheError("process references cannot be checked") from exc
            except OSError as exc:
                if exc.errno in {2, 3}:
                    continue
                raise BuildNodeCacheError("process references cannot be checked") from exc
            if (
                (metadata.st_dev, metadata.st_ino) in identities
                or _path_is_under_cache(link_value, cache_path)
            ):
                return True
        maps_path = pid_root / "maps"
        try:
            descriptor = os.open(
                maps_path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            try:
                consumed = 0
                pending = b""

                def mapped_inode_reference(line: bytes) -> bool:
                    if len(line) > MAX_PROC_MAP_LINE_BYTES:
                        raise BuildNodeCacheError("process reference map line exceeds its bound")
                    fields = line.split(maxsplit=5)
                    if len(fields) < 5:
                        return False
                    device = fields[3]
                    inode_text = fields[4]
                    if not inode_text.isdigit() or b":" not in device:
                        return False
                    major_text, minor_text = device.split(b":", 1)
                    try:
                        device_number = os.makedev(
                            int(major_text, 16), int(minor_text, 16)
                        )
                        inode_number = int(inode_text)
                    except (ValueError, OverflowError):
                        raise BuildNodeCacheError("process map is malformed") from None
                    if (device_number, inode_number) in identities:
                        return True
                    if len(fields) == 6:
                        mapped_path = fields[5].decode("utf-8", errors="replace")
                        if _path_is_under_cache(mapped_path, cache_path):
                            return True
                    return False

                while chunk := os.read(descriptor, 64 * 1024):
                    consumed += len(chunk)
                    if consumed > 8 * 1024 * 1024:
                        raise BuildNodeCacheError("process reference map exceeds its bound")
                    lines = (pending + chunk).split(b"\n")
                    pending = lines.pop()
                    if len(pending) > MAX_PROC_MAP_LINE_BYTES:
                        raise BuildNodeCacheError("process reference map line exceeds its bound")
                    if any(mapped_inode_reference(line) for line in lines):
                        return True
                if pending and mapped_inode_reference(pending):
                    return True
            finally:
                os.close(descriptor)
        except FileNotFoundError:
            # ENOENT is a normal race only if the process itself has exited.
            # If its proc directory remains, the maps check is unavailable and
            # eviction must fail closed.
            try:
                pid_root.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise BuildNodeCacheError(
                    "process references cannot be checked"
                ) from exc
            raise BuildNodeCacheError("process map is unavailable")
        except PermissionError as exc:
            raise BuildNodeCacheError("process references cannot be checked") from exc
        except OSError as exc:
            if exc.errno in {2, 3}:
                try:
                    pid_root.lstat()
                except FileNotFoundError:
                    continue
                except OSError as stat_exc:
                    raise BuildNodeCacheError(
                        "process references cannot be checked"
                    ) from stat_exc
                raise BuildNodeCacheError("process map is unavailable") from exc
            raise BuildNodeCacheError("process references cannot be checked") from exc
    return False


def _eviction_record(
    snapshot: _CacheSnapshot | None, *, status: str
) -> dict[str, object]:
    return {
        "schema": 1,
        "event": "build_node_cache_eviction",
        "node_version": live_qa_guard.NODE_VERSION,
        "cache_dev": snapshot.root_identity[0] if snapshot is not None else None,
        "cache_ino": snapshot.root_identity[1] if snapshot is not None else None,
        "manifest_tree_sha256": snapshot.tree_sha256 if snapshot is not None else None,
        "total_bytes": snapshot.total_bytes if snapshot is not None else 0,
        "status": status,
    }


def _evict_cache(
    build_root: Path,
    proc_root: Path,
    *,
    write_intent: Callable[[Mapping[str, object]], None],
    write_completion: Callable[[Mapping[str, object]], None],
) -> dict[str, Any]:
    """Implementation seam; only the public canonical wrapper is for callers."""

    if os.geteuid() != 0:
        raise BuildNodeCacheError("root privileges are required")
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise BuildNodeCacheError("safe recursive removal is unavailable")
    first = _cache_snapshot(build_root)
    if first is None:
        intent = _eviction_record(None, status="intent")
        try:
            write_intent(intent)
        except Exception as exc:
            raise BuildNodeCacheError(
                "pinned build Node eviction intent was not durable"
            ) from exc
        if _cache_snapshot(build_root) is not None:
            raise BuildNodeCacheError("pinned build Node cache appeared after intent")
        completion = {
            **_eviction_record(None, status="already-absent"),
            "reclaimed_bytes": 0,
        }
        try:
            write_completion(completion)
        except Exception as exc:
            raise BuildNodeCacheError(
                "absent pinned build Node completion receipt failed"
            ) from exc
        return {
            "status": "already-absent",
            "node_version": live_qa_guard.NODE_VERSION,
            "reclaimed_bytes": 0,
        }
    if _process_references(first, build_root=build_root, proc_root=proc_root):
        raise BuildNodeCacheError("pinned build Node cache is in use")
    second = _cache_snapshot(build_root)
    if second != first or _process_references(
        second, build_root=build_root, proc_root=proc_root
    ):
        raise BuildNodeCacheError("pinned build Node cache changed during preflight")
    intent = _eviction_record(second, status="intent")
    try:
        write_intent(intent)
    except Exception as exc:
        raise BuildNodeCacheError("pinned build Node eviction intent was not durable") from exc

    # Receipt creation is outside the filesystem target; revalidate everything
    # after it and immediately before mutation.
    third = _cache_snapshot(build_root)
    if third != second or _process_references(
        third, build_root=build_root, proc_root=proc_root
    ):
        raise BuildNodeCacheError("pinned build Node cache changed after intent")
    target = build_root / f"node-v{live_qa_guard.NODE_VERSION}"
    parent_now = _directory_identity(build_root, mode=0o755)
    try:
        current = target.lstat()
    except OSError as exc:
        raise BuildNodeCacheError("pinned build Node cache changed before removal") from exc
    if parent_now != third.parent_identity or _identity(current) != third.root_identity:
        raise BuildNodeCacheError("pinned build Node cache changed before removal")
    try:
        shutil.rmtree(target)
        descriptor = os.open(
            build_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if os.path.lexists(target):
            raise BuildNodeCacheError("pinned build Node cache remains after removal")
    except BuildNodeCacheError:
        raise
    except OSError as exc:
        raise BuildNodeCacheError("pinned build Node cache removal failed") from exc
    result = {
        "status": "removed",
        "node_version": live_qa_guard.NODE_VERSION,
        "manifest_tree_sha256": third.tree_sha256,
        "reclaimed_bytes": third.total_bytes,
        "regeneration": "pinned_archive_required",
    }
    completion = {
        **_eviction_record(third, status="removed"),
        "reclaimed_bytes": third.total_bytes,
        "regeneration": "pinned_archive_required",
    }
    try:
        write_completion(completion)
    except Exception as exc:
        raise BuildNodeCacheError(
            "pinned build Node was removed but completion receipt failed"
        ) from exc
    return result


def evict_pinned_build_node_cache(
    *,
    write_intent: Callable[[Mapping[str, object]], None],
    write_completion: Callable[[Mapping[str, object]], None],
) -> dict[str, Any]:
    """Evict the validated pinned cache; caller must hold all four canonical locks.

    This function accepts no filesystem path and exposes no standalone CLI. It
    removes only `/var/lib/oldsparky-build/node-v26.3.1`, after validating its
    immutable manifest/tree, checking every process reference, and repeating
    the full identity snapshot. A subsequent `prepare_build_node()` must fetch
    and validate the same pinned archive before it can publish the cache again.
    """

    build_root = live_qa_guard.BUILD_NODE_ROOT
    if build_root != Path("/var/lib/oldsparky-build"):
        raise BuildNodeCacheError("build Node cache root is not canonical")
    return _evict_cache(
        build_root,
        PROC_ROOT,
        write_intent=write_intent,
        write_completion=write_completion,
    )
