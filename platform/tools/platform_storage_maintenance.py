#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit

# The backup workflow executes this exact attested helper with Python's
# isolated mode.  `-I` intentionally omits the script directory from
# `sys.path`, so explicitly add only this file's sibling tools directory for
# the closed, source- and hash-validated helper bundle.
_SCRIPT_TOOLS_DIR = str(Path(__file__).resolve(strict=True).parent)
if _SCRIPT_TOOLS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_TOOLS_DIR)


def _load_staged_disk_policy() -> Any:
    helper_path = Path(__file__).resolve().with_name("platform_disk_policy.py")
    spec = importlib.util.spec_from_file_location(
        "_oldsparky_platform_disk_policy", helper_path
    )
    if spec is None or spec.loader is None:
        raise ImportError("platform disk policy helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


try:
    from .platform_disk_policy import (
        DEFAULT_MAX_USED_PERCENT,
        DEFAULT_MIN_FREE_GIB,
        is_healthy as disk_is_healthy,
        minimum_free_bytes as minimum_free_bytes_for_gib,
        snapshot_for_path as disk_snapshot_for_path,
    )
except ImportError:  # Direct execution from the tools directory.
    _disk_policy = _load_staged_disk_policy()
    DEFAULT_MAX_USED_PERCENT = _disk_policy.DEFAULT_MAX_USED_PERCENT
    DEFAULT_MIN_FREE_GIB = _disk_policy.DEFAULT_MIN_FREE_GIB
    disk_is_healthy = _disk_policy.is_healthy
    minimum_free_bytes_for_gib = _disk_policy.minimum_free_bytes
    disk_snapshot_for_path = _disk_policy.snapshot_for_path

try:
    from . import platform_live_qa_guard as live_qa_guard
    from . import platform_build_node_cache
    from .platform_release_retention import (
        RetentionPlan,
        apply_plan as apply_release_plan,
        build_retention_plan,
        exclusive_directory_lock,
        exclusive_retained_load_lock,
        human_bytes,
        release_operation_lock,
        resolved_release_target,
    )
except ImportError:  # Direct execution from the tools directory.
    import platform_live_qa_guard as live_qa_guard
    import platform_build_node_cache
    from platform_release_retention import (
        RetentionPlan,
        apply_plan as apply_release_plan,
        build_retention_plan,
        exclusive_directory_lock,
        exclusive_retained_load_lock,
        human_bytes,
        release_operation_lock,
        resolved_release_target,
    )


DEFAULT_APP_DIR = Path("/opt/oldsparky/platform")
DEFAULT_PLATFORM_SOURCE_ROOT = Path("/root/old_sparky/platform")
DEFAULT_SOURCE_RELEASE_DIR = Path("/root/old_sparky/platform/dist/releases")
DEFAULT_WEB_ARTIFACT_DIR = Path("/root/old_sparky/platform/apps/platform_web")
SAFE_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
SAFE_RUNTIME_ID_RE = re.compile(r"^runtime-[0-9a-f]{40}$")
FALLBACK_CACHE_COMPACTION_EVENT = "live_qa_fallback_runtime_compaction"
LEGACY_FALLBACK_RUNTIME_COMMIT = "4a04b2dffaf0d02c2d3910e7ba28dca9b89de209"
FALLBACK_CACHE_COMPACTION_MAX_INTENT_BYTES = 8 * 1024 * 1024
FALLBACK_CACHE_COMPACTION_MAX_FINAL_BYTES = 16 * 1024
RESTORE_DIAGNOSTIC_KEYS = {
    "schema",
    "restore_stage",
    "guard_reason",
    "free_bytes",
    "required_free_bytes",
    "temporary_database_created",
    "drop_outcome",
    "temporary_database_absent",
    "archive_sha256",
    "archive_size_bytes",
}
RESTORE_STAGES = {
    "pre_create_admission",
    "create_database",
    "extension_setup",
    "schema_setup",
    "restore_platform",
    "restore_public",
    "validate_table_count",
    "validate_connectivity",
    "validate_revision",
    "validate_extensions",
    "drop_database",
}
RESTORE_GUARD_REASONS = {
    "none",
    "disk_floor",
    "disk_unavailable",
    "child_unstopped",
    "command_start_failed",
    "command_failed",
}
RESTORE_DROP_OUTCOMES = {
    "not_required",
    "drop_failed",
    "database_present",
    "absence_unconfirmed",
    "confirmed_absent",
}


def _valid_restore_diagnostic(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != RESTORE_DIAGNOSTIC_KEYS:
        return False
    if (
        type(value.get("schema")) is not int
        or value["schema"] != 1
        or type(value.get("restore_stage")) is not str
        or value["restore_stage"] not in RESTORE_STAGES
        or type(value.get("guard_reason")) is not str
        or value["guard_reason"] not in RESTORE_GUARD_REASONS
        or type(value.get("drop_outcome")) is not str
        or value["drop_outcome"] not in RESTORE_DROP_OUTCOMES
        or (
            value.get("temporary_database_created") is not None
            and type(value.get("temporary_database_created")) is not bool
        )
        or (
            value.get("temporary_database_absent") is not None
            and type(value.get("temporary_database_absent")) is not bool
        )
    ):
        return False
    for key in ("free_bytes", "required_free_bytes"):
        if value.get(key) is not None and (
            type(value[key]) is not int or value[key] < 0
        ):
            return False
    if (
        type(value.get("archive_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value["archive_sha256"]) is None
        or type(value.get("archive_size_bytes")) is not int
        or value["archive_size_bytes"] < 0
    ):
        return False
    if value["guard_reason"] == "disk_floor":
        if (
            type(value.get("free_bytes")) is not int
            or type(value.get("required_free_bytes")) is not int
            or value["free_bytes"] >= value["required_free_bytes"]
        ):
            return False
    if value["temporary_database_created"] is False or value["temporary_database_created"] is None:
        return (
            (value["temporary_database_created"] is False or value["restore_stage"] == "create_database")
            and
            value["drop_outcome"] == "not_required"
            and value["temporary_database_absent"] is None
        )
    if value["drop_outcome"] == "not_required":
        return False
    if value["drop_outcome"] == "confirmed_absent":
        return value["temporary_database_absent"] is True
    if value["drop_outcome"] == "database_present":
        return value["temporary_database_absent"] is False
    if value["drop_outcome"] in {"drop_failed", "absence_unconfirmed"}:
        return value["temporary_database_absent"] is None
    return False


class BackupCommandFailure(RuntimeError):
    def __init__(self, diagnostic: dict[str, Any] | None = None) -> None:
        self.restore_diagnostic = diagnostic
        super().__init__("Platform backup failed.")


def _safe_release_id(value: Any) -> str | None:
    candidate = str(value)
    return candidate if SAFE_RELEASE_ID_RE.fullmatch(candidate) else None


def _safe_runtime_id(value: Any) -> str | None:
    candidate = str(value)
    return candidate if SAFE_RUNTIME_ID_RE.fullmatch(candidate) else None


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
    parser.add_argument("--backup-max-age-hours", type=float, default=24.0)
    parser.add_argument("--release-keep", type=int, default=5)
    parser.add_argument("--test-artifact-max-age-days", type=int, default=7)
    parser.add_argument("--screenshot-max-age-days", type=int, default=30)
    parser.add_argument("--failed-build-max-age-days", type=int, default=1)
    parser.add_argument("--report-keep", type=int, default=30)
    parser.add_argument("--live-qa-runtime-keep", type=int, default=1)
    parser.add_argument(
        "--minimum-free-gib", type=float, default=DEFAULT_MIN_FREE_GIB
    )
    parser.add_argument(
        "--maximum-used-percent", type=float, default=DEFAULT_MAX_USED_PERCENT
    )
    parser.add_argument("--skip-backup", action="store_true")
    parser.add_argument(
        "--private-backup-diagnostics",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--backup-only",
        action="store_true",
        help=(
            "Create and verify only the production backup under the canonical "
            "release, retained-load, build and live-QA lock order."
        ),
    )
    parser.add_argument(
        "--verify-existing-backup-only",
        action="store_true",
        help=(
            "Restore-verify only the newest existing backup under canonical host "
            "locks; do not create or rotate an archive."
        ),
    )
    parser.add_argument(
        "--purge-profile-access-cache-after-restore",
        action="store_true",
        help=(
            "Purge only the fixed v1/v2 tournament profile-access Redis namespaces "
            "after a database restore, while API and worker are stopped."
        ),
    )
    parser.add_argument(
        "--evict-pinned-build-node-cache",
        action="store_true",
        help=(
            "Explicitly remove only the validated pinned build-Node cache before "
            "verify-existing-backup-only; requires the canonical maintenance locks."
        ),
    )
    parser.add_argument("--eviction-run-id")
    parser.add_argument("--eviction-run-attempt")
    parser.add_argument("--eviction-source-sha")
    parser.add_argument("--eviction-bundle-sha256")
    parser.add_argument(
        "--compact-legacy-fallback-runtime-cache",
        action="store_true",
        help=(
            "Explicitly compact only the pinned 4a live-QA fallback Chromium "
            "payload before verify-existing-backup-only; requires canonical locks."
        ),
    )
    parser.add_argument("--compaction-run-id")
    parser.add_argument("--compaction-run-attempt")
    parser.add_argument("--compaction-source-sha")
    parser.add_argument("--compaction-bundle-sha256")
    parser.add_argument(
        "--resume-legacy-fallback-runtime-cache-compaction",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--resume-compaction-run-id")
    parser.add_argument("--resume-compaction-run-attempt")
    parser.add_argument("--resume-compaction-source-sha")
    parser.add_argument("--resume-compaction-bundle-sha256")
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
    if (
        not math.isfinite(args.backup_max_age_hours)
        or args.backup_max_age_hours <= 0
    ):
        parser.error("--backup-max-age-hours must be finite and positive")
    if args.backup_only and args.skip_backup:
        parser.error("--backup-only cannot be combined with --skip-backup")
    if args.backup_only and args.verify_existing_backup_only:
        parser.error("backup-only modes are mutually exclusive")
    if args.verify_existing_backup_only and args.skip_backup:
        parser.error("--verify-existing-backup-only cannot be combined with --skip-backup")
    if args.purge_profile_access_cache_after_restore:
        if not args.apply:
            parser.error("--purge-profile-access-cache-after-restore requires --apply")
        if not args.as_json:
            parser.error("--purge-profile-access-cache-after-restore requires --json")
        if (
            args.backup_only
            or args.verify_existing_backup_only
            or args.skip_backup
            or args.private_backup_diagnostics
            or args.evict_pinned_build_node_cache
            or args.compact_legacy_fallback_runtime_cache
            or args.resume_legacy_fallback_runtime_cache_compaction
        ):
            parser.error(
                "restore profile-access cache purge is an exclusive maintenance operation"
            )
    if args.evict_pinned_build_node_cache and not args.verify_existing_backup_only:
        parser.error("--evict-pinned-build-node-cache requires --verify-existing-backup-only")
    eviction_bindings = (
        args.eviction_run_id,
        args.eviction_run_attempt,
        args.eviction_source_sha,
        args.eviction_bundle_sha256,
    )
    if args.evict_pinned_build_node_cache:
        if (
            re.fullmatch(r"[1-9][0-9]{0,19}", args.eviction_run_id or "") is None
            or re.fullmatch(r"[1-9][0-9]{0,5}", args.eviction_run_attempt or "") is None
            or re.fullmatch(r"[0-9a-f]{40}", args.eviction_source_sha or "") is None
            or re.fullmatch(r"[0-9a-f]{64}", args.eviction_bundle_sha256 or "") is None
        ):
            parser.error("cache eviction requires closed source and workflow bindings")
    elif any(value is not None for value in eviction_bindings):
        parser.error("eviction identity fields require --evict-pinned-build-node-cache")
    compaction_bindings = (
        args.compaction_run_id,
        args.compaction_run_attempt,
        args.compaction_source_sha,
        args.compaction_bundle_sha256,
    )
    if args.compact_legacy_fallback_runtime_cache:
        if (
            not args.verify_existing_backup_only
            or args.evict_pinned_build_node_cache
            or re.fullmatch(r"[1-9][0-9]{0,19}", args.compaction_run_id or "") is None
            or re.fullmatch(r"[1-9][0-9]{0,5}", args.compaction_run_attempt or "") is None
            or re.fullmatch(r"[0-9a-f]{40}", args.compaction_source_sha or "") is None
            or re.fullmatch(r"[0-9a-f]{64}", args.compaction_bundle_sha256 or "") is None
        ):
            parser.error(
                "fallback cache compaction requires verify-existing mode and closed source/run bindings"
            )
    elif any(value is not None for value in compaction_bindings):
        parser.error(
            "compaction identity fields require --compact-legacy-fallback-runtime-cache"
        )
    resume_bindings = (
        args.resume_compaction_run_id,
        args.resume_compaction_run_attempt,
        args.resume_compaction_source_sha,
        args.resume_compaction_bundle_sha256,
    )
    if args.resume_legacy_fallback_runtime_cache_compaction:
        if (
            not args.verify_existing_backup_only
            or not args.compact_legacy_fallback_runtime_cache
            or re.fullmatch(r"[1-9][0-9]{0,19}", args.resume_compaction_run_id or "") is None
            or re.fullmatch(r"[1-9][0-9]{0,5}", args.resume_compaction_run_attempt or "") is None
            or re.fullmatch(r"[0-9a-f]{40}", args.resume_compaction_source_sha or "") is None
            or re.fullmatch(r"[0-9a-f]{64}", args.resume_compaction_bundle_sha256 or "") is None
        ):
            parser.error(
                "compaction recovery requires verify-existing mode and exact receipt bindings"
            )
        if args.resume_compaction_source_sha != args.compaction_source_sha and args.compact_legacy_fallback_runtime_cache:
            parser.error("compaction recovery and current source SHA must match exactly")
    elif any(value is not None for value in resume_bindings):
        parser.error(
            "resume identity fields require --resume-legacy-fallback-runtime-cache-compaction"
        )
    if args.backup_only and not args.apply:
        parser.error("--backup-only requires --apply")
    if args.verify_existing_backup_only and not args.apply:
        parser.error("--verify-existing-backup-only requires --apply")
    if args.private_backup_diagnostics and not (args.backup_only or args.verify_existing_backup_only):
        parser.error("--private-backup-diagnostics requires a backup-only mode")
    if args.report_keep < 1:
        parser.error("--report-keep must be at least 1")
    if not 1 <= args.live_qa_runtime_keep <= 100:
        parser.error("--live-qa-runtime-keep must be between 1 and 100")
    if not math.isfinite(args.minimum_free_gib) or args.minimum_free_gib < 0:
        parser.error("--minimum-free-gib must be finite and non-negative")
    if (
        not math.isfinite(args.maximum_used_percent)
        or not 0 < args.maximum_used_percent <= 100
    ):
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
        return (
            path.name
            if (path / "RELEASE.json").is_file()
            and SAFE_RELEASE_ID_RE.fullmatch(path.name)
            else None
        )
    if not path.is_file() or path.is_symlink():
        return None
    for suffix in (".tar.gz.sha256", ".tar.gz"):
        if path.name.endswith(suffix):
            slug = path.name[: -len(suffix)]
            return slug if slug and SAFE_RELEASE_ID_RE.fullmatch(slug) else None
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
        raise RuntimeError("Source release path is not a directory")

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
            raise RuntimeError("Refusing source artifact outside release directory")
        metadata = tuple(path.lstat() for path in resolved_paths)
        if any(
            stat_result.st_uid != 0
            or stat.S_IMODE(stat_result.st_mode) & 0o022
            or not (
                stat.S_ISREG(stat_result.st_mode) or stat.S_ISDIR(stat_result.st_mode)
            )
            for stat_result in metadata
        ):
            raise RuntimeError("Refusing non-root-owned source artifact")
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
            raise RuntimeError("Artifact deletion plan identity mismatch")
        for path, identity in zip(group.paths, group.identities, strict=True):
            try:
                metadata = path.lstat()
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise RuntimeError("Artifact deletion target is unavailable") from exc
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
                raise RuntimeError("Refusing unsafe artifact deletion target")
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
            raise RuntimeError("Refusing unsafe transient deletion target")
        reclaimed += path_size(path)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    return reclaimed


def release_plan_summary(plan: RetentionPlan) -> dict[str, Any]:
    return {
        "protected": [
            safe_id
            for entry in plan.protected
            if (safe_id := _safe_release_id(entry.path.name)) is not None
        ],
        "retained": [
            safe_id
            for entry in plan.retained
            if (safe_id := _safe_release_id(entry.path.name)) is not None
        ],
        "deleted": [
            safe_id
            for entry in plan.candidates
            if (safe_id := _safe_release_id(entry.path.name)) is not None
        ],
        "protected_count": len(plan.protected),
        "retained_count": len(plan.retained),
        "deleted_count": len(plan.candidates),
        "reclaimable_bytes": plan.reclaimable_bytes,
    }


def artifact_plan_summary(plan: ArtifactRetentionPlan) -> dict[str, Any]:
    return {
        "protected": [
            safe_id
            for group in plan.protected
            if (safe_id := _safe_release_id(group.slug)) is not None
        ],
        "retained": [
            safe_id
            for group in plan.retained
            if (safe_id := _safe_release_id(group.slug)) is not None
        ],
        "deleted": [
            safe_id
            for group in plan.candidates
            if (safe_id := _safe_release_id(group.slug)) is not None
        ],
        "protected_count": len(plan.protected),
        "retained_count": len(plan.retained),
        "deleted_count": len(plan.candidates),
        "reclaimable_bytes": plan.reclaimable_bytes,
    }


def live_qa_runtime_plan_summary(
    plan: live_qa_guard.RuntimeCacheRetentionPlan,
) -> dict[str, Any]:
    return {
        "protected": [
            safe_id
            for entry in plan.protected
            if (safe_id := _safe_runtime_id(entry.path.name)) is not None
        ],
        "retained": [
            safe_id
            for entry in plan.retained
            if (safe_id := _safe_runtime_id(entry.path.name)) is not None
        ],
        "deleted": [
            safe_id
            for entry in plan.candidates
            if (safe_id := _safe_runtime_id(entry.path.name)) is not None
        ],
        "reclaimed_tombstones": [],
        "protected_count": len(plan.protected),
        "retained_count": len(plan.retained),
        "deleted_count": len(plan.candidates),
        "reclaimed_tombstone_count": len(plan.tombstones),
    }


def disk_snapshot(path: Path) -> dict[str, int | float]:
    """Preserve the maintenance JSON shape while sharing policy semantics."""

    return disk_snapshot_for_path(path).as_dict()


def _run_backup_command(
    command: list[str], *, forward_failure_diagnostics: bool = False
) -> dict[str, Any]:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        if forward_failure_diagnostics and completed.stderr:
            sys.stderr.write(completed.stderr[:65536])
        raise RuntimeError("Platform backup returned invalid JSON output.") from exc
    if completed.returncode != 0 or not isinstance(result, dict) or not result.get("ok"):
        if forward_failure_diagnostics:
            diagnostic_parts = [
                value[:65536]
                for value in (completed.stderr, result.get("error") if isinstance(result, dict) else None)
                if isinstance(value, str) and value
            ]
            if diagnostic_parts:
                sys.stderr.write("\n".join(diagnostic_parts)[:65536])
                if not diagnostic_parts[-1].endswith("\n"):
                    sys.stderr.write("\n")
        diagnostic = result.get("restore_diagnostic") if isinstance(result, dict) else None
        raise BackupCommandFailure(
            diagnostic if _valid_restore_diagnostic(diagnostic) else None
        )
    return result


def run_backup(
    app_dir: Path,
    *,
    keep: int,
    max_age_hours: float = 24.0,
    rotate_existing: bool = True,
    private_failure_diagnostics: bool = False,
) -> dict[str, Any]:
    if not math.isfinite(max_age_hours) or max_age_hours <= 0:
        raise ValueError("backup max age must be finite and positive")
    script = Path(__file__).with_name("platform_backup_restore_drill.py")
    shared_dir = app_dir / "shared"
    create_command = [
        sys.executable,
        "-B",
        str(script),
        "--env-file",
        str(shared_dir / ".env.platform"),
        "--output-dir",
        str(shared_dir / "backups"),
        "--keep",
        str(keep),
        "--rotate-existing" if rotate_existing else "--preserve-existing",
        "--json",
    ]
    result = _run_backup_command(
        create_command,
        forward_failure_diagnostics=private_failure_diagnostics,
    )
    check_result = _run_backup_command(
        [
            sys.executable,
            "-B",
            str(script),
            "--env-file",
            str(shared_dir / ".env.platform"),
            "--output-dir",
            str(shared_dir / "backups"),
            "--check-latest",
            "--max-age-hours",
            str(max_age_hours),
            "--json",
        ],
        forward_failure_diagnostics=private_failure_diagnostics,
    )
    if result.get("restore_verified") is not True or check_result.get("restore_verified") is not True:
        raise RuntimeError("Platform backup was not restore-verified.")
    return {
        "size_bytes": result.get("size_bytes")
        if isinstance(result.get("size_bytes"), int)
        and not isinstance(result.get("size_bytes"), bool)
        and result.get("size_bytes") >= 0
        else None,
        "duration_seconds": result.get("duration_seconds")
        if isinstance(result.get("duration_seconds"), (int, float))
        and not isinstance(result.get("duration_seconds"), bool)
        and result.get("duration_seconds") >= 0
        else None,
        "restore_verified": result.get("restore_verified") is True,
        "alembic_revision_verified": result.get("alembic_revision_verified") is True,
        "checksum_present": isinstance(result.get("sha256"), str),
        "restored_table_count": result.get("restored_table_count")
        if isinstance(result.get("restored_table_count"), int)
        and not isinstance(result.get("restored_table_count"), bool)
        and result.get("restored_table_count") >= 0
        else None,
        "age_hours": check_result.get("age_hours")
        if isinstance(check_result.get("age_hours"), (int, float))
        and not isinstance(check_result.get("age_hours"), bool)
        and check_result.get("age_hours") >= 0
        else None,
        "removed_count": len(result.get("removed") or []),
        "rotation_mode": result.get("rotation_mode"),
        "preexisting_archive_count": result.get("preexisting_archive_count")
        if isinstance(result.get("preexisting_archive_count"), int)
        and not isinstance(result.get("preexisting_archive_count"), bool)
        else None,
        "preexisting_sidecar_count": result.get("preexisting_sidecar_count")
        if isinstance(result.get("preexisting_sidecar_count"), int)
        and not isinstance(result.get("preexisting_sidecar_count"), bool)
        else None,
        "preexisting_archive_inventory_sha256": result.get(
            "preexisting_archive_inventory_sha256"
        )
        if isinstance(result.get("preexisting_archive_inventory_sha256"), str)
        else None,
        "postexisting_archive_inventory_sha256": result.get(
            "postexisting_archive_inventory_sha256"
        )
        if isinstance(result.get("postexisting_archive_inventory_sha256"), str)
        else None,
        "preexisting_archives_preserved": result.get("preexisting_archives_preserved") is True,
    }


RESTORE_PURGE_SERVICES = ("deadlock-api.service", "deadlock-worker.service")
RESTORE_PURGE_SYSTEM_PYTHON = Path("/usr/bin/python3.12")


def _restore_purge_python(app_dir: Path) -> Path:
    """Validate and return the fixed shared-venv launcher without resolving argv."""

    try:
        canonical_app_dir = DEFAULT_APP_DIR.resolve(strict=True)
        app_metadata = app_dir.lstat()
    except OSError as exc:
        raise RuntimeError("canonical restore Python runtime is unavailable") from exc
    if (
        app_dir != canonical_app_dir
        or not stat.S_ISDIR(app_metadata.st_mode)
        or stat.S_ISLNK(app_metadata.st_mode)
        or app_metadata.st_uid != os.geteuid()
        or app_metadata.st_gid != os.getegid()
        or app_metadata.st_mode & 0o022
    ):
        raise RuntimeError("canonical restore Python runtime is unsafe")

    shared = app_dir / "shared"
    venv = shared / "venv"
    bin_dir = venv / "bin"
    expected_owner = (app_metadata.st_uid, app_metadata.st_gid)
    for directory in (shared, venv, bin_dir):
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise RuntimeError("canonical restore Python runtime is unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (metadata.st_uid, metadata.st_gid) != expected_owner
            or metadata.st_mode & 0o022
            or metadata.st_nlink < 2
        ):
            raise RuntimeError("canonical restore Python runtime is unsafe")

    python = bin_dir / "python"
    try:
        python_metadata = python.lstat()
        resolved_python = python.resolve(strict=True)
        resolved_metadata = resolved_python.lstat()
    except OSError as exc:
        raise RuntimeError("canonical restore Python runtime is unavailable") from exc
    if (
        not (stat.S_ISLNK(python_metadata.st_mode) or stat.S_ISREG(python_metadata.st_mode))
        or python_metadata.st_uid != expected_owner[0]
        or python_metadata.st_gid != expected_owner[1]
        or (stat.S_ISREG(python_metadata.st_mode) and python_metadata.st_mode & 0o022)
        or python_metadata.st_nlink != 1
        or resolved_python != RESTORE_PURGE_SYSTEM_PYTHON
        or not stat.S_ISREG(resolved_metadata.st_mode)
        or resolved_metadata.st_uid != 0
        or resolved_metadata.st_gid != 0
        or resolved_metadata.st_mode & 0o022
        or not os.access(resolved_python, os.X_OK)
    ):
        raise RuntimeError("canonical restore Python runtime is unsafe")
    return python


def _require_restore_services_stopped() -> None:
    """Require both write-capable application units to be explicitly inactive."""

    for unit in RESTORE_PURGE_SERVICES:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", unit],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("restore cache purge could not verify application services") from exc
        if result.returncode != 3 or result.stdout.strip() != "inactive":
            raise RuntimeError("restore cache purge requires stopped API and worker services")


def _purge_restored_profile_access_cache(app_dir: Path, current_release: Path) -> int:
    """Invoke the deployed service's fixed all-version purge with its Redis URL."""

    from platform_safe_env_exec import load_env_file

    python = _restore_purge_python(app_dir)

    values = load_env_file(app_dir / "shared" / ".env.platform")
    redis_url = values.get("PLATFORM_REDIS_URL")
    if not isinstance(redis_url, str) or not redis_url:
        raise RuntimeError("canonical platform Redis configuration is unavailable")
    parsed = urlsplit(redis_url)
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
        raise RuntimeError("canonical platform Redis configuration is invalid")

    api_root = current_release / "apps" / "platform_api"
    if not api_root.is_dir() or api_root.is_symlink():
        raise RuntimeError("current API source is unavailable for restore cache purge")
    if not (api_root / "app" / "services" / "tournament_profile_access.py").is_file():
        raise RuntimeError("current profile-access purge helper is unavailable")

    # Use a clean child environment so an inherited test or operator override
    # cannot redirect this fixed purge to another Redis database.  The URL is
    # consumed by the child only and never appears in output or argv.
    child_env = {
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PLATFORM_ENVIRONMENT": "production",
        "PLATFORM_SHARED_DIR": str(app_dir / "shared"),
        "PLATFORM_REDIS_URL": redis_url,
    }
    child_code = (
        "import asyncio\n"
        "from pathlib import Path\n"
        "import sys\n"
        "root = Path(sys.argv[1]).resolve(strict=True)\n"
        "sys.path[:0] = [str(root / 'apps/platform_api'), "
        "str(root / 'python_packages'), str(root)]\n"
        "from app.services.tournament_profile_access import "
        "purge_all_tournament_profile_access_cache\n"
        "count = asyncio.run(purge_all_tournament_profile_access_cache())\n"
        "if isinstance(count, bool) or not isinstance(count, int) or count < 0:\n"
        "    raise SystemExit(2)\n"
        "print(count)\n"
    )
    try:
        result = subprocess.run(
            [str(python), "-I", "-B", "-c", child_code, str(current_release)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=child_env,
            cwd="/",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("restore profile-access cache purge did not complete") from exc
    if result.returncode != 0 or re.fullmatch(r"(?:0|[1-9][0-9]{0,5})\n", result.stdout) is None:
        raise RuntimeError("restore profile-access cache purge did not complete")
    return int(result.stdout)


def verify_existing_backup(
    app_dir: Path,
    *,
    max_age_hours: float,
    private_failure_diagnostics: bool = False,
) -> dict[str, Any]:
    """Verify the newest existing backup while the caller holds maintenance locks."""

    if not math.isfinite(max_age_hours) or max_age_hours <= 0:
        raise ValueError("backup max age must be finite and positive")
    script = Path(__file__).with_name("platform_backup_restore_drill.py")
    shared_dir = app_dir / "shared"
    result = _run_backup_command(
        [
            sys.executable,
            "-B",
            str(script),
            "--env-file",
            str(shared_dir / ".env.platform"),
            "--output-dir",
            str(shared_dir / "backups"),
            "--verify-latest-existing",
            "--max-age-hours",
            str(max_age_hours),
            "--preserve-existing",
            "--json",
        ],
        forward_failure_diagnostics=private_failure_diagnostics,
    )
    return {
        "status": "verified-existing",
        "verified_existing": True,
        "created": False,
        "rotation_mode": "preserve-existing",
        "removed_count": 0,
        **result,
    }


def write_build_node_cache_receipt(
    app_dir: Path,
    *,
    phase: str,
    record: dict[str, Any],
    run_id: str,
    run_attempt: str,
    source_sha: str,
    bundle_sha256: str,
) -> str:
    """Durably record only closed cache-eviction identity and byte fields."""

    if os.geteuid() != 0:
        raise RuntimeError("build Node cache receipts require root")
    if phase not in {"intent", "completion"}:
        raise ValueError("build Node cache receipt phase is invalid")
    if re.fullmatch(r"[1-9][0-9]{0,19}", run_id) is None or re.fullmatch(
        r"[1-9][0-9]{0,5}", run_attempt
    ) is None:
        raise ValueError("build Node cache receipt run identity is invalid")
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None or re.fullmatch(
        r"[0-9a-f]{64}", bundle_sha256
    ) is None:
        raise ValueError("build Node cache receipt source binding is invalid")

    intent_keys = {
        "schema",
        "event",
        "node_version",
        "cache_dev",
        "cache_ino",
        "manifest_tree_sha256",
        "total_bytes",
        "status",
    }
    if phase == "intent":
        if set(record) != intent_keys or record.get("status") != "intent":
            raise ValueError("build Node cache intent record is not closed")
    else:
        status = record.get("status")
        expected_keys = intent_keys | {"reclaimed_bytes"}
        if status == "removed":
            expected_keys.add("regeneration")
        if set(record) != expected_keys or status not in {"removed", "already-absent"}:
            raise ValueError("build Node cache completion record is not closed")
        if status == "removed" and record.get("regeneration") != "pinned_archive_required":
            raise ValueError("build Node cache regeneration binding is invalid")
    if (
        type(record.get("schema")) is not int
        or record["schema"] != 1
        or record.get("event") != "build_node_cache_eviction"
        or record.get("node_version") != live_qa_guard.NODE_VERSION
        or type(record.get("total_bytes")) is not int
        or record["total_bytes"] < 0
    ):
        raise ValueError("build Node cache receipt fields are invalid")
    cache_dev = record.get("cache_dev")
    cache_ino = record.get("cache_ino")
    tree_sha = record.get("manifest_tree_sha256")
    if cache_dev is None:
        if cache_ino is not None or tree_sha is not None or record["total_bytes"] != 0:
            raise ValueError("absent build Node cache identity is inconsistent")
    elif (
        type(cache_dev) is not int
        or cache_dev < 0
        or type(cache_ino) is not int
        or cache_ino <= 0
        or not isinstance(tree_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", tree_sha) is None
    ):
        raise ValueError("build Node cache identity is invalid")
    if phase == "completion":
        reclaimed = record.get("reclaimed_bytes")
        if type(reclaimed) is not int or reclaimed < 0:
            raise ValueError("build Node cache reclaimed-byte count is invalid")
        if record["status"] == "removed" and reclaimed != record["total_bytes"]:
            raise ValueError("build Node cache reclaimed-byte count changed")
        if record["status"] == "already-absent" and (
            cache_dev is not None or reclaimed != 0
        ):
            raise ValueError("absent build Node cache completion is inconsistent")

    shared_dir = app_dir / "shared"
    try:
        shared_before = shared_dir.lstat()
        shared_fd = os.open(
            shared_dir,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise RuntimeError("shared directory is unavailable for cache receipt") from exc
    try:
        shared_open = os.fstat(shared_fd)
        if (
            not stat.S_ISDIR(shared_before.st_mode)
            or not stat.S_ISDIR(shared_open.st_mode)
            or (shared_before.st_dev, shared_before.st_ino)
            != (shared_open.st_dev, shared_open.st_ino)
            or shared_open.st_uid != 0
            or shared_open.st_gid != 0
            or shared_open.st_nlink < 2
            or stat.S_IMODE(shared_open.st_mode) & 0o022
        ):
            raise RuntimeError("shared directory is unsafe for cache receipt")
        receipt_name = (
            f"build-node-cache-eviction-{run_id}-{run_attempt}.{phase}.json"
        )
        payload = {
            "schema": 1,
            "event": "build_node_cache_eviction",
            "phase": phase,
            "run_id": int(run_id),
            "run_attempt": int(run_attempt),
            "source_sha": source_sha,
            "bundle_sha256": bundle_sha256,
            **record,
            "recorded_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "ascii"
        )
        fd = os.open(
            receipt_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=shared_fd,
        )
        try:
            opened = os.fstat(fd)
            entry_stat = os.stat(
                receipt_name, dir_fd=shared_fd, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != 0
                or opened.st_gid != 0
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
                or (opened.st_dev, opened.st_ino)
                != (entry_stat.st_dev, entry_stat.st_ino)
            ):
                raise RuntimeError("cache receipt file metadata is unsafe")
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise RuntimeError("cache receipt write made no progress")
                view = view[written:]
            os.fsync(fd)
            after = os.fstat(fd)
            if (
                after.st_dev != opened.st_dev
                or after.st_ino != opened.st_ino
                or not stat.S_ISREG(after.st_mode)
                or after.st_uid != 0
                or after.st_gid != 0
                or after.st_nlink != 1
                or stat.S_IMODE(after.st_mode) != 0o600
                or after.st_size != len(encoded)
            ):
                raise RuntimeError("cache receipt file changed while writing")
            verify_fd = os.open(
                receipt_name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=shared_fd,
            )
            try:
                verify_before = os.fstat(verify_fd)
                chunks = bytearray()
                while len(chunks) <= len(encoded):
                    chunk = os.read(verify_fd, min(4096, len(encoded) + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                verify_after = os.fstat(verify_fd)
                if (
                    (verify_before.st_dev, verify_before.st_ino)
                    != (opened.st_dev, opened.st_ino)
                    or (verify_after.st_dev, verify_after.st_ino)
                    != (opened.st_dev, opened.st_ino)
                    or bytes(chunks) != encoded
                ):
                    raise RuntimeError("cache receipt readback does not match")
            finally:
                os.close(verify_fd)
            os.fsync(shared_fd)
            shared_after = os.fstat(shared_fd)
            shared_path_after = shared_dir.lstat()
            if (
                (shared_after.st_dev, shared_after.st_ino)
                != (shared_open.st_dev, shared_open.st_ino)
                or (shared_path_after.st_dev, shared_path_after.st_ino)
                != (shared_open.st_dev, shared_open.st_ino)
            ):
                raise RuntimeError("shared directory changed while writing cache receipt")
        finally:
            os.close(fd)
        return receipt_name
    finally:
        os.close(shared_fd)


def _compaction_record_bytes(record: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(record),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _runtime_cache_compaction_receipt_name(
    run_id: str, run_attempt: str, phase: str
) -> str:
    if (
        re.fullmatch(r"[1-9][0-9]{0,19}", run_id) is None
        or re.fullmatch(r"[1-9][0-9]{0,5}", run_attempt) is None
        or phase not in {"intent", "validated", "completion"}
    ):
        raise ValueError("runtime cache compaction receipt identity is invalid")
    return f"liveqa-fallback-cache-compaction-{run_id}-{run_attempt}.{phase}.json"


def _validate_runtime_cache_compaction_record(
    record: Mapping[str, Any],
    *,
    phase: str,
    run_id: str,
    run_attempt: str,
    source_sha: str,
) -> None:
    common = {
        "schema",
        "event",
        "run_id",
        "attempt",
        "operation_source_sha",
        "source_commit",
        "cache_dev",
        "cache_ino",
        "old_tree_sha256",
        "old_manifest_sha256",
        "old_manifest",
        "old_manifest_raw_b64",
        "old_chromium_tree_sha256",
        "old_chromium_inventory_sha256",
        "old_chromium_entry_count",
        "old_chromium_dev",
        "old_chromium_ino",
        "old_chromium_allocated_bytes",
        "old_sandbox_sha256",
        "old_non_chromium_tree_sha256",
        "rollback_name",
        "phase",
    }
    phase_fields = {
        "intent": common | {"old_chromium_inventory"},
        "validated": common
        | {
            "intent_sha256",
            "new_tree_sha256",
            "new_manifest_sha256",
        },
        "completion": common
        | {
            "intent_sha256",
            "new_tree_sha256",
            "new_manifest_sha256",
            "reclaimed_bytes",
            "result",
        },
    }
    if phase not in phase_fields or set(record) != phase_fields[phase]:
        raise ValueError("runtime cache compaction record fields are not closed")
    record_phase = "complete" if phase == "completion" else phase
    if (
        type(record.get("schema")) is not int
        or record.get("schema") != 1
        or record.get("event") != FALLBACK_CACHE_COMPACTION_EVENT
        or record.get("phase") != record_phase
        or type(record.get("run_id")) is not int
        or record.get("run_id") != int(run_id)
        or type(record.get("attempt")) is not int
        or record.get("attempt") != int(run_attempt)
        or record.get("operation_source_sha") != source_sha
        or record.get("source_commit")
        != LEGACY_FALLBACK_RUNTIME_COMMIT
    ):
        raise ValueError("runtime cache compaction receipt binding is invalid")
    for key in (
        "old_tree_sha256",
        "old_manifest_sha256",
        "old_chromium_tree_sha256",
        "old_chromium_inventory_sha256",
        "old_sandbox_sha256",
        "old_non_chromium_tree_sha256",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", str(record.get(key, ""))) is None:
            raise ValueError("runtime cache compaction receipt digest is invalid")
    for key in (
        "cache_dev",
        "cache_ino",
        "old_chromium_dev",
        "old_chromium_ino",
    ):
        if type(record.get(key)) is not int or int(record[key]) < 0:
            raise ValueError("runtime cache compaction receipt identity is invalid")
    for key in ("old_chromium_entry_count", "old_chromium_allocated_bytes"):
        if type(record.get(key)) is not int or int(record[key]) < 0:
            raise ValueError("runtime cache compaction receipt byte/count field is invalid")
    rollback = record.get("rollback_name")
    if not isinstance(rollback, str) or re.fullmatch(
        rf"\.runtime-{LEGACY_FALLBACK_RUNTIME_COMMIT}\.chromium-compaction-[0-9a-f]{{32}}",
        rollback,
    ) is None:
        raise ValueError("runtime cache compaction rollback name is invalid")
    if phase == "intent":
        if not isinstance(record.get("old_manifest"), dict) or not isinstance(
            record.get("old_chromium_inventory"), list
        ):
            raise ValueError("runtime cache compaction inventory is invalid")
        if record.get("old_chromium_entry_count") != len(
            record["old_chromium_inventory"]
        ):
            raise ValueError("runtime cache compaction inventory count is invalid")
    encoded_manifest = record.get("old_manifest_raw_b64")
    if (
        not isinstance(encoded_manifest, str)
        or len(encoded_manifest) > ((live_qa_guard.MAX_JSON_BYTES + 2) // 3) * 4
    ):
        raise ValueError("runtime cache compaction manifest bytes exceed their bound")
    try:
        old_manifest_raw = base64.b64decode(encoded_manifest, validate=True)
        parsed_manifest = live_qa_guard._canonical_cache_manifest(old_manifest_raw)
    except (ValueError, base64.binascii.Error, live_qa_guard.GuardError) as exc:
        raise ValueError("runtime cache compaction manifest bytes are invalid") from exc
    if (
        len(old_manifest_raw) > live_qa_guard.MAX_JSON_BYTES
        or base64.b64encode(old_manifest_raw).decode("ascii") != encoded_manifest
        or parsed_manifest != record.get("old_manifest")
        or hashlib.sha256(old_manifest_raw).hexdigest()
        != record.get("old_manifest_sha256")
        or parsed_manifest.get("tree_sha256") != record.get("old_tree_sha256")
        or parsed_manifest.get("source_commit")
        != LEGACY_FALLBACK_RUNTIME_COMMIT
    ):
        raise ValueError("runtime cache compaction manifest bytes do not match")
    if phase in {"validated", "completion"}:
        for key in ("intent_sha256", "new_tree_sha256", "new_manifest_sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", str(record.get(key, ""))) is None:
                raise ValueError("runtime cache compaction validated digest is invalid")
    if phase == "completion" and (
        type(record.get("reclaimed_bytes")) is not int
        or int(record["reclaimed_bytes"]) < 0
        or record.get("result") not in {"compacted", "restored"}
    ):
        raise ValueError("runtime cache compaction completion is invalid")


def write_runtime_cache_compaction_receipt(
    app_dir: Path,
    *,
    phase: str,
    record: Mapping[str, Any],
    run_id: str,
    run_attempt: str,
    source_sha: str,
    bundle_sha256: str,
) -> str:
    """Write one bounded, durable source/run-bound compaction phase receipt."""

    if os.geteuid() != 0:
        raise RuntimeError("runtime cache compaction receipts require root")
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None or re.fullmatch(
        r"[0-9a-f]{64}", bundle_sha256
    ) is None:
        raise ValueError("runtime cache compaction artifact binding is invalid")
    _validate_runtime_cache_compaction_record(
        record,
        phase=phase,
        run_id=run_id,
        run_attempt=run_attempt,
        source_sha=source_sha,
    )
    encoded_record = _compaction_record_bytes(record)
    limit = (
        FALLBACK_CACHE_COMPACTION_MAX_INTENT_BYTES
        if phase == "intent"
        else FALLBACK_CACHE_COMPACTION_MAX_FINAL_BYTES
    )
    if len(encoded_record) > limit:
        raise ValueError("runtime cache compaction receipt exceeds its bound")
    payload = {
        "schema": 1,
        "event": FALLBACK_CACHE_COMPACTION_EVENT,
        "phase": phase,
        "run_id": int(run_id),
        "run_attempt": int(run_attempt),
        "source_sha": source_sha,
        "bundle_sha256": bundle_sha256,
        "record_sha256": hashlib.sha256(encoded_record).hexdigest(),
        "record": dict(record),
    }
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("ascii")
    if len(encoded) > limit:
        raise ValueError("runtime cache compaction envelope exceeds its bound")

    shared_dir = app_dir / "shared"
    try:
        before = shared_dir.lstat()
        shared_fd = os.open(
            shared_dir,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise RuntimeError("shared directory is unavailable for compaction receipt") from exc
    try:
        opened_dir = os.fstat(shared_fd)
        if (
            not stat.S_ISDIR(before.st_mode)
            or not stat.S_ISDIR(opened_dir.st_mode)
            or (before.st_dev, before.st_ino) != (opened_dir.st_dev, opened_dir.st_ino)
            or opened_dir.st_uid != 0
            or opened_dir.st_gid != 0
            or opened_dir.st_nlink < 2
            or stat.S_IMODE(opened_dir.st_mode) & 0o022
        ):
            raise RuntimeError("shared directory is unsafe for compaction receipt")
        name = _runtime_cache_compaction_receipt_name(run_id, run_attempt, phase)
        fd = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=shared_fd,
        )
        try:
            opened = os.fstat(fd)
            entry = os.stat(name, dir_fd=shared_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != 0
                or opened.st_gid != 0
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
                or (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino)
            ):
                raise RuntimeError("compaction receipt file metadata is unsafe")
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise RuntimeError("compaction receipt write made no progress")
                view = view[written:]
            os.fsync(fd)
            after = os.fstat(fd)
            if (
                (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                or after.st_uid != 0
                or after.st_gid != 0
                or after.st_nlink != 1
                or stat.S_IMODE(after.st_mode) != 0o600
                or after.st_size != len(encoded)
            ):
                raise RuntimeError("compaction receipt changed while writing")
            os.fsync(shared_fd)
            final_dir = os.fstat(shared_fd)
            final_path = os.lstat(shared_dir)
            if (
                (final_dir.st_dev, final_dir.st_ino)
                != (opened_dir.st_dev, opened_dir.st_ino)
                or (final_path.st_dev, final_path.st_ino)
                != (opened_dir.st_dev, opened_dir.st_ino)
            ):
                raise RuntimeError("shared directory changed while writing receipt")
        finally:
            os.close(fd)
        return name
    finally:
        os.close(shared_fd)


def read_runtime_cache_compaction_receipt(
    app_dir: Path,
    *,
    phase: str,
    run_id: str,
    run_attempt: str,
    source_sha: str,
    bundle_sha256: str,
) -> dict[str, Any] | None:
    """Read one exact receipt; absence is distinct from malformed state."""

    shared_dir = app_dir / "shared"
    name = _runtime_cache_compaction_receipt_name(run_id, run_attempt, phase)
    try:
        shared_before = shared_dir.lstat()
        shared_fd = os.open(
            shared_dir,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise RuntimeError("shared directory is unavailable for compaction receipt") from exc
    try:
        shared_open = os.fstat(shared_fd)
        if (
            not stat.S_ISDIR(shared_before.st_mode)
            or (shared_before.st_dev, shared_before.st_ino)
            != (shared_open.st_dev, shared_open.st_ino)
            or shared_open.st_uid != 0
            or shared_open.st_gid != 0
            or stat.S_IMODE(shared_open.st_mode) & 0o022
        ):
            raise RuntimeError("shared directory is unsafe for compaction receipt")
        try:
            entry = os.stat(name, dir_fd=shared_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        limit = (
            FALLBACK_CACHE_COMPACTION_MAX_INTENT_BYTES
            if phase == "intent"
            else FALLBACK_CACHE_COMPACTION_MAX_FINAL_BYTES
        )
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != 0
            or entry.st_gid != 0
            or entry.st_nlink != 1
            or stat.S_IMODE(entry.st_mode) != 0o600
            or entry.st_size <= 0
            or entry.st_size > limit
        ):
            raise RuntimeError("compaction receipt metadata is unsafe")
        fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=shared_fd,
        )
        try:
            before = os.fstat(fd)
            def fingerprint(value: os.stat_result) -> tuple[int, ...]:
                return (
                    value.st_dev,
                    value.st_ino,
                    value.st_mode,
                    value.st_uid,
                    value.st_gid,
                    value.st_nlink,
                    value.st_size,
                    value.st_mtime_ns,
                    value.st_ctime_ns,
                )
            if fingerprint(before) != fingerprint(entry):
                raise RuntimeError("compaction receipt changed while opening")
            chunks = bytearray()
            while len(chunks) <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            after = os.fstat(fd)
            final_entry = os.stat(name, dir_fd=shared_fd, follow_symlinks=False)
            if (
                len(chunks) > limit
                or fingerprint(before) != fingerprint(after)
                or fingerprint(before) != fingerprint(final_entry)
            ):
                raise RuntimeError("compaction receipt changed while reading")
        finally:
            os.close(fd)
        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate receipt key")
                result[key] = value
            return result
        payload = json.loads(bytes(chunks), object_pairs_hook=reject_duplicates)
        expected_keys = {
            "schema",
            "event",
            "phase",
            "run_id",
            "run_attempt",
            "source_sha",
            "bundle_sha256",
            "record_sha256",
            "record",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected_keys
            or type(payload.get("schema")) is not int
            or payload.get("schema") != 1
            or payload.get("event") != FALLBACK_CACHE_COMPACTION_EVENT
            or payload.get("phase") != phase
            or payload.get("run_id") != int(run_id)
            or type(payload.get("run_id")) is not int
            or payload.get("run_attempt") != int(run_attempt)
            or type(payload.get("run_attempt")) is not int
            or payload.get("source_sha") != source_sha
            or payload.get("bundle_sha256") != bundle_sha256
            or not isinstance(payload.get("record"), dict)
        ):
            raise RuntimeError("compaction receipt binding is invalid")
        encoded_record = _compaction_record_bytes(payload["record"])
        if hashlib.sha256(encoded_record).hexdigest() != payload.get("record_sha256"):
            raise RuntimeError("compaction receipt record digest is invalid")
        canonical = (
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("ascii")
        if canonical != bytes(chunks):
            raise RuntimeError("compaction receipt encoding is not canonical")
        _validate_runtime_cache_compaction_record(
            payload["record"],
            phase=phase,
            run_id=run_id,
            run_attempt=run_attempt,
            source_sha=source_sha,
        )
        return payload["record"]
    finally:
        os.close(shared_fd)


def _runtime_cache_compaction_summary(
    record: Mapping[str, Any],
    *,
    receipt_names: Mapping[str, str],
    resumed_from: tuple[str, str] | None = None,
) -> dict[str, Any]:
    if record.get("phase") != "complete" or record.get("result") not in {
        "compacted",
        "restored",
    }:
        raise RuntimeError("runtime cache compaction did not complete")
    result: dict[str, Any] = {
        "status": "completed",
        "result": record["result"],
        "reclaimed_bytes": record.get("reclaimed_bytes"),
        "source_commit": record.get("source_commit"),
        "old_tree_sha256": record.get("old_tree_sha256"),
        "new_tree_sha256": record.get("new_tree_sha256"),
        "intent_receipt": receipt_names.get("intent"),
        "validated_receipt": receipt_names.get("validated"),
        "completion_receipt": receipt_names.get("completion"),
    }
    if resumed_from is not None:
        result["resumed_run_id"] = resumed_from[0]
        result["resumed_run_attempt"] = resumed_from[1]
    return result


def compact_legacy_fallback_runtime_cache(
    app_dir: Path,
    *,
    run_id: str,
    run_attempt: str,
    source_sha: str,
    bundle_sha256: str,
    resume_run_id: str | None = None,
    resume_run_attempt: str | None = None,
    resume_source_sha: str | None = None,
    resume_bundle_sha256: str | None = None,
) -> dict[str, Any]:
    """Compact or recover only the fixed 4a fallback under caller-held locks."""

    if (
        re.fullmatch(r"[1-9][0-9]{0,19}", run_id) is None
        or re.fullmatch(r"[1-9][0-9]{0,5}", run_attempt) is None
        or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
        or re.fullmatch(r"[0-9a-f]{64}", bundle_sha256) is None
    ):
        raise ValueError("runtime cache compaction operation binding is invalid")
    receipt_names: dict[str, str] = {}
    last_records: dict[str, dict[str, Any]] = {}

    def writer(phase: str) -> Callable[[Mapping[str, object]], None]:
        def write(record: Mapping[str, object]) -> None:
            if phase in receipt_names:
                raise RuntimeError("runtime cache compaction receipt phase repeated")
            name = write_runtime_cache_compaction_receipt(
                app_dir,
                phase=phase,
                record=record,
                run_id=run_id,
                run_attempt=run_attempt,
                source_sha=source_sha,
                bundle_sha256=bundle_sha256,
            )
            receipt_names[phase] = name
            last_records[phase] = dict(record)

        return write

    resumed_from: tuple[str, str] | None = None
    resumed_compacted = False
    if resume_run_id is not None:
        if (
            resume_run_attempt is None
            or resume_source_sha is None
            or resume_bundle_sha256 is None
            or resume_source_sha != source_sha
        ):
            raise ValueError("runtime cache recovery must use the exact current source SHA")
        intent = read_runtime_cache_compaction_receipt(
            app_dir,
            phase="intent",
            run_id=resume_run_id,
            run_attempt=resume_run_attempt,
            source_sha=resume_source_sha,
            bundle_sha256=resume_bundle_sha256,
        )
        validated = read_runtime_cache_compaction_receipt(
            app_dir,
            phase="validated",
            run_id=resume_run_id,
            run_attempt=resume_run_attempt,
            source_sha=resume_source_sha,
            bundle_sha256=resume_bundle_sha256,
        )
        prior_completion = read_runtime_cache_compaction_receipt(
            app_dir,
            phase="completion",
            run_id=resume_run_id,
            run_attempt=resume_run_attempt,
            source_sha=resume_source_sha,
            bundle_sha256=resume_bundle_sha256,
        )
        if intent is None or prior_completion is not None:
            raise RuntimeError("runtime cache recovery receipt state is not pending")
        def write_recovery_completion(record: Mapping[str, object]) -> None:
            name = write_runtime_cache_compaction_receipt(
                app_dir,
                phase="completion",
                record=record,
                run_id=resume_run_id,
                run_attempt=resume_run_attempt,
                source_sha=resume_source_sha,
                bundle_sha256=resume_bundle_sha256,
            )
            receipt_names["completion"] = name
            last_records["completion"] = dict(record)

        recovered = live_qa_guard.recover_legacy_runtime_cache(
            intent,
            validated,
            run_id=int(resume_run_id),
            attempt=int(resume_run_attempt),
            operation_source_sha=resume_source_sha,
            write_completion=write_recovery_completion,
            probe_runtime=live_qa_guard.probe_compacted_legacy_runtime_cache,
        )
        if "completion" not in receipt_names:
            raise RuntimeError("runtime cache recovery completion receipt is missing")
        resumed_from = (resume_run_id, resume_run_attempt)
        resumed_compacted = recovered.get("result") == "compacted"
        if recovered.get("result") not in {"compacted", "restored"}:
            raise RuntimeError("runtime cache recovery outcome is invalid")
        persisted_completion = read_runtime_cache_compaction_receipt(
            app_dir,
            phase="completion",
            run_id=resume_run_id,
            run_attempt=resume_run_attempt,
            source_sha=resume_source_sha,
            bundle_sha256=resume_bundle_sha256,
        )
        if persisted_completion != last_records["completion"]:
            raise RuntimeError("runtime cache recovery receipt readback changed")
        if resumed_compacted:
            return _runtime_cache_compaction_summary(
                recovered,
                receipt_names={
                    "intent": _runtime_cache_compaction_receipt_name(
                        resume_run_id, resume_run_attempt, "intent"
                    ),
                    "validated": _runtime_cache_compaction_receipt_name(
                        resume_run_id, resume_run_attempt, "validated"
                    ),
                    "completion": receipt_names["completion"],
                },
                resumed_from=resumed_from,
            )
        receipt_names.clear()
        last_records.clear()

    if not resumed_compacted:
        current_result = live_qa_guard.compact_legacy_runtime_cache(
            run_id=int(run_id),
            attempt=int(run_attempt),
            operation_source_sha=source_sha,
            write_intent=writer("intent"),
            write_validated=writer("validated"),
            write_completion=writer("completion"),
            probe_runtime=live_qa_guard.probe_compacted_legacy_runtime_cache,
        )
        if set(receipt_names) != {"intent", "validated", "completion"}:
            raise RuntimeError("runtime cache compaction receipts are incomplete")
        for phase in ("intent", "validated", "completion"):
            persisted = read_runtime_cache_compaction_receipt(
                app_dir,
                phase=phase,
                run_id=run_id,
                run_attempt=run_attempt,
                source_sha=source_sha,
                bundle_sha256=bundle_sha256,
            )
            if persisted != last_records[phase]:
                raise RuntimeError("runtime cache compaction receipt readback changed")
        return _runtime_cache_compaction_summary(
            current_result, receipt_names=receipt_names, resumed_from=resumed_from
        )
    raise RuntimeError("runtime cache compaction state is invalid")


@contextmanager
def live_qa_machine_lock() -> Iterator[None]:
    """Join the existing live-QA machine lock after release/build locks."""

    descriptor = live_qa_guard._open_machine_lock()
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


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


def _open_dir_at(parent_fd: int, name: str) -> int:
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_fd,
    )


def _validate_source_lock_directory(fd: int, *, device: int) -> os.stat_result:
    opened = os.fstat(fd)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != 0
        or opened.st_gid != 0
        or stat.S_IMODE(opened.st_mode) != 0o755
        or opened.st_dev != device
        or opened.st_nlink < 2
    ):
        raise RuntimeError("unsafe canonical source build lock directory")
    return opened


def _ensure_canonical_source_release_lock_directory(path: Path) -> tuple[int, int]:
    """Create only the fixed builder lock path, with no-follow inode checks."""

    expected = DEFAULT_SOURCE_RELEASE_DIR
    platform_root = DEFAULT_PLATFORM_SOURCE_ROOT
    if path != expected or expected != platform_root / "dist" / "releases":
        raise RuntimeError("source build lock initialization is not canonical")
    if not platform_root.is_absolute():
        raise RuntimeError("canonical platform source root must be absolute")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd = os.open("/", flags)
    descriptors = [root_fd]
    try:
        root_stat = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != 0
            or root_stat.st_gid != 0
            or root_stat.st_mode & 0o022
        ):
            raise RuntimeError("unsafe filesystem root for source build lock")
        device = root_stat.st_dev
        current_fd = root_fd
        parts = platform_root.parts[1:] + ("dist", "releases")
        for index, component in enumerate(parts):
            created = False
            try:
                child_fd = _open_dir_at(current_fd, component)
            except FileNotFoundError:
                # Only the two final, fixed builder directories may be created.
                if index < len(parts) - 2:
                    raise RuntimeError("canonical source lock parent is absent")
                os.mkdir(component, 0o755, dir_fd=current_fd)
                os.fsync(current_fd)
                child_fd = _open_dir_at(current_fd, component)
                created = True
            descriptors.append(child_fd)
            if created:
                # The workflow runs with umask 077; normalize only the inode
                # created by this call, never an existing directory.
                os.fchown(child_fd, 0, 0)
                os.fchmod(child_fd, 0o755)
                os.fsync(child_fd)
            child_stat = os.fstat(child_fd)
            path_stat = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(child_stat.st_mode)
                or stat.S_ISLNK(path_stat.st_mode)
                or (path_stat.st_dev, path_stat.st_ino)
                != (child_stat.st_dev, child_stat.st_ino)
                or child_stat.st_uid != 0
                or child_stat.st_gid != 0
                or child_stat.st_dev != device
                or child_stat.st_mode & 0o022
            ):
                raise RuntimeError("unsafe canonical source build lock parent")
            if component in {"dist", "releases"} and stat.S_IMODE(
                child_stat.st_mode
            ) != 0o755:
                raise RuntimeError("canonical source build directory mode mismatch")
            current_fd = child_fd
        final_stat = _validate_source_lock_directory(current_fd, device=device)
        return final_stat.st_dev, final_stat.st_ino
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def source_release_lock(
    path: Path, *, initialize_if_missing: bool = False
) -> Iterator[Path | None]:
    """Join the builder flock, optionally initializing its one fixed lock path."""

    expected_identity: tuple[int, int] | None = None
    if not os.path.lexists(path):
        if not initialize_if_missing:
            yield None
            return
        expected_identity = _ensure_canonical_source_release_lock_directory(path)
    elif initialize_if_missing:
        # The opt-in path validates even existing components before locking.
        expected_identity = _ensure_canonical_source_release_lock_directory(path)
    with exclusive_directory_lock(
        path, label="platform release build output"
    ) as resolved:
        if expected_identity is not None:
            current = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISDIR(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or (current.st_dev, current.st_ino) != expected_identity
                or resolved != path
            ):
                raise RuntimeError("canonical source build lock identity changed")
        yield resolved


@contextmanager
def maintenance_lock_scope(
    args: argparse.Namespace, *, app_dir: Path
) -> Iterator[Path | None]:
    """Hold maintenance locks in the only supported global order."""

    # Keep this order aligned with deploy/build/live-QA tooling:
    # release -> retained-load -> build -> live-QA.
    with release_operation_lock(app_dir):
        with exclusive_retained_load_lock():
            with source_release_lock(
                args.source_release_dir,
                initialize_if_missing=getattr(
                    args, "evict_pinned_build_node_cache", False
                ) or getattr(args, "compact_legacy_fallback_runtime_cache", False),
            ) as source_release_dir:
                yield source_release_dir


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
                **run_backup(
                    app_dir,
                    keep=args.backup_keep,
                    max_age_hours=getattr(args, "backup_max_age_hours", 24.0),
                    rotate_existing=True,
                ),
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
    if (
        getattr(args, "evict_pinned_build_node_cache", False)
        or getattr(args, "compact_legacy_fallback_runtime_cache", False)
        or getattr(args, "purge_profile_access_cache_after_restore", False)
    ):
        try:
            canonical_app_dir = DEFAULT_APP_DIR.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(
                "canonical backup verification app root is unavailable"
            ) from exc
        if (
            app_dir != canonical_app_dir
            or args.source_release_dir != DEFAULT_SOURCE_RELEASE_DIR
        ):
            raise RuntimeError(
                "canonical app/build lock roots are required for fixed cache maintenance"
            )
    disk_before_snapshot = disk_snapshot_for_path(Path("/"))

    if args.apply:
        # Fixed global order: release transaction lock, retained-load lock,
        # build-output lock, then live-QA machine lock. Install/rollback take
        # only the first; deploy takes the first two; builds take only the
        # third; standalone live-QA retention takes first then the fourth.
        with maintenance_lock_scope(args, app_dir=app_dir) as source_release_dir:
            if getattr(args, "purge_profile_access_cache_after_restore", False):
                try:
                    canonical_app_dir = DEFAULT_APP_DIR.resolve(strict=True)
                    canonical_build_dir = DEFAULT_SOURCE_RELEASE_DIR.resolve(strict=True)
                    releases_dir = (canonical_app_dir / "releases").resolve(strict=True)
                except OSError as exc:
                    raise RuntimeError(
                        "canonical restore cache purge lock roots are unavailable"
                    ) from exc
                if (
                    app_dir != canonical_app_dir
                    or source_release_dir is None
                    or source_release_dir != canonical_build_dir
                ):
                    raise RuntimeError(
                        "canonical app/build lock roots are required for restore cache purge"
                    )
                current_release = resolved_release_target(
                    app_dir, "current", releases_dir
                )
                with live_qa_machine_lock():
                    _require_restore_services_stopped()
                    purged_count = _purge_restored_profile_access_cache(
                        app_dir, current_release
                    )
                return {
                    "ok": True,
                    "status": "completed",
                    "mode": "restore-profile-access-cache-purge",
                    "purged_key_count": purged_count,
                    "services_stopped": True,
                }
            if getattr(args, "verify_existing_backup_only", False):
                # The lock owner never creates or rotates a backup in this mode.
                # The backup tool only updates the selected newest sidecar after
                # its existing archive passes the complete restore drill.
                cache_result: dict[str, Any] = {"status": "not-requested"}
                backup_result: dict[str, Any]
                if getattr(args, "evict_pinned_build_node_cache", False):
                    try:
                        canonical_app_dir = DEFAULT_APP_DIR.resolve(strict=True)
                        canonical_build_dir = DEFAULT_SOURCE_RELEASE_DIR.resolve(
                            strict=True
                        )
                    except OSError as exc:
                        raise RuntimeError(
                            "canonical backup verification lock roots are unavailable"
                        ) from exc
                    if (
                        app_dir != canonical_app_dir
                        or source_release_dir is None
                        or source_release_dir != canonical_build_dir
                    ):
                        raise RuntimeError(
                            "canonical app/build lock roots are required for pinned Node cache eviction"
                        )
                    # maintenance_lock_scope holds release, retained-load and
                    # source/build-output locks. Hold the final canonical lock
                    # across both cache eviction and the existing restore drill.
                    with live_qa_machine_lock():
                        intent_record: dict[str, Any] | None = None
                        receipt_names: dict[str, str] = {}

                        def write_intent(record: dict[str, Any]) -> None:
                            nonlocal intent_record
                            if intent_record is not None:
                                raise RuntimeError("cache eviction intent was already recorded")
                            receipt_names["intent"] = write_build_node_cache_receipt(
                                app_dir,
                                phase="intent",
                                record=record,
                                run_id=args.eviction_run_id,
                                run_attempt=args.eviction_run_attempt,
                                source_sha=args.eviction_source_sha,
                                bundle_sha256=args.eviction_bundle_sha256,
                            )
                            intent_record = dict(record)

                        def write_completion(record: dict[str, Any]) -> None:
                            if intent_record is None:
                                raise RuntimeError("cache eviction completion has no intent")
                            for key in (
                                "schema",
                                "event",
                                "node_version",
                                "cache_dev",
                                "cache_ino",
                                "manifest_tree_sha256",
                                "total_bytes",
                            ):
                                if record.get(key) != intent_record.get(key):
                                    raise RuntimeError(
                                        "cache eviction completion does not match its intent"
                                    )
                            receipt_names["completion"] = write_build_node_cache_receipt(
                                app_dir,
                                phase="completion",
                                record=record,
                                run_id=args.eviction_run_id,
                                run_attempt=args.eviction_run_attempt,
                                source_sha=args.eviction_source_sha,
                                bundle_sha256=args.eviction_bundle_sha256,
                            )

                        cache_result = platform_build_node_cache.evict_pinned_build_node_cache(
                            write_intent=write_intent,
                            write_completion=write_completion,
                        )
                        if set(receipt_names) != {"intent", "completion"}:
                            raise RuntimeError("cache eviction receipts are incomplete")
                        cache_result = {
                            **cache_result,
                            "intent_receipt": receipt_names["intent"],
                            "completion_receipt": receipt_names["completion"],
                            "run_id": args.eviction_run_id,
                            "run_attempt": args.eviction_run_attempt,
                            "source_sha": args.eviction_source_sha,
                            "bundle_sha256": args.eviction_bundle_sha256,
                        }
                        backup_result = verify_existing_backup(
                            app_dir,
                            max_age_hours=args.backup_max_age_hours,
                            private_failure_diagnostics=getattr(
                                args, "private_backup_diagnostics", False
                            ),
                        )
                elif getattr(args, "compact_legacy_fallback_runtime_cache", False):
                    try:
                        canonical_app_dir = DEFAULT_APP_DIR.resolve(strict=True)
                        canonical_build_dir = DEFAULT_SOURCE_RELEASE_DIR.resolve(
                            strict=True
                        )
                    except OSError as exc:
                        raise RuntimeError(
                            "canonical backup verification lock roots are unavailable"
                        ) from exc
                    if (
                        app_dir != canonical_app_dir
                        or source_release_dir is None
                        or source_release_dir != canonical_build_dir
                    ):
                        raise RuntimeError(
                            "canonical app/build lock roots are required for fallback cache compaction"
                        )
                    # All four canonical locks stay held across compaction,
                    # browser probes, receipts and the backup restore drill.
                    with live_qa_machine_lock():
                        cache_result = compact_legacy_fallback_runtime_cache(
                            app_dir,
                            run_id=args.compaction_run_id,
                            run_attempt=args.compaction_run_attempt,
                            source_sha=args.compaction_source_sha,
                            bundle_sha256=args.compaction_bundle_sha256,
                            resume_run_id=getattr(args, "resume_compaction_run_id", None),
                            resume_run_attempt=getattr(
                                args, "resume_compaction_run_attempt", None
                            ),
                            resume_source_sha=getattr(
                                args, "resume_compaction_source_sha", None
                            ),
                            resume_bundle_sha256=getattr(
                                args, "resume_compaction_bundle_sha256", None
                            ),
                        )
                        backup_result = verify_existing_backup(
                            app_dir,
                            max_age_hours=args.backup_max_age_hours,
                            private_failure_diagnostics=getattr(
                                args, "private_backup_diagnostics", False
                            ),
                        )
                else:
                    backup_result = verify_existing_backup(
                        app_dir,
                        max_age_hours=args.backup_max_age_hours,
                        private_failure_diagnostics=getattr(
                            args, "private_backup_diagnostics", False
                        ),
                    )
                maintenance_result = (
                    RetentionPlan((), (), ()),
                    ArtifactRetentionPlan((), (), ()),
                    (),
                    (),
                    (),
                    {
                        "build_node_cache": (
                            cache_result
                            if getattr(args, "evict_pinned_build_node_cache", False)
                            else {"status": "not-requested"}
                        ),
                        "fallback_runtime_cache_compaction": (
                            cache_result
                            if getattr(args, "compact_legacy_fallback_runtime_cache", False)
                            else {"status": "not-requested"}
                        ),
                        **backup_result,
                        "status": "completed",
                    },
                    {
                        "failed_builds": 0,
                        "browser_test_artifacts": 0,
                        "preprod_screenshots": 0,
                    },
                )
                live_qa_plan = live_qa_guard.RuntimeCacheRetentionPlan((), (), (), ())
            elif getattr(args, "backup_only", False):
                # Do not construct or apply release, artifact, transient or
                # live-QA retention plans in this mode. The backup owner may
                # Preserve every pre-existing archive; archive rotation belongs
                # only to the separate full-maintenance retention path.
                # A backup/restore failure exits before any deletion path.
                maintenance_result = (
                    RetentionPlan((), (), ()),
                    ArtifactRetentionPlan((), (), ()),
                    (),
                    (),
                    (),
                    {
                        "status": "completed",
                        **run_backup(
                            app_dir,
                            keep=args.backup_keep,
                            max_age_hours=getattr(args, "backup_max_age_hours", 24.0),
                            rotate_existing=False,
                            private_failure_diagnostics=getattr(
                                args, "private_backup_diagnostics", False
                            ),
                        ),
                    },
                    {
                        "failed_builds": 0,
                        "browser_test_artifacts": 0,
                        "preprod_screenshots": 0,
                    },
                )
                live_qa_plan = live_qa_guard.RuntimeCacheRetentionPlan((), (), (), ())
            else:
                maintenance_result = _plan_and_maybe_apply(
                    args,
                    app_dir=app_dir,
                    source_release_dir=source_release_dir,
                )
                live_qa_plan = (
                    live_qa_guard.prune_runtime_cache_release_lock_held(
                        apply=True,
                        keep=args.live_qa_runtime_keep,
                        root=getattr(
                            args,
                            "live_qa_runtime_root",
                            live_qa_guard.RUNNER_CACHE_ROOT,
                        ),
                        app_dir=app_dir,
                    )
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

    disk_after_snapshot = disk_snapshot_for_path(Path("/"))
    minimum_free_bytes = minimum_free_bytes_for_gib(args.minimum_free_gib)
    storage_ok = disk_is_healthy(
        disk_after_snapshot,
        min_free_bytes=minimum_free_bytes,
        max_used_percent=args.maximum_used_percent,
    )
    disk_before = disk_before_snapshot.as_dict()
    disk_after = disk_after_snapshot.as_dict()
    completed_at = datetime.now(UTC)
    return {
        "ok": storage_ok,
        "mode": (
            "verify-existing-backup-only"
            if getattr(args, "verify_existing_backup_only", False)
            else "backup-only"
            if getattr(args, "backup_only", False)
            else "apply" if args.apply else "dry-run"
        ),
        "started_at_utc": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at_utc": completed_at.isoformat().replace("+00:00", "Z"),
        "duration_seconds": round((completed_at - started_at).total_seconds(), 3),
        "backup": backup,
        "production_releases": release_plan_summary(production_plan),
        "source_release_artifacts": artifact_plan_summary(source_plan),
        "live_qa_runtime_caches": live_qa_runtime_plan_summary(live_qa_plan),
        "transient": {
            "failed_builds": {
                "count": len(failed_builds),
                "reclaimable_bytes": transient_reclaimed["failed_builds"],
            },
            "browser_test_artifacts": {
                "count": len(test_artifacts),
                "reclaimable_bytes": transient_reclaimed["browser_test_artifacts"],
            },
            "preprod_screenshots": {
                "count": len(screenshots),
                "reclaimable_bytes": transient_reclaimed["preprod_screenshots"],
            },
            "reclaimable_bytes": transient_reclaimed,
        },
        "disk_before": disk_before,
        "disk_after": disk_after,
        "limits": {
            "minimum_free_bytes": minimum_free_bytes,
            "maximum_used_percent": args.maximum_used_percent,
            "backup_keep": args.backup_keep,
            "backup_max_age_hours": getattr(args, "backup_max_age_hours", 24.0),
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
            f"{key}: delete={section['deleted_count']}, "
            f"reclaimable={human_bytes(section['reclaimable_bytes'])}"
        )
    live_qa = report["live_qa_runtime_caches"]
    print(
        "live_qa_runtime_caches: "
        f"delete={live_qa['deleted_count']}, "
        f"reclaim_tombstones={live_qa['reclaimed_tombstone_count']}"
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
        if args.apply and not getattr(
            args, "purge_profile_access_cache_after_restore", False
        ):
            write_report(
                args.app_dir / "shared" / "maintenance",
                report,
                keep=args.report_keep,
            )
        if args.as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_summary(report)
        return 0 if report["ok"] else 1
    except Exception as exc:
        message = str(exc).lower()
        if "backup" in message or "restore" in message:
            error_class = "backup"
        elif "lock" in message or "transaction" in message:
            error_class = "lock"
        elif "unsafe" in message or "symlink" in message or "target" in message:
            error_class = "integrity"
        elif "missing" in message or "directory" in message:
            error_class = "configuration"
        else:
            error_class = "storage"
        if args.as_json:
            result: dict[str, Any] = {
                "ok": False,
                "status": "failed",
                "error_class": error_class,
            }
            diagnostic = getattr(exc, "restore_diagnostic", None)
            if _valid_restore_diagnostic(diagnostic):
                result["restore_diagnostic"] = diagnostic
            print(
                json.dumps(result, ensure_ascii=False, indent=2)
            )
        else:
            print(f"[FAIL] Platform storage maintenance ({error_class})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
