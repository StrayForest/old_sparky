#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import errno
import fcntl
import os
from pathlib import Path
import shutil
import stat
from typing import Iterator


@dataclass(frozen=True, slots=True)
class ReleaseEntry:
    path: Path
    modified_at: datetime
    size_bytes: int
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    protected: tuple[ReleaseEntry, ...]
    retained: tuple[ReleaseEntry, ...]
    candidates: tuple[ReleaseEntry, ...]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self.candidates)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or apply production platform release retention. "
            "Dry-run is the default; deletion requires --apply."
        )
    )
    parser.add_argument(
        "--app-dir",
        type=Path,
        default=Path("/opt/oldsparky/platform"),
        help="Platform runtime directory (default: /opt/oldsparky/platform).",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=5,
        help="Keep this many newest releases in addition to protected symlink targets.",
    )
    parser.add_argument(
        "--min-age-days",
        type=int,
        default=0,
        help="Never delete a release newer than this age (default: 0 days).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete planned candidates. Without this flag the command is read-only.",
    )
    args = parser.parse_args()
    if args.keep < 0 or args.keep > 200:
        parser.error("--keep must be between 0 and 200")
    if args.min_age_days < 0 or args.min_age_days > 3650:
        parser.error("--min-age-days must be between 0 and 3650")
    return args


def directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            total += item.stat().st_size
    return total


def release_entry(path: Path) -> ReleaseEntry:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or path.resolve(strict=True) != path
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RuntimeError(f"Release directory metadata is unsafe: {path}")
    return ReleaseEntry(
        path=path,
        modified_at=datetime.fromtimestamp(metadata.st_mtime, tz=UTC),
        size_bytes=directory_size(path),
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )


@contextmanager
def exclusive_directory_lock(
    path: Path,
    *,
    label: str,
    pending_state: Path | None = None,
) -> Iterator[Path]:
    """Hold the same non-blocking flock used by release/build shell tooling."""

    if os.geteuid() != 0:
        raise RuntimeError(f"{label} requires root")
    resolved = path.resolve(strict=True)
    metadata = resolved.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RuntimeError(f"Unsafe {label} directory: {resolved}")
    descriptor = os.open(
        resolved,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_uid != 0
            or stat.S_IMODE(opened.st_mode) & 0o022
        ):
            raise RuntimeError(f"{label} directory changed during validation")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RuntimeError(f"Another operation holds the {label} lock") from exc
            raise
        if pending_state is not None and os.path.lexists(pending_state):
            raise RuntimeError(
                "A pending release operation must be recovered before retention"
            )
        yield resolved
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def release_operation_lock(app_dir: Path) -> Iterator[Path]:
    resolved_app_dir = app_dir.resolve(strict=True)
    shared_dir = resolved_app_dir / "shared"
    with exclusive_directory_lock(
        shared_dir,
        label="platform release",
        pending_state=shared_dir / ".release-operation.json",
    ):
        yield resolved_app_dir


def resolved_release_target(app_dir: Path, link_name: str, releases_dir: Path) -> Path:
    link_path = app_dir / link_name
    if not link_path.is_symlink():
        raise RuntimeError(f"Required symlink is missing: {link_path}")
    target = link_path.resolve(strict=True)
    if target.parent != releases_dir:
        raise RuntimeError(
            f"Refusing unexpected {link_name} target outside {releases_dir}: {target}"
        )
    if not target.is_dir():
        raise RuntimeError(f"{link_name} target is not a directory: {target}")
    return target


def build_retention_plan(
    app_dir: Path,
    *,
    keep: int,
    min_age_days: int,
    now: datetime | None = None,
) -> RetentionPlan:
    resolved_app_dir = app_dir.resolve(strict=True)
    releases_dir = (resolved_app_dir / "releases").resolve(strict=True)
    if not releases_dir.is_dir():
        raise RuntimeError(f"Release directory is missing: {releases_dir}")

    protected_paths = {
        resolved_release_target(resolved_app_dir, "current", releases_dir),
        resolved_release_target(resolved_app_dir, "previous", releases_dir),
    }
    release_paths = sorted(
        (
            path.resolve(strict=True)
            for path in releases_dir.iterdir()
            if path.is_dir() and not path.is_symlink()
        ),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    entries = [release_entry(path) for path in release_paths]
    entries_by_path = {entry.path: entry for entry in entries}
    missing_protected = protected_paths.difference(entries_by_path)
    if missing_protected:
        raise RuntimeError(
            "Protected release target is not a regular release directory: "
            + ", ".join(str(path) for path in sorted(missing_protected))
        )

    protected = tuple(
        sorted(
            (entries_by_path[path] for path in protected_paths),
            key=lambda entry: entry.path.name,
        )
    )
    newest_paths = {entry.path for entry in entries[:keep]}
    cutoff = (now or datetime.now(UTC)) - timedelta(days=min_age_days)

    retained: list[ReleaseEntry] = []
    candidates: list[ReleaseEntry] = []
    for entry in entries:
        if entry.path in protected_paths:
            continue
        if entry.path in newest_paths or entry.modified_at > cutoff:
            retained.append(entry)
        else:
            candidates.append(entry)

    return RetentionPlan(
        protected=protected,
        retained=tuple(retained),
        candidates=tuple(candidates),
    )


def human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{amount:.1f} TiB"


def print_entries(title: str, entries: tuple[ReleaseEntry, ...]) -> None:
    print(f"\n{title}: {len(entries)}")
    for entry in entries:
        print(
            f"  {entry.modified_at.isoformat()}  "
            f"{human_bytes(entry.size_bytes):>10}  {entry.path.name}"
        )


def _plan_app_dir(plan: RetentionPlan, app_dir: Path | None) -> Path:
    if app_dir is not None:
        return app_dir.resolve(strict=True)
    entries = (*plan.protected, *plan.retained, *plan.candidates)
    parents = {entry.path.parent for entry in entries}
    if len(parents) != 1:
        raise RuntimeError("Retention plan does not identify one release directory")
    releases_dir = next(iter(parents))
    if releases_dir.name != "releases":
        raise RuntimeError("Retention plan release directory is invalid")
    return releases_dir.parent.resolve(strict=True)


def _validate_candidate(
    entry: ReleaseEntry,
    *,
    app_dir: Path,
    releases_dir: Path,
) -> None:
    protected_now = {
        resolved_release_target(app_dir, "current", releases_dir),
        resolved_release_target(app_dir, "previous", releases_dir),
    }
    if entry.path in protected_now:
        raise RuntimeError(
            f"Refusing to delete a release that became protected: {entry.path}"
        )
    try:
        metadata = entry.path.lstat()
        resolved = entry.path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(
            f"Release deletion target is unavailable: {entry.path}"
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != entry.path
        or entry.path.parent != releases_dir
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_dev != entry.device
        or metadata.st_ino != entry.inode
    ):
        raise RuntimeError(
            f"Release deletion target changed after planning: {entry.path}"
        )


def apply_plan(plan: RetentionPlan, *, app_dir: Path | None = None) -> None:
    resolved_app_dir = _plan_app_dir(plan, app_dir)
    releases_dir = (resolved_app_dir / "releases").resolve(strict=True)
    for entry in plan.candidates:
        _validate_candidate(
            entry,
            app_dir=resolved_app_dir,
            releases_dir=releases_dir,
        )
        shutil.rmtree(entry.path)


def print_plan(
    plan: RetentionPlan, *, app_dir: Path, keep: int, min_age_days: int, apply: bool
) -> None:
    print("# Platform Release Retention")
    print(f"app_dir: {app_dir.resolve()}")
    print(f"keep_newest: {keep}")
    print(f"minimum_age_days: {min_age_days}")
    print(f"mode: {'apply' if apply else 'dry-run'}")
    print_entries("Protected releases", plan.protected)
    print_entries("Other retained releases", plan.retained)
    print_entries("Deletion candidates", plan.candidates)
    print(f"\nreclaimable: {human_bytes(plan.reclaimable_bytes)}")


def main() -> None:
    args = parse_args()
    if not args.apply:
        plan = build_retention_plan(
            args.app_dir,
            keep=args.keep,
            min_age_days=args.min_age_days,
        )
        print_plan(
            plan,
            app_dir=args.app_dir,
            keep=args.keep,
            min_age_days=args.min_age_days,
            apply=False,
        )
        print("No files changed. Re-run with --apply to delete candidates.")
        return

    with release_operation_lock(args.app_dir) as resolved_app_dir:
        plan = build_retention_plan(
            resolved_app_dir,
            keep=args.keep,
            min_age_days=args.min_age_days,
        )
        print_plan(
            plan,
            app_dir=resolved_app_dir,
            keep=args.keep,
            min_age_days=args.min_age_days,
            apply=True,
        )
        apply_plan(plan, app_dir=resolved_app_dir)
        print(f"Deleted {len(plan.candidates)} release(s).")


if __name__ == "__main__":
    main()
