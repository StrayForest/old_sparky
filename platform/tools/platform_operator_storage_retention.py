#!/usr/bin/env python3
"""Prune only known, reproducible operator and task artifacts on the origin host."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import Iterable


MAIN_CHECKOUT = Path("/root/old_sparky")
HOME = Path("/root")
GIT_DIR = MAIN_CHECKOUT / ".git"
NPM_ROOT = HOME / ".npm"
CACHE_ROOT = HOME / ".cache"
VSCODE_SERVERS = HOME / ".vscode-server" / "cli" / "servers"
TMP_ROOT = Path("/tmp")
LOCK_PATH = Path("/run/oldsparky-operator-storage-retention.lock")
EXPECTED_OWNER_UID = 0

CODEX_ARTIFACT_RE = re.compile(r"^codex-performance-(\d{8})$")
VSCODE_SERVER_RE = re.compile(r"^(?:Stable|Insiders)-[0-9a-f]{40}$")
TMP_BUILD_RE = re.compile(r"^oldsparky-web-hermetic\.[A-Za-z0-9_-]+$")
WORKTREE_DEPS = (
    Path("platform/.venv_platform"),
    Path("platform/.cache/platform-ci-venv"),
    Path("platform/apps/platform_web/node_modules"),
    Path("platform/apps/platform_web/.next"),
    Path("platform/apps/platform_web/test-results"),
    Path("platform/apps/platform_web/playwright-report"),
)
WORKTREE_SCAN_ROOTS = (
    (Path("/root"), "old_sparky-"),
    (Path("/root/oldsparky-worktrees"), None),
    (Path("/root/.codex/worktrees"), None),
    (Path("/dev/shm"), "oldsparky-"),
)
CACHE_CHILDREN = (
    "ms-playwright",
    "pip",
    "pip-audit",
    "gh",
    "mesa_shader_cache",
    "gstreamer-1.0",
    "python-entrypoints",
    "Microsoft",
)
BUSY_PROCESS_NAMES = {
    "node",
    "npm",
    "npx",
    "chromium",
    "chrome",
    "chrome-headless-shell",
    "firefox",
    "webkit",
    "codex",
}
CACHE_BUSY_PROCESS_NAMES = {
    "ms-playwright": BUSY_PROCESS_NAMES | {"python", "python3", "python3.12"},
    "pip": {"python", "python3", "python3.12", "pip"},
    "pip-audit": {"python", "python3", "python3.12", "pip-audit"},
    "gh": {"gh"},
}


class RetentionError(RuntimeError):
    """A host path changed or failed a closed cleanup check."""


@dataclass(frozen=True)
class Candidate:
    category: str
    path: Path
    identity: tuple[int, int, int, int]
    allocated_bytes: int
    worktree_branch: str | None = None


def _run_git(*args: str, cwd: Path | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd or MAIN_CHECKOUT), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RetentionError("git metadata check failed") from exc
    return result.stdout


def _path_reference_matches(target: Path, link: str) -> bool:
    link = link.removesuffix(" (deleted)")
    root = str(target)
    return link == root or link.startswith(root + os.sep)


def _is_root_process(process: Path) -> bool:
    """Use only the effective UID from proc status; never read command lines."""
    try:
        for line in (process / "status").read_text(encoding="ascii").splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[2]) == 0
    except (OSError, ValueError, IndexError):
        return False
    return False


def process_references(target: Path, process_names: set[str] | None = None) -> bool:
    """Check process cwd, executable, root, descriptors and maps without argv/env."""
    selected_names = process_names or set()
    try:
        pids = os.listdir("/proc")
    except OSError as exc:
        raise RetentionError("process inventory unavailable") from exc
    for pid in pids:
        if not pid.isdecimal():
            continue
        process = Path("/proc") / pid
        try:
            comm = (process / "comm").read_text(encoding="ascii").strip()
            if (
                int(pid) != os.getpid()
                and comm in selected_names
                and _is_root_process(process)
            ):
                return True
        except OSError:
            continue
        for name in ("cwd", "exe", "root"):
            try:
                if _path_reference_matches(target, os.readlink(process / name)):
                    return True
            except OSError:
                pass
        try:
            descriptors = os.listdir(process / "fd")
        except OSError:
            descriptors = ()
        for descriptor in descriptors:
            try:
                if _path_reference_matches(
                    target, os.readlink(process / "fd" / descriptor)
                ):
                    return True
            except OSError:
                continue
        try:
            with (process / "maps").open(encoding="utf-8", errors="replace") as stream:
                if any(str(target) in line for line in stream):
                    return True
        except OSError:
            pass
    return False


def _tree_allocated_bytes(path: Path, *, owner_uid: int | None = None) -> int:
    """Validate a single-device tree without following links and count allocated bytes."""
    owner_uid = EXPECTED_OWNER_UID if owner_uid is None else owner_uid
    try:
        root_info = path.lstat()
    except OSError as exc:
        raise RetentionError("candidate unavailable") from exc
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != owner_uid:
        raise RetentionError("candidate root metadata is unsafe")
    device = root_info.st_dev
    total = root_info.st_blocks * 512
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise RetentionError("candidate tree is unreadable") from exc
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RetentionError("candidate tree changed during scan") from exc
            if info.st_uid != owner_uid or info.st_dev != device:
                raise RetentionError("candidate tree ownership or device is unsafe")
            if stat.S_ISDIR(info.st_mode):
                stack.append(Path(entry.path))
                total += info.st_blocks * 512
            elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                total += info.st_blocks * 512
            else:
                raise RetentionError("candidate tree contains a special file")
    return total


def _identity(path: Path) -> tuple[int, int, int, int]:
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)


def _candidate(category: str, path: Path) -> Candidate | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != EXPECTED_OWNER_UID:
        return None
    allocated = _tree_allocated_bytes(path)
    return Candidate(category, path, _identity(path), allocated)


def _managed_worktree(path: Path) -> bool:
    if path == MAIN_CHECKOUT:
        return False
    if path.parent == Path("/root") and path.name.startswith("old_sparky-"):
        return True
    if path.parent in {
        Path("/root/oldsparky-worktrees"),
        Path("/root/.codex/worktrees"),
    }:
        return True
    if path.parent == Path("/dev/shm"):
        return path.name.startswith("oldsparky-")
    return False


def _same_common_directory(path: Path) -> bool:
    try:
        expected = Path(
            os.fsdecode(
                _run_git("rev-parse", "--path-format=absolute", "--git-common-dir")
            ).strip()
        )
        actual = Path(
            os.fsdecode(
                _run_git(
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                    cwd=path,
                )
            ).strip()
        )
        return expected.resolve(strict=True) == actual.resolve(strict=True)
    except (OSError, RetentionError):
        return False


def _worktree_records() -> list[tuple[Path, str | None, bool]]:
    output = _run_git("worktree", "list", "--porcelain")
    records: list[tuple[Path, str | None, bool]] = []
    for block in output.decode("utf-8", errors="strict").strip().split("\n\n"):
        if not block:
            continue
        path: Path | None = None
        branch: str | None = None
        locked = False
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = Path(line[9:])
            elif line.startswith("branch refs/heads/"):
                branch = line.removeprefix("branch refs/heads/")
            elif line == "locked" or line.startswith("locked "):
                locked = True
        if path is not None:
            records.append((path, branch, locked))
    return records


def _worktree_status(path: Path) -> tuple[bool, tuple[Path, ...]]:
    output = _run_git(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        cwd=path,
    )
    records = [record for record in output.split(b"\0") if record]
    allowed = tuple(WORKTREE_DEPS)
    removable: set[Path] = set()
    for record in records:
        if len(record) < 4 or record[2:3] != b" ":
            return False, ()
        status = record[:2].decode("ascii", errors="replace")
        try:
            relative = Path(os.fsdecode(record[3:]).rstrip("/"))
        except (TypeError, ValueError):
            return False, ()
        if status not in {"??", "!!"}:
            return False, ()
        if relative.is_absolute() or ".." in relative.parts:
            return False, ()
        if relative == Path("platform/.cache"):
            cache_dir = path / relative
            try:
                children = {entry.name for entry in cache_dir.iterdir()}
            except OSError:
                return False, ()
            if children != {"platform-ci-venv"}:
                return False, ()
            removable.add(Path("platform/.cache/platform-ci-venv"))
            continue
        matched = next(
            (dependency for dependency in allowed if relative == dependency or dependency in relative.parents),
            None,
        )
        if matched is None:
            return False, ()
        removable.add(matched)
    return True, tuple(sorted(removable, key=str))


def _worktree_candidate(path: Path, branch: str) -> Candidate:
    allocated = _tree_allocated_bytes(path)
    return Candidate("linked_worktrees", path, _identity(path), allocated, branch)


def build_plan(
    now: datetime | None = None,
) -> tuple[list[Candidate], dict[str, int], dict[str, int]]:
    now = now or datetime.now(timezone.utc)
    candidates: list[Candidate] = []
    skipped = {
        "busy": 0,
        "unsafe": 0,
        "dirty_or_unknown_worktrees": 0,
        "locked_worktrees": 0,
        "detached_worktrees": 0,
    }
    worktree_counts = {"registered_extra": 0, "unregistered_directories": 0}

    if GIT_DIR.is_dir() and not GIT_DIR.is_symlink():
        cutoff = now.date() - timedelta(days=14)
        for path in GIT_DIR.iterdir():
            match = CODEX_ARTIFACT_RE.fullmatch(path.name)
            if match is None or not path.is_dir() or path.is_symlink():
                continue
            try:
                artifact_date = datetime.strptime(match.group(1), "%Y%m%d").date()
            except ValueError:
                skipped["unsafe"] += 1
                continue
            if artifact_date >= cutoff:
                continue
            try:
                candidate = _candidate("codex_artifacts", path)
            except RetentionError:
                skipped["unsafe"] += 1
                continue
            if candidate is None:
                skipped["unsafe"] += 1
                continue
            if process_references(path):
                skipped["busy"] += 1
            else:
                candidates.append(candidate)

    for path, category in (
        (NPM_ROOT / "_cacache", "npm_cache"),
        (NPM_ROOT / "_npx", "npm_cache"),
        *((CACHE_ROOT / name, "user_cache") for name in CACHE_CHILDREN),
    ):
        try:
            candidate = _candidate(category, path)
        except RetentionError:
            skipped["unsafe"] += 1
            continue
        if candidate is None:
            continue
        process_names = BUSY_PROCESS_NAMES if category == "npm_cache" else None
        if category == "user_cache":
            process_names = CACHE_BUSY_PROCESS_NAMES.get(path.name)
        if process_references(path, process_names):
            skipped["busy"] += 1
        else:
            candidates.append(candidate)

    try:
        vscode_paths = VSCODE_SERVERS.iterdir() if VSCODE_SERVERS.is_dir() else ()
        vscode_entries = tuple(vscode_paths)
    except OSError:
        vscode_entries = ()
        skipped["unsafe"] += 1
    for path in vscode_entries:
        if not VSCODE_SERVER_RE.fullmatch(path.name):
            continue
        try:
            candidate = _candidate("vscode_servers", path)
        except RetentionError:
            skipped["unsafe"] += 1
            continue
        if candidate is None:
            skipped["unsafe"] += 1
            continue
        if process_references(path):
            skipped["busy"] += 1
        else:
            candidates.append(candidate)

    tmp_cutoff = now.timestamp() - 24 * 60 * 60
    if TMP_ROOT.is_dir():
        for path in TMP_ROOT.iterdir():
            if not TMP_BUILD_RE.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
                continue
            try:
                if path.stat().st_mtime > tmp_cutoff:
                    continue
                candidate = _candidate("task_temp", path)
            except (OSError, RetentionError):
                skipped["unsafe"] += 1
                continue
            if candidate is None:
                skipped["unsafe"] += 1
            elif process_references(path):
                skipped["busy"] += 1
            else:
                candidates.append(candidate)

    main_real = MAIN_CHECKOUT.resolve(strict=True)
    registered: set[Path] = {main_real}
    for path, branch, locked in _worktree_records():
        try:
            resolved = path.resolve(strict=True)
            if resolved == main_real or not _managed_worktree(path):
                continue
            registered.add(resolved)
            worktree_counts["registered_extra"] += 1
            info = path.lstat()
            if locked:
                skipped["locked_worktrees"] += 1
                continue
            if branch is None:
                skipped["detached_worktrees"] += 1
                continue
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != EXPECTED_OWNER_UID or resolved != path:
                skipped["dirty_or_unknown_worktrees"] += 1
                continue
            if not _same_common_directory(path):
                skipped["dirty_or_unknown_worktrees"] += 1
                continue
            clean, _ = _worktree_status(path)
            if not clean:
                skipped["dirty_or_unknown_worktrees"] += 1
                continue
            if process_references(path):
                skipped["busy"] += 1
                continue
            candidate = _worktree_candidate(path, branch)
        except (OSError, RetentionError):
            skipped["unsafe"] += 1
            continue
        candidates.append(candidate)

    for parent, pattern in WORKTREE_SCAN_ROOTS:
        if not parent.is_dir() or parent.is_symlink():
            continue
        for child in parent.iterdir():
            if (
                not child.is_dir()
                or child.is_symlink()
                or (pattern is not None and not child.name.startswith(pattern))
            ):
                continue
            try:
                resolved = child.resolve(strict=True)
            except OSError:
                continue
            if resolved not in registered:
                worktree_counts["unregistered_directories"] += 1

    return candidates, skipped, worktree_counts


def _revalidate(candidate: Candidate) -> None:
    try:
        if candidate.path.is_symlink() or _identity(candidate.path) != candidate.identity:
            raise RetentionError("candidate identity changed")
        if _tree_allocated_bytes(candidate.path) < 0:
            raise RetentionError("candidate tree validation failed")
    except OSError as exc:
        raise RetentionError("candidate changed before deletion") from exc
    process_names: set[str] | None = None
    if candidate.category == "npm_cache":
        process_names = BUSY_PROCESS_NAMES
    elif candidate.category == "user_cache":
        process_names = CACHE_BUSY_PROCESS_NAMES.get(candidate.path.name)
    if process_references(candidate.path, process_names):
        raise RetentionError("candidate became busy")


def _remove_candidate(candidate: Candidate) -> int:
    _revalidate(candidate)
    if candidate.category == "linked_worktrees":
        clean, deps = _worktree_status(candidate.path)
        if not clean or candidate.worktree_branch is None:
            raise RetentionError("worktree changed before removal")
        for dependency in deps:
            path = candidate.path / dependency
            if path.exists() or path.is_symlink():
                dependency_candidate = _candidate("worktree_dependency", path)
                if dependency_candidate is None:
                    raise RetentionError("worktree dependency metadata is unsafe")
                _revalidate(dependency_candidate)
                shutil.rmtree(path)
        _run_git("worktree", "remove", str(candidate.path), cwd=MAIN_CHECKOUT)
        return candidate.allocated_bytes
    shutil.rmtree(candidate.path)
    return candidate.allocated_bytes


def run(apply: bool) -> dict[str, object]:
    if os.geteuid() != 0:
        raise RetentionError("root_required")
    try:
        descriptor = os.open(
            LOCK_PATH,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise RetentionError("lock_unavailable") from exc
    lock_info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(lock_info.st_mode)
        or lock_info.st_uid != 0
        or lock_info.st_nlink != 1
        or stat.S_IMODE(lock_info.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise RetentionError("lock_metadata_unsafe")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise RetentionError("lock_busy") from exc
    try:
        candidates, skipped, worktree_counts = build_plan()
        counts: dict[str, int] = {}
        reclaimable = 0
        for candidate in candidates:
            counts[candidate.category] = counts.get(candidate.category, 0) + 1
            reclaimable += candidate.allocated_bytes
        report: dict[str, object] = {
            "mode": "apply" if apply else "preview",
            "status": "passed",
            "candidate_counts": counts,
            "skipped": skipped,
            "worktrees": {
                **worktree_counts,
                "eligible": counts.get("linked_worktrees", 0),
                "removed": 0,
            },
            "reclaimable_bytes": reclaimable,
            "removed_counts": {},
            "removed_bytes": 0,
        }
        if any(skipped.values()) or worktree_counts["unregistered_directories"]:
            report["status"] = "completed_with_skips"
        if not apply:
            return report
        removed_counts: dict[str, int] = {}
        removed_bytes = 0
        for candidate in candidates:
            removed_bytes += _remove_candidate(candidate)
            removed_counts[candidate.category] = removed_counts.get(candidate.category, 0) + 1
        report["removed_counts"] = removed_counts
        report["removed_bytes"] = removed_bytes
        report["reclaimable_bytes"] = removed_bytes
        report["worktrees"]["removed"] = removed_counts.get("linked_worktrees", 0)
        return report
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete reviewed allowlisted candidates")
    args = parser.parse_args(argv)
    try:
        report = run(args.apply)
    except RetentionError as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
