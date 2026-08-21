#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Any, Iterator

try:
    from . import platform_live_qa_guard as live_qa_guard
    from .platform_release_retention import (
        RetentionPlan,
        apply_plan as apply_release_plan,
        build_retention_plan,
        exclusive_directory_lock,
        human_bytes,
        release_operation_lock,
        resolved_release_target,
    )
except ImportError:  # Direct execution from the tools directory.
    import platform_live_qa_guard as live_qa_guard
    from platform_release_retention import (
        RetentionPlan,
        apply_plan as apply_release_plan,
        build_retention_plan,
        exclusive_directory_lock,
        human_bytes,
        release_operation_lock,
        resolved_release_target,
    )


DEFAULT_APP_DIR = Path("/opt/oldsparky/platform")
DEFAULT_SOURCE_RELEASE_DIR = Path("/root/old_sparky/platform/dist/releases")
DEFAULT_WEB_ARTIFACT_DIR = Path("/root/old_sparky/platform/apps/platform_web")


@dataclass(frozen=True, slots=True)
class ArtifactGroup:
    slug: str
    modified_at: datetime
    paths: tuple[Path, ...]
    identities: tuple[tuple[int, int], ...]
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ArtifactRetentionPlan:
    protected: tuple[ArtifactGroup, ...]
    retained: tuple[ArtifactGroup, ...]
    candidates: tuple[ArtifactGroup, ...]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(group.size_bytes for group in self.candidates)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a restore-verified platform backup and prune only known, "
            "reproducible platform storage artifacts. Dry-run is the default."
        )
    )
    parser.add_argument("--app-dir", type=Path, default=DEFAULT_APP_DIR)
    parser.add_argument(
        "--source-release-dir", type=Path, default=DEFAULT_SOURCE_RELEASE_DIR
    )
    parser.add_argument(
        "--web-artifact-dir", type=Path, default=DEFAULT_WEB_ARTIFACT_DIR
    )
    parser.add_argument("--backup-keep", type=int, default=14)
    parser.add_argument("--release-keep", type=int, default=5)
    parser.add_argument("--test-artifact-max-age-days", type=int, default=7)
    parser.add_argument("--screenshot-max-age-days", type=int, default=30)
    parser.add_argument("--failed-build-max-age-days", type=int, default=1)
    parser.add_argument("--report-keep", type=int, default=30)
    parser.add_argument("--live-qa-runtime-keep", type=int, default=1)
    parser.add_argument("--minimum-free-gib", type=float, default=5.0)
    parser.add_argument("--maximum-used-percent", type=float, default=85.0)
    parser.add_argument("--skip-backup", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    for name in (
        "backup_keep",
        "release_keep",
        "test_artifact_max_age_days",
        "screenshot_max_age_days",
        "failed_build_max_age_days",
        "report_keep",
    ):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} must not be negative")
    if args.backup_keep < 1:
        parser.error("--backup-keep must be at least 1")
    if args.report_keep < 1:
        parser.error("--report-keep must be at least 1")
    if not 1 <= args.live_qa_runtime_keep <= 100:
        parser.error("--live-qa-runtime-keep must be between 1 and 100")
    if args.minimum_free_gib < 0:
        parser.error("--minimum-free-gib must not be negative")
    if not 0 < args.maximum_used_percent <= 100:
        parser.error("--maximum-used-percent must be within (0, 100]")
    return args


def path_size(path: Path) -> int:
    if path.is_file() and not path.is_symlink():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            total += item.stat().st_size
    return total


def artifact_slug(path: Path) -> str | None:
    if path.is_dir() and not path.is_symlink():
        return path.name if (path / "RELEASE.json").is_file() else None
    if not path.is_file() or path.is_symlink():
        return None
    for suffix in (".tar.gz.sha256", ".tar.gz"):
        if path.name.endswith(suffix):
            slug = path.name[: -len(suffix)]
            return slug or None
    return None


def build_artifact_retention_plan(
    release_dir: Path,
    *,
    protected_slugs: set[str],
    keep: int,
) -> ArtifactRetentionPlan:
    if keep < 0:
        raise ValueError("keep must not be negative")
    if not release_dir.exists():
        return ArtifactRetentionPlan((), (), ())
    resolved_dir = release_dir.resolve(strict=True)
    if not resolved_dir.is_dir():
        raise RuntimeError(f"Source release path is not a directory: {resolved_dir}")

    grouped: dict[str, list[Path]] = {}
    for path in resolved_dir.iterdir():
        slug = artifact_slug(path)
        if slug is not None:
            grouped.setdefault(slug, []).append(path)

    groups: list[ArtifactGroup] = []
    for slug, paths in grouped.items():
        resolved_paths = tuple(
            sorted((path.resolve(strict=True) for path in paths), key=str)
        )
        if any(path.parent != resolved_dir for path in resolved_paths):
            raise RuntimeError(
                f"Refusing source artifact outside {resolved_dir}: {slug}"
            )
        metadata = tuple(path.lstat() for path in resolved_paths)
        if any(
            stat_result.st_uid != 0
            or stat.S_IMODE(stat_result.st_mode) & 0o022
            or not (
                stat.S_ISREG(stat_result.st_mode) or stat.S_ISDIR(stat_result.st_mode)
            )
            for stat_result in metadata
        ):
            raise RuntimeError(f"Refusing non-root-owned source artifact: {slug}")
        groups.append(
            ArtifactGroup(
                slug=slug,
                modified_at=datetime.fromtimestamp(
                    max(path.stat().st_mtime for path in resolved_paths), tz=UTC
                ),
                paths=resolved_paths,
                identities=tuple(
                    (stat_result.st_dev, stat_result.st_ino) for stat_result in metadata
                ),
                size_bytes=sum(path_size(path) for path in resolved_paths),
            )
        )

    groups.sort(key=lambda group: (group.modified_at, group.slug), reverse=True)
    newest_slugs = {group.slug for group in groups[:keep]}
    protected = tuple(group for group in groups if group.slug in protected_slugs)
    retained = tuple(
        group
        for group in groups
        if group.slug not in protected_slugs and group.slug in newest_slugs
    )
    candidates = tuple(
        group
        for group in groups
        if group.slug not in protected_slugs and group.slug not in newest_slugs
    )
    return ArtifactRetentionPlan(protected, retained, candidates)


def apply_artifact_retention_plan(
    plan: ArtifactRetentionPlan, release_dir: Path
) -> None:
    resolved_dir = release_dir.resolve(strict=True)
    for group in plan.candidates:
        if len(group.paths) != len(group.identities):
            raise RuntimeError(
                f"Artifact deletion plan identity mismatch: {group.slug}"
            )
        for path, identity in zip(group.paths, group.identities, strict=True):
            try:
                metadata = path.lstat()
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise RuntimeError(
                    f"Artifact deletion target is unavailable: {path}"
                ) from exc
            if (
                path.is_symlink()
                or path.parent != resolved_dir
                or resolved != path
                or metadata.st_uid != 0
                or stat.S_IMODE(metadata.st_mode) & 0o022
                or not (
                    stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
                )
                or (metadata.st_dev, metadata.st_ino) != identity
            ):
                raise RuntimeError(f"Refusing unsafe artifact deletion target: {path}")
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


def collect_old_children(
    directory: Path,
    *,
    patterns: tuple[str, ...],
    max_age_days: int,
    now: datetime | None = None,
) -> tuple[Path, ...]:
    if max_age_days < 0:
        raise ValueError("max_age_days must not be negative")
    if not directory.exists():
        return ()
    resolved_dir = directory.resolve(strict=True)
    cutoff = (now or datetime.now(UTC)) - timedelta(days=max_age_days)
    candidates: list[Path] = []
    for child in resolved_dir.iterdir():
        if child.is_symlink() or not any(
            fnmatch(child.name, pattern) for pattern in patterns
        ):
            continue
        modified_at = datetime.fromtimestamp(child.stat().st_mtime, tz=UTC)
        if modified_at <= cutoff:
            candidates.append(child.resolve(strict=True))
    return tuple(sorted(candidates, key=str))


def delete_known_children(directory: Path, candidates: tuple[Path, ...]) -> int:
    if not directory.exists():
        return 0
    resolved_dir = directory.resolve(strict=True)
    reclaimed = 0
    for path in candidates:
        if path.is_symlink() or path.parent != resolved_dir:
            raise RuntimeError(f"Refusing unsafe transient deletion target: {path}")
        reclaimed += path_size(path)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    return reclaimed


def release_plan_summary(plan: RetentionPlan) -> dict[str, Any]:
    return {
        "protected": [entry.path.name for entry in plan.protected],
        "retained": [entry.path.name for entry in plan.retained],
        "deleted": [entry.path.name for entry in plan.candidates],
        "reclaimable_bytes": plan.reclaimable_bytes,
    }


def artifact_plan_summary(plan: ArtifactRetentionPlan) -> dict[str, Any]:
    return {
        "protected": [group.slug for group in plan.protected],
        "retained": [group.slug for group in plan.retained],
        "deleted": [group.slug for group in plan.candidates],
        "reclaimable_bytes": plan.reclaimable_bytes,
    }


def live_qa_runtime_plan_summary(
    plan: live_qa_guard.RuntimeCacheRetentionPlan,
) -> dict[str, Any]:
    return {
        "protected": [entry.path.name for entry in plan.protected],
        "retained": [entry.path.name for entry in plan.retained],
        "deleted": [entry.path.name for entry in plan.candidates],
        "reclaimed_tombstones": [entry.path.name for entry in plan.tombstones],
    }


def disk_snapshot(path: Path) -> dict[str, int | float]:
    usage = shutil.disk_usage(path)
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": round(usage.used * 100 / usage.total, 2),
    }


def run_backup(app_dir: Path, *, keep: int) -> dict[str, Any]:
    script = Path(__file__).with_name("platform_backup_restore_drill.py")
    shared_dir = app_dir / "shared"
    command = [
        sys.executable,
        str(script),
        "--env-file",
        str(shared_dir / ".env.platform"),
        "--output-dir",
        str(shared_dir / "backups"),
        "--keep",
        str(keep),
        "--json",
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Platform backup returned invalid JSON output.") from exc
    if completed.returncode != 0 or not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "Platform backup failed."))
    return {
        "dump_file": result.get("dump_file"),
        "metadata_file": result.get("metadata_file"),
        "size_bytes": result.get("size_bytes"),
        "duration_seconds": result.get("duration_seconds"),
        "restore_verified": result.get("restore_verified"),
        "restored_table_count": result.get("restored_table_count"),
        "removed_count": len(result.get("removed") or []),
    }


def write_report(report_dir: Path, report: dict[str, Any], *, keep: int) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"platform-maintenance-{timestamp}.json"
    temporary_path = report_dir / f".{report_path.name}.{id(report)}.tmp"
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary_path.chmod(0o600)
    temporary_path.replace(report_path)
    reports = sorted(
        report_dir.glob("platform-maintenance-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for obsolete in reports[keep:]:
        if (
            obsolete.is_file()
            and not obsolete.is_symlink()
            and obsolete.parent == report_dir.resolve()
        ):
            obsolete.unlink()
    return report_path


@contextmanager
def source_release_lock(path: Path) -> Iterator[Path | None]:
    """Join the build directory flock, or freeze a proven-absent source contour."""

    if not os.path.lexists(path):
        yield None
        return
    with exclusive_directory_lock(
        path, label="platform release build output"
    ) as resolved:
        yield resolved


def _plan_and_maybe_apply(
    args: argparse.Namespace,
    *,
    app_dir: Path,
    source_release_dir: Path | None,
) -> tuple[
    RetentionPlan,
    ArtifactRetentionPlan,
    tuple[Path, ...],
    tuple[Path, ...],
    tuple[Path, ...],
    dict[str, Any],
    dict[str, int],
]:
    releases_dir = (app_dir / "releases").resolve(strict=True)
    protected_slugs = {
        resolved_release_target(app_dir, "current", releases_dir).name,
        resolved_release_target(app_dir, "previous", releases_dir).name,
    }
    production_plan = build_retention_plan(
        app_dir,
        keep=args.release_keep,
        min_age_days=0,
    )
    if source_release_dir is None:
        source_plan = ArtifactRetentionPlan((), (), ())
        failed_builds: tuple[Path, ...] = ()
    else:
        source_plan = build_artifact_retention_plan(
            source_release_dir,
            protected_slugs=protected_slugs,
            keep=args.release_keep,
        )
        failed_builds = collect_old_children(
            source_release_dir,
            patterns=(".build-*",),
            max_age_days=args.failed_build_max_age_days,
        )
    test_artifacts = collect_old_children(
        args.web_artifact_dir,
        patterns=("test-results*", "playwright-report*"),
        max_age_days=args.test_artifact_max_age_days,
    )
    screenshot_dir = app_dir / "shared" / "preprod-screenshots"
    screenshots = collect_old_children(
        screenshot_dir,
        patterns=("*",),
        max_age_days=args.screenshot_max_age_days,
    )
    backup: dict[str, Any] = {"status": "skipped"}

    if args.apply:
        if not args.skip_backup:
            backup = {
                "status": "completed",
                **run_backup(app_dir, keep=args.backup_keep),
            }
        apply_release_plan(production_plan, app_dir=app_dir)
        if source_release_dir is not None:
            apply_artifact_retention_plan(source_plan, source_release_dir)
        transient_reclaimed = {
            "failed_builds": (
                delete_known_children(source_release_dir, failed_builds)
                if source_release_dir is not None
                else 0
            ),
            "browser_test_artifacts": delete_known_children(
                args.web_artifact_dir, test_artifacts
            ),
            "preprod_screenshots": delete_known_children(screenshot_dir, screenshots),
        }
    else:
        transient_reclaimed = {
            "failed_builds": sum(path_size(path) for path in failed_builds),
            "browser_test_artifacts": sum(path_size(path) for path in test_artifacts),
            "preprod_screenshots": sum(path_size(path) for path in screenshots),
        }

    return (
        production_plan,
        source_plan,
        failed_builds,
        test_artifacts,
        screenshots,
        backup,
        transient_reclaimed,
    )


def run_maintenance(args: argparse.Namespace) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    app_dir = args.app_dir.resolve(strict=True)
    disk_before = disk_snapshot(Path("/"))

    if args.apply:
        # Fixed global order: release transaction lock, build-output lock, then
        # live-QA machine lock. Install/rollback take only the first; builds take
        # only the second; standalone live-QA retention takes first then third.
        with release_operation_lock(app_dir):
            with source_release_lock(args.source_release_dir) as source_release_dir:
                maintenance_result = _plan_and_maybe_apply(
                    args,
                    app_dir=app_dir,
                    source_release_dir=source_release_dir,
                )
                live_qa_plan = live_qa_guard.prune_runtime_cache_release_lock_held(
                    apply=True,
                    keep=args.live_qa_runtime_keep,
                    root=getattr(
                        args,
                        "live_qa_runtime_root",
                        live_qa_guard.RUNNER_CACHE_ROOT,
                    ),
                    app_dir=app_dir,
                )
    else:
        source_release_dir = (
            args.source_release_dir if args.source_release_dir.exists() else None
        )
        maintenance_result = _plan_and_maybe_apply(
            args,
            app_dir=app_dir,
            source_release_dir=source_release_dir,
        )
        live_qa_plan = live_qa_guard.prune_runtime_cache(
            apply=False,
            keep=args.live_qa_runtime_keep,
            root=getattr(
                args,
                "live_qa_runtime_root",
                live_qa_guard.RUNNER_CACHE_ROOT,
            ),
            app_dir=app_dir,
        )

    (
        production_plan,
        source_plan,
        failed_builds,
        test_artifacts,
        screenshots,
        backup,
        transient_reclaimed,
    ) = maintenance_result

    disk_after = disk_snapshot(Path("/"))
    minimum_free_bytes = int(args.minimum_free_gib * 1024**3)
    storage_ok = (
        int(disk_after["free_bytes"]) >= minimum_free_bytes
        and float(disk_after["used_percent"]) <= args.maximum_used_percent
    )
    completed_at = datetime.now(UTC)
    return {
        "ok": storage_ok,
        "mode": "apply" if args.apply else "dry-run",
        "started_at_utc": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at_utc": completed_at.isoformat().replace("+00:00", "Z"),
        "duration_seconds": round((completed_at - started_at).total_seconds(), 3),
        "backup": backup,
        "production_releases": release_plan_summary(production_plan),
        "source_release_artifacts": artifact_plan_summary(source_plan),
        "live_qa_runtime_caches": live_qa_runtime_plan_summary(live_qa_plan),
        "transient": {
            "failed_builds": [path.name for path in failed_builds],
            "browser_test_artifacts": [path.name for path in test_artifacts],
            "preprod_screenshots": [path.name for path in screenshots],
            "reclaimable_bytes": transient_reclaimed,
        },
        "disk_before": disk_before,
        "disk_after": disk_after,
        "limits": {
            "minimum_free_bytes": minimum_free_bytes,
            "maximum_used_percent": args.maximum_used_percent,
            "live_qa_runtime_keep": args.live_qa_runtime_keep,
        },
    }


def print_summary(report: dict[str, Any]) -> None:
    print(
        f"[{'OK' if report['ok'] else 'FAIL'}] Platform storage maintenance ({report['mode']})"
    )
    backup = report["backup"]
    print(f"backup: {backup.get('status')}")
    for key in ("production_releases", "source_release_artifacts"):
        section = report[key]
        print(
            f"{key}: delete={len(section['deleted'])}, "
            f"reclaimable={human_bytes(section['reclaimable_bytes'])}"
        )
    live_qa = report["live_qa_runtime_caches"]
    print(
        "live_qa_runtime_caches: "
        f"delete={len(live_qa['deleted'])}, "
        f"reclaim_tombstones={len(live_qa['reclaimed_tombstones'])}"
    )
    disk_after = report["disk_after"]
    print(
        f"disk_after: used={disk_after['used_percent']}%, "
        f"free={human_bytes(int(disk_after['free_bytes']))}"
    )


def main() -> int:
    args = parse_args()
    try:
        report = run_maintenance(args)
        if args.apply:
            report_path = write_report(
                args.app_dir / "shared" / "maintenance",
                report,
                keep=args.report_keep,
            )
            report["report_file"] = str(report_path)
        if args.as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_summary(report)
        return 0 if report["ok"] else 1
    except Exception as exc:
        if args.as_json:
            print(
                json.dumps(
                    {"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2
                )
            )
        else:
            print(f"[FAIL] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
