#!/usr/bin/env python3
"""Safely migrate bounded legacy platform media into immutable R2 variants."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import sys
import tempfile
from typing import Any, Protocol
import urllib.parse
import urllib.request
from uuid import NAMESPACE_URL, uuid5


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLATFORM_ROOT.parent
sys.path.insert(0, str(PLATFORM_ROOT))

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.config import (
    PLATFORM_SCHEMA,
    PlatformSettings,
    get_settings,
    validate_platform_settings,
)
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.errors import MediaError
from python_packages.platform_infra.media.image_processor import (
    ImagePolicy,
    ImageProcessor,
    VARIANT_SPECS,
)
from python_packages.platform_infra.media.r2_storage import (
    IMMUTABLE_CACHE_CONTROL,
    MediaStorage,
    R2Storage,
    StoredObjectMetadata,
)
from python_packages.platform_infra.media.repository import (
    AssetDescriptor,
    MediaRepository,
)
from python_packages.platform_infra.media.service import MediaService
from python_packages.platform_infra.media.source_store import inspect_container
from python_packages.platform_infra.media.tasks import build_media_service
from python_packages.platform_infra.models import MediaAsset, PlayerProfile, Tournament
from python_packages.platform_infra.object_storage import object_key_from_upload_url
from python_packages.platform_infra.redis import redis_client
from tools.platform_backup_restore_drill import check_latest_backup


CHECKPOINT_VERSION = 1
DEFAULT_CHECKPOINT = Path(
    "/opt/oldsparky/platform/shared/media-migration/checkpoint.json"
)
DEFAULT_BACKUP_DIR = Path("/opt/oldsparky/platform/shared/backups")
DEFAULT_LIMIT = 100
MAX_LIMIT = 1_000
MAX_INVENTORY_RECORDS = 10_000
FILE_CHUNK_BYTES = 64 * 1024
DEFAULT_CLEANUP_GRACE_HOURS = 24 * 7
DEFAULT_VERIFY_MAX_AGE_HOURS = 24
CLEANUP_CONFIRMATION = "DELETE_VERIFIED_LEGACY_MEDIA"
MEDIA_PROCESSING_LOCK_KEY = "platform:media:processing-lock"
LOCK_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


class MigrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LegacyMediaCandidate:
    identity: str
    cursor: str
    purpose: str
    owner_id: str
    subject_type: str
    legacy_url: str
    active_asset_id: str | None
    source_kind: str
    source_key: str | None = None
    conflict_code: str | None = None


@dataclass(frozen=True)
class CandidateAnalysis:
    candidate: LegacyMediaCandidate
    ok: bool
    error_code: str | None = None
    source_path: Path | None = None
    source_mime: str | None = None
    source_bytes: int = 0
    source_sha256: str | None = None
    projected_variants: int = 0
    projected_output_bytes: int = 0


@dataclass(frozen=True)
class TargetState:
    active_asset_id: str | None
    legacy_url: str | None


class ServiceBuilder(Protocol):
    def __call__(self, db_session: AsyncSession) -> MediaService: ...


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory and migrate legacy platform avatars/banners into decoded, "
            "immutable R2 variants. Dry-run is the default and never mutates DB, R2, "
            "checkpoint, or originals."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="Inventory and estimate only.")
    modes.add_argument("--apply", action="store_true", help="Create variants and link them in DB.")
    modes.add_argument("--verify", action="store_true", help="Verify checkpointed DB/R2 assets.")
    modes.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete only verified legacy local originals after all safety gates pass.",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Protected platform environment file; PLATFORM_ENV_FILE is the fallback.",
    )
    parser.add_argument(
        "--resume-from",
        default=None,
        help="Process only deterministic cursors lexicographically after this reported cursor.",
    )
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--backup-max-age-hours", type=float, default=24.0)
    parser.add_argument(
        "--cleanup-grace-hours",
        type=float,
        default=float(DEFAULT_CLEANUP_GRACE_HOURS),
    )
    parser.add_argument(
        "--verify-max-age-hours",
        type=float,
        default=float(DEFAULT_VERIFY_MAX_AGE_HOURS),
    )
    parser.add_argument(
        "--confirm-cleanup",
        default="",
        help=f"Required only with --cleanup: {CLEANUP_CONFIRMATION}",
    )
    parser.add_argument(
        "--verify-cdn",
        action="store_true",
        help="Also perform one bounded direct-CDN GET per verified variant.",
    )
    parser.add_argument("--cdn-timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if not any((args.dry_run, args.apply, args.verify, args.cleanup)):
        args.dry_run = True
    validate_args(args)
    return args


def validate_args(args: argparse.Namespace) -> None:
    if args.limit < 1 or args.limit > MAX_LIMIT:
        raise MigrationError(
            "invalid_limit",
            f"--limit must be between 1 and {MAX_LIMIT}.",
        )
    if args.resume_from is not None:
        value = args.resume_from.strip()
        if not value or len(value) > 240 or any(ord(char) < 32 for char in value):
            raise MigrationError("invalid_resume_cursor", "--resume-from is invalid.")
        args.resume_from = value
    if args.cdn_timeout <= 0 or args.cdn_timeout > 60:
        raise MigrationError(
            "invalid_cdn_timeout",
            "--cdn-timeout must be greater than zero and at most 60 seconds.",
        )
    if args.backup_max_age_hours <= 0:
        raise MigrationError(
            "invalid_backup_age",
            "--backup-max-age-hours must be positive.",
        )
    if args.cleanup_grace_hours < 24:
        raise MigrationError(
            "invalid_cleanup_grace",
            "--cleanup-grace-hours must be at least 24 hours.",
        )
    if args.verify_max_age_hours <= 0:
        raise MigrationError(
            "invalid_verify_age",
            "--verify-max-age-hours must be positive.",
        )
    if args.cleanup:
        if args.confirm_cleanup != CLEANUP_CONFIRMATION:
            raise MigrationError(
                "cleanup_confirmation_required",
                f"--cleanup requires --confirm-cleanup {CLEANUP_CONFIRMATION}.",
            )
    elif args.confirm_cleanup:
        raise MigrationError(
            "unexpected_cleanup_confirmation",
            "--confirm-cleanup is valid only with --cleanup.",
        )
    if args.verify_cdn and not (args.verify or args.cleanup):
        raise MigrationError(
            "unexpected_cdn_verification",
            "--verify-cdn is valid only with --verify or --cleanup.",
        )


def load_env_file(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise MigrationError(
            "unsafe_env_file",
            "Platform env file must be a regular file, not a symlink.",
        )
    if stat.S_IMODE(path.stat().st_mode) & 0o007:
        raise MigrationError(
            "unsafe_env_file_permissions",
            "Platform env file must not be accessible to other users.",
        )
    os.environ.setdefault("PLATFORM_SHARED_DIR", str(path.resolve().parent))
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'\"")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def utc_iso(value: dt.datetime | None = None) -> str:
    return (value or utc_now()).astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def safe_relative_url_path(url: str, prefix: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return False
    if not parsed.path.startswith(prefix) or "\\" in parsed.path:
        return False
    path = PurePosixPath(parsed.path)
    return ".." not in path.parts and str(path) == parsed.path


def classify_legacy_reference(
    *,
    purpose: str,
    legacy_url: str,
    active_asset_id: str | None,
) -> tuple[str, str | None, str | None]:
    if active_asset_id:
        return "manual_conflict", None, "active_asset_and_legacy_reference"
    if safe_relative_url_path(legacy_url, "/assets/"):
        return "packaged_fallback", None, None
    parsed = urllib.parse.urlsplit(legacy_url)
    if parsed.query or parsed.fragment:
        return "manual_conflict", None, "legacy_reference_has_query_or_fragment"
    key = object_key_from_upload_url(legacy_url)
    if key is not None:
        expected_prefix = {
            "profile_avatar": "avatars/",
            "profile_banner": "profile-banners/",
            "tournament_banner": "tournament-covers/",
        }[purpose]
        if not key.startswith(expected_prefix):
            return "manual_conflict", None, "legacy_upload_prefix_mismatch"
        return "local_upload", key, None
    return "manual_conflict", None, "unsupported_legacy_reference"


def build_candidate(
    *,
    purpose: str,
    owner_id: str,
    subject_type: str,
    legacy_url: str,
    active_asset_id: str | None,
) -> LegacyMediaCandidate:
    source_kind, source_key, conflict_code = classify_legacy_reference(
        purpose=purpose,
        legacy_url=legacy_url,
        active_asset_id=active_asset_id,
    )
    priority = {
        "local_upload": "00",
        "manual_conflict": "10",
        "packaged_fallback": "20",
    }[source_kind]
    identity = f"{purpose}:{owner_id}"
    cursor = f"{priority}:{source_kind}:{identity}"
    return LegacyMediaCandidate(
        identity=identity,
        cursor=cursor,
        purpose=purpose,
        owner_id=owner_id,
        subject_type=subject_type,
        legacy_url=legacy_url,
        active_asset_id=active_asset_id,
        source_kind=source_kind,
        source_key=source_key,
        conflict_code=conflict_code,
    )


async def load_inventory(db_session: AsyncSession) -> list[LegacyMediaCandidate]:
    profile_rows = (
        await db_session.execute(
            select(
                PlayerProfile.user_id,
                PlayerProfile.avatar_url,
                PlayerProfile.avatar_asset_id,
                PlayerProfile.banner_url,
                PlayerProfile.banner_asset_id,
            )
            .where(
                or_(
                    PlayerProfile.avatar_url.is_not(None),
                    PlayerProfile.banner_url.is_not(None),
                )
            )
            .order_by(PlayerProfile.user_id)
            .limit(MAX_INVENTORY_RECORDS + 1)
        )
    ).all()
    tournament_rows = (
        await db_session.execute(
            select(Tournament.id, Tournament.cover_url, Tournament.banner_asset_id)
            .where(Tournament.cover_url.is_not(None))
            .order_by(Tournament.id)
            .limit(MAX_INVENTORY_RECORDS + 1)
        )
    ).all()
    if len(profile_rows) > MAX_INVENTORY_RECORDS or len(tournament_rows) > MAX_INVENTORY_RECORDS:
        raise MigrationError(
            "inventory_bound_exceeded",
            f"Media inventory exceeds the {MAX_INVENTORY_RECORDS}-row safety bound.",
        )

    candidates: list[LegacyMediaCandidate] = []
    for user_id, avatar_url, avatar_asset_id, banner_url, banner_asset_id in profile_rows:
        if avatar_url:
            candidates.append(
                build_candidate(
                    purpose="profile_avatar",
                    owner_id=str(user_id),
                    subject_type="profile",
                    legacy_url=str(avatar_url),
                    active_asset_id=str(avatar_asset_id) if avatar_asset_id else None,
                )
            )
        if banner_url:
            candidates.append(
                build_candidate(
                    purpose="profile_banner",
                    owner_id=str(user_id),
                    subject_type="profile",
                    legacy_url=str(banner_url),
                    active_asset_id=str(banner_asset_id) if banner_asset_id else None,
                )
            )
    for tournament_id, cover_url, banner_asset_id in tournament_rows:
        candidates.append(
            build_candidate(
                purpose="tournament_banner",
                owner_id=str(tournament_id),
                subject_type="tournament",
                legacy_url=str(cover_url),
                active_asset_id=str(banner_asset_id) if banner_asset_id else None,
            )
        )
    return sorted(candidates, key=lambda candidate: candidate.cursor)


def after_resume[T](items: list[T], *, resume_from: str | None, cursor: Callable[[T], str]) -> list[T]:
    if resume_from is None:
        return items
    return [item for item in items if cursor(item) > resume_from]


def production_image_processor(settings: PlatformSettings) -> ImageProcessor:
    return ImageProcessor(
        ImagePolicy(
            max_pixels=settings.platform_media_max_pixels,
            max_dimension=settings.platform_media_max_dimension,
            max_variant_bytes=settings.platform_media_max_variant_bytes,
            processing_timeout_seconds=settings.platform_media_processing_timeout_seconds,
        )
    )


def resolve_local_original(
    candidate: LegacyMediaCandidate,
    *,
    upload_root: Path,
) -> Path:
    if candidate.source_kind != "local_upload" or not candidate.source_key:
        raise MigrationError("not_local_upload", "Candidate is not a legacy local upload.")
    root = upload_root.resolve()
    key_path = PurePosixPath(candidate.source_key)
    if key_path.is_absolute() or ".." in key_path.parts:
        raise MigrationError("unsafe_source_path", "Legacy source key is unsafe.")
    source = root.joinpath(*key_path.parts)
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise MigrationError("unsafe_source_path", "Legacy source escaped upload root.") from exc
    try:
        metadata = source.lstat()
    except FileNotFoundError as exc:
        raise MigrationError("source_missing", "Legacy local original is missing.") from exc
    if source.is_symlink() or not source.is_file() or metadata.st_nlink < 1:
        raise MigrationError("unsafe_source_path", "Legacy source is not a regular file.")
    resolved = source.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MigrationError("unsafe_source_path", "Legacy source escaped upload root.") from exc
    return resolved


def hash_bounded_file(path: Path, *, max_bytes: int) -> tuple[int, str]:
    descriptor = open_regular_file(path)
    with os.fdopen(descriptor, "rb") as source:
        return hash_bounded_stream(source, max_bytes=max_bytes)


def open_regular_file(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MigrationError(
            "unsafe_source_path",
            "Legacy source could not be opened without following links.",
        ) from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink < 1:
        os.close(descriptor)
        raise MigrationError("unsafe_source_path", "Legacy source is not a regular file.")
    return descriptor


def hash_bounded_stream(source, *, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    for chunk in iter(lambda: source.read(FILE_CHUNK_BYTES), b""):
        total += len(chunk)
        if total > max_bytes:
            raise MigrationError(
                "media_too_large",
                "Legacy original exceeds the configured media input bound.",
            )
        digest.update(chunk)
    if total == 0:
        raise MigrationError("empty_media", "Legacy original is empty.")
    return total, digest.hexdigest()


def analyze_candidate(
    candidate: LegacyMediaCandidate,
    *,
    settings: PlatformSettings,
    processor: ImageProcessor,
) -> CandidateAnalysis:
    if candidate.source_kind != "local_upload":
        return CandidateAnalysis(
            candidate=candidate,
            ok=False,
            error_code=candidate.conflict_code or candidate.source_kind,
        )
    try:
        source_path = resolve_local_original(
            candidate,
            upload_root=settings.platform_upload_dir,
        )
        source_bytes, source_sha256 = hash_bounded_file(
            source_path,
            max_bytes=settings.platform_media_max_input_bytes,
        )
        source_mime = inspect_container(source_path)
        projected_asset_id = str(
            uuid5(
                NAMESPACE_URL,
                f"oldsparky-media-migration:{candidate.identity}:{source_sha256}",
            )
        )
        variants = processor.process(
            source_path,
            purpose=candidate.purpose,
            asset_id=projected_asset_id,
            owner_id=candidate.owner_id,
        )
        return CandidateAnalysis(
            candidate=candidate,
            ok=True,
            source_path=source_path,
            source_mime=source_mime,
            source_bytes=source_bytes,
            source_sha256=source_sha256,
            projected_variants=len(variants),
            projected_output_bytes=sum(variant.byte_size for variant in variants),
        )
    except (MigrationError, MediaError, ValueError) as exc:
        return CandidateAnalysis(
            candidate=candidate,
            ok=False,
            error_code=getattr(exc, "code", "invalid_source"),
        )
    except Exception:
        return CandidateAnalysis(
            candidate=candidate,
            ok=False,
            error_code="analysis_failed",
        )


def inventory_summary(candidates: list[LegacyMediaCandidate]) -> dict[str, Any]:
    by_purpose = Counter(candidate.purpose for candidate in candidates)
    by_source = Counter(candidate.source_kind for candidate in candidates)
    return {
        "legacy_references": len(candidates),
        "by_purpose": dict(sorted(by_purpose.items())),
        "by_source": dict(sorted(by_source.items())),
        "packaged_fallback_retained": by_source["packaged_fallback"],
        "local_uploads": by_source["local_upload"],
        "manual_conflicts": by_source["manual_conflict"],
    }


def next_resume_cursor[T](selected: list[T], remaining: list[T], cursor: Callable[[T], str]) -> str | None:
    if not selected or not remaining:
        return None
    return cursor(selected[-1])


async def run_dry_run(
    args: argparse.Namespace,
    *,
    settings: PlatformSettings,
    candidates: list[LegacyMediaCandidate],
) -> tuple[dict[str, Any], int]:
    processor = production_image_processor(settings)
    resumed = after_resume(
        candidates,
        resume_from=args.resume_from,
        cursor=lambda candidate: candidate.cursor,
    )
    actionable = [candidate for candidate in resumed if candidate.source_kind == "local_upload"]
    selected = actionable[: args.limit]
    remaining = actionable[args.limit :]
    analyses = [
        analyze_candidate(candidate, settings=settings, processor=processor)
        for candidate in selected
    ]
    hashes = Counter(
        analysis.source_sha256
        for analysis in analyses
        if analysis.ok and analysis.source_sha256
    )
    duplicate_files = sum(count - 1 for count in hashes.values() if count > 1)
    valid = [analysis for analysis in analyses if analysis.ok]
    invalid = [analysis for analysis in analyses if not analysis.ok]
    source_bytes = sum(analysis.source_bytes for analysis in valid)
    output_bytes = sum(analysis.projected_output_bytes for analysis in valid)
    conflicts = [
        {
            "cursor": candidate.cursor,
            "code": candidate.conflict_code or "manual_conflict",
        }
        for candidate in resumed
        if candidate.source_kind == "manual_conflict"
    ][:50]
    report = {
        "ok": not invalid and not conflicts,
        "mode": "dry-run",
        "mutated": False,
        "inventory": inventory_summary(candidates),
        "selection": {
            "limit": args.limit,
            "resume_from": args.resume_from,
            "selected_local_uploads": len(selected),
            "remaining_local_uploads": len(remaining),
            "next_resume_from": next_resume_cursor(
                selected,
                remaining,
                lambda candidate: candidate.cursor,
            ),
        },
        "estimate": {
            "valid_sources": len(valid),
            "invalid_sources": len(invalid),
            "source_bytes": source_bytes,
            "projected_variants": sum(item.projected_variants for item in valid),
            "projected_storage_bytes": output_bytes,
            "compression_ratio": round(output_bytes / source_bytes, 4)
            if source_bytes
            else None,
            "projected_class_a_puts": sum(item.projected_variants for item in valid),
            "projected_class_b": 0,
            "sha256_duplicate_files": duplicate_files,
        },
        "invalid": [
            {
                "cursor": analysis.candidate.cursor,
                "source_key": analysis.candidate.source_key,
                "code": analysis.error_code,
            }
            for analysis in invalid
        ],
        "manual_conflicts": conflicts,
        "checkpoint_written": False,
    }
    return report, 0 if report["ok"] else 2


class CheckpointStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().absolute()
        try:
            self.path.relative_to(REPOSITORY_ROOT.resolve())
        except ValueError:
            pass
        else:
            raise MigrationError(
                "checkpoint_inside_repository",
                "Migration checkpoint must be outside the Git repository.",
            )
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._lock_handle = None

    def __enter__(self) -> "CheckpointStore":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        resolved_parent = self.path.parent.resolve(strict=True)
        try:
            resolved_parent.relative_to(REPOSITORY_ROOT.resolve())
        except ValueError:
            pass
        else:
            raise MigrationError(
                "checkpoint_inside_repository",
                "Migration checkpoint resolved inside the Git repository.",
            )
        self._lock_handle = self.lock_path.open("a+", encoding="utf-8")
        os.chmod(self.lock_path, 0o600)
        try:
            fcntl.flock(
                self._lock_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            self._lock_handle.close()
            self._lock_handle = None
            raise MigrationError(
                "migration_already_running",
                "Another media migration process holds the checkpoint lock.",
            ) from exc
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self._lock_handle is not None:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": CHECKPOINT_VERSION,
                "created_at": utc_iso(),
                "updated_at": utc_iso(),
                "records": {},
                "runs": [],
            }
        metadata = self.path.lstat()
        if self.path.is_symlink() or not self.path.is_file():
            raise MigrationError(
                "unsafe_checkpoint",
                "Checkpoint must be a regular file, not a symlink.",
            )
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise MigrationError(
                "unsafe_checkpoint_permissions",
                "Checkpoint must be owned by the current user with mode 0600.",
            )
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationError(
                "invalid_checkpoint",
                "Checkpoint could not be read as valid JSON.",
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != CHECKPOINT_VERSION
            or not isinstance(payload.get("records"), dict)
            or not isinstance(payload.get("runs"), list)
        ):
            raise MigrationError(
                "invalid_checkpoint",
                f"Checkpoint must use format version {CHECKPOINT_VERSION}.",
            )
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        payload["version"] = CHECKPOINT_VERSION
        payload["updated_at"] = utc_iso()
        serialized = json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        ) + "\n"
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                os.chmod(temporary.name, 0o600)
                temporary.write(serialized)
                temporary.flush()
                os.fsync(temporary.fileno())
            assert temporary_path is not None
            os.replace(temporary_path, self.path)
            temporary_path = None
            directory_fd = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def record_run(
    checkpoint: dict[str, Any],
    *,
    mode: str,
    report: dict[str, Any],
) -> None:
    runs = checkpoint.setdefault("runs", [])
    runs.append(
        {
            "mode": mode,
            "completed_at": utc_iso(),
            "ok": bool(report.get("ok")),
            "processed": int(report.get("processed") or 0),
            "failed": int(report.get("failed") or 0),
            "last_cursor": report.get("last_cursor"),
        }
    )
    del runs[:-50]


def iter_original_chunks(path: Path) -> Iterator[bytes]:
    descriptor = open_regular_file(path)
    with os.fdopen(descriptor, "rb") as source:
        yield from iter(lambda: source.read(FILE_CHUNK_BYTES), b"")


async def get_target_state(
    db_session: AsyncSession,
    candidate: LegacyMediaCandidate,
) -> TargetState | None:
    if candidate.purpose in {"profile_avatar", "profile_banner"}:
        profile = await db_session.scalar(
            select(PlayerProfile).where(PlayerProfile.user_id == candidate.owner_id)
        )
        if profile is None:
            return None
        if candidate.purpose == "profile_avatar":
            return TargetState(profile.avatar_asset_id, profile.avatar_url)
        return TargetState(profile.banner_asset_id, profile.banner_url)
    tournament = await db_session.scalar(
        select(Tournament).where(Tournament.id == candidate.owner_id)
    )
    if tournament is None:
        return None
    return TargetState(tournament.banner_asset_id, tournament.cover_url)


def asset_owner_matches(asset: MediaAsset, candidate: LegacyMediaCandidate) -> bool:
    if asset.purpose != candidate.purpose:
        return False
    if candidate.purpose in {"profile_avatar", "profile_banner"}:
        return asset.owner_user_id == candidate.owner_id and asset.tournament_id is None
    return asset.tournament_id == candidate.owner_id and asset.owner_user_id is None


async def existing_inflight_asset(
    db_session: AsyncSession,
    candidate: LegacyMediaCandidate,
) -> MediaAsset | None:
    owner_filter = (
        MediaAsset.owner_user_id == candidate.owner_id
        if candidate.purpose in {"profile_avatar", "profile_banner"}
        else MediaAsset.tournament_id == candidate.owner_id
    )
    assets = tuple(
        await db_session.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.purpose == candidate.purpose,
                owner_filter,
                MediaAsset.status.in_(("pending", "processing")),
            )
            .order_by(MediaAsset.updated_at.desc(), MediaAsset.id.desc())
            .limit(2)
        )
    )
    if len(assets) > 1:
        raise MigrationError(
            "multiple_inflight_assets",
            "Multiple in-flight assets require manual reconciliation.",
        )
    return assets[0] if assets else None


@asynccontextmanager
async def media_processing_lock() -> AsyncIterator[None]:
    client = redis_client()
    token = secrets.token_urlsafe(24)
    acquired = False
    try:
        acquired = bool(
            await client.set(
                MEDIA_PROCESSING_LOCK_KEY,
                token,
                nx=True,
                ex=180,
            )
        )
        if not acquired:
            raise MigrationError(
                "media_processing_busy",
                "The media worker is processing another asset; retry this bounded batch.",
            )
        yield
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError(
            "media_processing_lock_unavailable",
            "Redis media-processing lock is unavailable.",
        ) from exc
    finally:
        if acquired:
            try:
                await client.eval(
                    LOCK_RELEASE_SCRIPT,
                    1,
                    MEDIA_PROCESSING_LOCK_KEY,
                    token,
                )
            except Exception:
                pass
        await client.aclose()


@asynccontextmanager
async def no_processing_lock() -> AsyncIterator[None]:
    yield


def checkpoint_record_for_analysis(
    analysis: CandidateAnalysis,
    *,
    asset_id: str,
    accepted_at: str,
) -> dict[str, Any]:
    candidate = analysis.candidate
    return {
        "identity": candidate.identity,
        "cursor": candidate.cursor,
        "purpose": candidate.purpose,
        "owner_id": candidate.owner_id,
        "subject_type": candidate.subject_type,
        "source_kind": "local_upload",
        "legacy_url": candidate.legacy_url,
        "source_key": candidate.source_key,
        "source_bytes": analysis.source_bytes,
        "source_sha256": analysis.source_sha256,
        "source_mime": analysis.source_mime,
        "asset_id": asset_id,
        "accepted_at": accepted_at,
        "apply_status": "pending",
        "original_retained": True,
    }


async def apply_candidate(
    analysis: CandidateAnalysis,
    *,
    checkpoint: dict[str, Any],
    checkpoint_store: CheckpointStore,
    service_builder: ServiceBuilder = build_media_service,
    lock_factory: Callable[[], Any] = media_processing_lock,
) -> dict[str, Any]:
    candidate = analysis.candidate
    if not analysis.ok or analysis.source_path is None or analysis.source_sha256 is None:
        return {
            "cursor": candidate.cursor,
            "ok": False,
            "code": analysis.error_code or "invalid_source",
        }
    records = checkpoint.setdefault("records", {})
    record = records.get(candidate.identity)

    async with session_factory()() as db_session:
        state = await get_target_state(db_session, candidate)
        if state is None:
            return {"cursor": candidate.cursor, "ok": False, "code": "owner_missing"}
        if state.active_asset_id and not (
            record and record.get("asset_id") == state.active_asset_id
        ):
            return {
                "cursor": candidate.cursor,
                "ok": False,
                "code": "active_asset_conflict",
            }
        if state.legacy_url not in {candidate.legacy_url, None}:
            return {
                "cursor": candidate.cursor,
                "ok": False,
                "code": "legacy_reference_changed",
            }

        service = service_builder(db_session)
        async with lock_factory():
            asset: MediaAsset | None = None
            if record is not None:
                if (
                    record.get("source_sha256") != analysis.source_sha256
                    or record.get("source_key") != candidate.source_key
                ):
                    return {
                        "cursor": candidate.cursor,
                        "ok": False,
                        "code": "checkpoint_source_mismatch",
                    }
                asset = await db_session.get(MediaAsset, str(record.get("asset_id")))
                if asset is None or not asset_owner_matches(asset, candidate):
                    return {
                        "cursor": candidate.cursor,
                        "ok": False,
                        "code": "checkpoint_asset_mismatch",
                    }
            else:
                asset = await existing_inflight_asset(db_session, candidate)
                if asset is not None and asset.source_sha256 != analysis.source_sha256:
                    return {
                        "cursor": candidate.cursor,
                        "ok": False,
                        "code": "inflight_source_mismatch",
                    }

            if asset is None:

                async def audit_acceptance(staged, superseded_asset_ids) -> None:
                    if (
                        staged.sha256 != analysis.source_sha256
                        or staged.byte_size != analysis.source_bytes
                        or staged.mime_type != analysis.source_mime
                    ):
                        raise MigrationError(
                            "source_changed_during_apply",
                            "Legacy source changed between analysis and durable acceptance.",
                        )
                    await write_audit_log(
                        db_session,
                        actor_user_id=None,
                        action="media.migration.accepted",
                        subject_type=candidate.subject_type,
                        subject_id=candidate.owner_id,
                        payload={
                            "asset_id": staged.asset_id,
                            "purpose": candidate.purpose,
                            "source_key": candidate.source_key,
                            "source_bytes": staged.byte_size,
                            "superseded_asset_ids": list(superseded_asset_ids),
                        },
                    )

                accepted = await service.accept_upload(
                    chunks=iter_original_chunks(analysis.source_path),
                    declared_mime=analysis.source_mime,
                    purpose=candidate.purpose,
                    owner_user_id=(
                        candidate.owner_id
                        if candidate.purpose in {"profile_avatar", "profile_banner"}
                        else None
                    ),
                    tournament_id=(
                        candidate.owner_id
                        if candidate.purpose == "tournament_banner"
                        else None
                    ),
                    before_commit=audit_acceptance,
                )
                asset_id = accepted.asset_id
                record = checkpoint_record_for_analysis(
                    analysis,
                    asset_id=asset_id,
                    accepted_at=utc_iso(),
                )
                records[candidate.identity] = record
                checkpoint_store.save(checkpoint)
            else:
                asset_id = asset.id
                if record is None:
                    record = checkpoint_record_for_analysis(
                        analysis,
                        asset_id=asset_id,
                        accepted_at=utc_iso(asset.created_at),
                    )
                    records[candidate.identity] = record
                    checkpoint_store.save(checkpoint)

            assert record is not None
            current_asset = await db_session.get(MediaAsset, asset_id)
            if current_asset is None:
                return {
                    "cursor": candidate.cursor,
                    "ok": False,
                    "code": "asset_missing_after_acceptance",
                }
            if current_asset.status == "ready":
                process_status = "ready"
                variant_count = len(VARIANT_SPECS[candidate.purpose])
                process_error = None
            elif current_asset.status in {"pending", "processing"}:
                process_result = await service.process_asset(asset_id)
                process_status = process_result.status
                variant_count = process_result.variants
                process_error = process_result.error_code
            else:
                process_status = current_asset.status
                variant_count = 0
                process_error = current_asset.error_code

        await db_session.rollback()
        final_state = await get_target_state(db_session, candidate)

    retained_bytes, retained_sha256 = hash_bounded_file(
        analysis.source_path,
        max_bytes=analysis.source_bytes,
    )
    original_retained = (
        retained_bytes == analysis.source_bytes
        and retained_sha256 == analysis.source_sha256
    )
    ready = bool(
        process_status == "ready"
        and final_state is not None
        and final_state.active_asset_id == asset_id
        and final_state.legacy_url is None
        and original_retained
    )
    record.update(
        {
            "apply_status": process_status,
            "apply_error_code": process_error,
            "projected_variants": analysis.projected_variants,
            "projected_output_bytes": analysis.projected_output_bytes,
            "actual_variants": variant_count,
            "original_retained": original_retained,
        }
    )
    if ready:
        record["applied_at"] = record.get("applied_at") or utc_iso()
        record.pop("apply_error_code", None)
    checkpoint_store.save(checkpoint)
    return {
        "cursor": candidate.cursor,
        "asset_id": asset_id,
        "ok": ready,
        "status": process_status,
        "code": None if ready else process_error or "asset_not_ready",
        "variants": variant_count,
        "original_retained": original_retained,
    }


async def run_apply(
    args: argparse.Namespace,
    *,
    settings: PlatformSettings,
    candidates: list[LegacyMediaCandidate],
    checkpoint_store: CheckpointStore,
    checkpoint: dict[str, Any],
    service_builder: ServiceBuilder = build_media_service,
    lock_factory: Callable[[], Any] = media_processing_lock,
) -> tuple[dict[str, Any], int]:
    resumed = after_resume(
        candidates,
        resume_from=args.resume_from,
        cursor=lambda candidate: candidate.cursor,
    )
    actionable = [candidate for candidate in resumed if candidate.source_kind == "local_upload"]
    selected = actionable[: args.limit]
    remaining = actionable[args.limit :]
    processor = production_image_processor(settings)
    results: list[dict[str, Any]] = []
    for candidate in selected:
        analysis = analyze_candidate(candidate, settings=settings, processor=processor)
        try:
            result = await apply_candidate(
                analysis,
                checkpoint=checkpoint,
                checkpoint_store=checkpoint_store,
                service_builder=service_builder,
                lock_factory=lock_factory,
            )
        except (MigrationError, MediaError) as exc:
            result = {
                "cursor": candidate.cursor,
                "ok": False,
                "code": getattr(exc, "code", "apply_failed"),
            }
        except Exception:
            result = {
                "cursor": candidate.cursor,
                "ok": False,
                "code": "apply_failed",
            }
        results.append(result)
    failed = sum(not result["ok"] for result in results)
    variants = sum(int(result.get("variants") or 0) for result in results if result["ok"])
    report = {
        "ok": failed == 0,
        "mode": "apply",
        "mutated": bool(selected),
        "processed": len(results),
        "succeeded": len(results) - failed,
        "failed": failed,
        "last_cursor": results[-1]["cursor"] if results else None,
        "next_resume_from": next_resume_cursor(
            selected,
            remaining,
            lambda candidate: candidate.cursor,
        ),
        "packaged_fallback_retained": sum(
            candidate.source_kind == "packaged_fallback" for candidate in candidates
        ),
        "operations": {
            "class_a_puts": variants,
            "class_b": 0,
            "local_original_deletes": 0,
        },
        "results": results,
        "checkpoint": str(checkpoint_store.path),
    }
    record_run(checkpoint, mode="apply", report=report)
    checkpoint_store.save(checkpoint)
    return report, 0 if report["ok"] else 2


def candidate_from_checkpoint(record: dict[str, Any]) -> LegacyMediaCandidate:
    required = (
        "identity",
        "cursor",
        "purpose",
        "owner_id",
        "subject_type",
        "legacy_url",
        "source_key",
        "asset_id",
    )
    if any(not isinstance(record.get(name), str) or not record.get(name) for name in required):
        raise MigrationError(
            "invalid_checkpoint_record",
            "Checkpoint media record is incomplete.",
        )
    rebuilt = build_candidate(
        purpose=str(record["purpose"]),
        owner_id=str(record["owner_id"]),
        subject_type=str(record["subject_type"]),
        legacy_url=str(record["legacy_url"]),
        active_asset_id=None,
    )
    if (
        rebuilt.identity != record["identity"]
        or rebuilt.cursor != record["cursor"]
        or rebuilt.source_kind != "local_upload"
        or rebuilt.source_key != record["source_key"]
        or record.get("source_kind") != "local_upload"
    ):
        raise MigrationError(
            "invalid_checkpoint_record",
            "Checkpoint media identity/source validation failed.",
        )
    return rebuilt


def descriptor_fingerprint(descriptor: AssetDescriptor) -> str:
    payload = {
        "asset_id": descriptor.asset_id,
        "purpose": descriptor.purpose,
        "status": descriptor.status,
        "variants": [
            {
                "name": variant.variant_name,
                "key": variant.object_key,
                "mime": variant.mime_type,
                "width": variant.width,
                "height": variant.height,
                "bytes": variant.byte_size,
                "sha256": variant.sha256,
            }
            for variant in descriptor.variants
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_descriptor(
    descriptor: AssetDescriptor | None,
    *,
    candidate: LegacyMediaCandidate,
    asset_id: str,
) -> AssetDescriptor:
    if descriptor is None:
        raise MigrationError("asset_missing", "Checkpointed media asset is missing from DB.")
    if (
        descriptor.asset_id != asset_id
        or descriptor.purpose != candidate.purpose
        or descriptor.status != "ready"
        or descriptor.error_code is not None
    ):
        raise MigrationError(
            "asset_not_ready",
            "Checkpointed media asset is not a clean ready asset.",
        )
    expected = {
        spec.name: (spec.width, spec.height) for spec in VARIANT_SPECS[candidate.purpose]
    }
    actual_names = {variant.variant_name for variant in descriptor.variants}
    if actual_names != set(expected) or len(descriptor.variants) != len(expected):
        raise MigrationError(
            "variant_set_mismatch",
            "DB media variants do not match the production variant set.",
        )
    for variant in descriptor.variants:
        if (
            (variant.width, variant.height) != expected[variant.variant_name]
            or variant.mime_type != "image/webp"
            or variant.byte_size <= 0
            or len(variant.sha256) != 64
        ):
            raise MigrationError(
                "variant_metadata_mismatch",
                "DB media variant metadata is invalid.",
            )
    return descriptor


def validate_head_metadata(
    variant,
    stored: StoredObjectMetadata | None,
) -> None:
    if stored is None:
        raise MigrationError("r2_object_missing", "Expected immutable R2 variant is missing.")
    if (
        stored.object_key != variant.object_key
        or stored.content_type != "image/webp"
        or stored.byte_size != variant.byte_size
        or stored.sha256 != variant.sha256
        or stored.cache_control != IMMUTABLE_CACHE_CONTROL
    ):
        raise MigrationError(
            "r2_metadata_mismatch",
            "R2 HeadObject metadata does not match PostgreSQL.",
        )


def public_variant_url(settings: PlatformSettings, object_key: str) -> str:
    base = str(settings.platform_media_public_base_url or "").rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise MigrationError(
            "invalid_cdn_base",
            "PLATFORM_MEDIA_PUBLIC_BASE_URL must be a credential-free HTTPS origin.",
        )
    return f"{base}/{urllib.parse.quote(object_key, safe='/')}"


def verify_cdn_variant(
    settings: PlatformSettings,
    variant,
    *,
    timeout: float,
) -> str:
    url = public_variant_url(settings, variant.object_key)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "OldSparky-Media-Migration/1",
            "Accept": "image/webp",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        content = response.read(variant.byte_size + 1)
        headers = response.headers
        if (
            response.status != 200
            or len(content) != variant.byte_size
            or headers.get_content_type().lower() != "image/webp"
            or hashlib.sha256(content).hexdigest() != variant.sha256
            or headers.get("Set-Cookie") is not None
            or headers.get("CF-Ray") is None
        ):
            raise MigrationError(
                "cdn_variant_mismatch",
                "Direct CDN response does not match the verified DB/R2 variant.",
            )
        cache_control = headers.get("Cache-Control") or ""
        directives = {
            directive.strip().lower()
            for directive in cache_control.split(",")
            if directive.strip()
        }
        if "public" not in directives or "immutable" not in directives:
            raise MigrationError(
                "cdn_cache_policy_mismatch",
                "Direct CDN response is missing immutable public caching.",
            )
        return (headers.get("CF-Cache-Status") or "UNKNOWN").upper()


async def verify_checkpoint_record(
    record: dict[str, Any],
    *,
    settings: PlatformSettings,
    storage: MediaStorage,
    verify_cdn: bool,
    cdn_timeout: float,
) -> dict[str, Any]:
    candidate = candidate_from_checkpoint(record)
    asset_id = str(record["asset_id"])
    async with session_factory()() as db_session:
        asset = await db_session.get(MediaAsset, asset_id)
        if asset is None or not asset_owner_matches(asset, candidate):
            raise MigrationError(
                "asset_ownership_mismatch",
                "Checkpointed asset ownership does not match its migration target.",
            )
        if (
            asset.source_sha256 != record.get("source_sha256")
            or asset.source_bytes != record.get("source_bytes")
            or asset.source_mime != record.get("source_mime")
        ):
            raise MigrationError(
                "asset_source_mismatch",
                "Checkpoint source evidence does not match PostgreSQL.",
            )
        state = await get_target_state(db_session, candidate)
        if (
            state is None
            or state.active_asset_id != asset_id
            or state.legacy_url is not None
        ):
            raise MigrationError(
                "active_link_mismatch",
                "Target is not transactionally linked to the checkpointed ready asset.",
            )
        descriptor = validate_descriptor(
            await MediaRepository(db_session).descriptor(asset_id),
            candidate=candidate,
            asset_id=asset_id,
        )
        await db_session.rollback()

    head_count = 0
    cdn_count = 0
    cache_statuses: Counter[str] = Counter()
    for variant in descriptor.variants:
        stored = await asyncio.to_thread(storage.head, variant.object_key)
        head_count += 1
        validate_head_metadata(variant, stored)
        if verify_cdn:
            cache_status = await asyncio.to_thread(
                verify_cdn_variant,
                settings,
                variant,
                timeout=cdn_timeout,
            )
            cdn_count += 1
            cache_statuses[cache_status] += 1
    return {
        "cursor": candidate.cursor,
        "asset_id": asset_id,
        "ok": True,
        "descriptor_fingerprint": descriptor_fingerprint(descriptor),
        "variants": len(descriptor.variants),
        "head_objects": head_count,
        "cdn_gets": cdn_count,
        "cdn_cache_statuses": dict(sorted(cache_statuses.items())),
    }


def checkpoint_records_after_resume(
    checkpoint: dict[str, Any],
    *,
    resume_from: str | None,
) -> list[dict[str, Any]]:
    records = [
        record
        for record in checkpoint.get("records", {}).values()
        if isinstance(record, dict) and record.get("asset_id")
    ]
    records.sort(key=lambda record: str(record.get("cursor") or ""))
    return after_resume(
        records,
        resume_from=resume_from,
        cursor=lambda record: str(record.get("cursor") or ""),
    )


async def run_verify(
    args: argparse.Namespace,
    *,
    settings: PlatformSettings,
    checkpoint_store: CheckpointStore,
    checkpoint: dict[str, Any],
    storage: MediaStorage,
) -> tuple[dict[str, Any], int]:
    resumed = checkpoint_records_after_resume(
        checkpoint,
        resume_from=args.resume_from,
    )
    selected = resumed[: args.limit]
    remaining = resumed[args.limit :]
    results: list[dict[str, Any]] = []
    for record in selected:
        try:
            result = await verify_checkpoint_record(
                record,
                settings=settings,
                storage=storage,
                verify_cdn=args.verify_cdn,
                cdn_timeout=args.cdn_timeout,
            )
        except (MigrationError, MediaError) as exc:
            result = {
                "cursor": str(record.get("cursor") or ""),
                "asset_id": record.get("asset_id"),
                "ok": False,
                "code": getattr(exc, "code", "verify_failed"),
            }
        except Exception:
            result = {
                "cursor": str(record.get("cursor") or ""),
                "asset_id": record.get("asset_id"),
                "ok": False,
                "code": "verify_failed",
            }
        results.append(result)
        if result["ok"]:
            record["verified_at"] = utc_iso()
            record["verification"] = {
                "ok": True,
                "descriptor_fingerprint": result["descriptor_fingerprint"],
                "variants": result["variants"],
                "head_objects": result["head_objects"],
                "cdn_gets": result["cdn_gets"],
            }
            record.pop("verify_error_code", None)
        else:
            record.pop("verified_at", None)
            record["verification"] = {"ok": False}
            record["verify_error_code"] = result["code"]
        checkpoint_store.save(checkpoint)

    failed = sum(not result["ok"] for result in results)
    report = {
        "ok": bool(results) and failed == 0,
        "mode": "verify",
        "mutated": False,
        "processed": len(results),
        "succeeded": len(results) - failed,
        "failed": failed,
        "last_cursor": results[-1]["cursor"] if results else None,
        "next_resume_from": next_resume_cursor(
            selected,
            remaining,
            lambda record: str(record.get("cursor") or ""),
        ),
        "operations": {
            "class_a": 0,
            "class_b_head_objects": sum(
                int(result.get("head_objects") or 0) for result in results
            ),
            "cdn_gets": sum(int(result.get("cdn_gets") or 0) for result in results),
            "list_objects": 0,
        },
        "results": results,
        "checkpoint": str(checkpoint_store.path),
    }
    if not results:
        report["code"] = "no_checkpointed_assets"
    record_run(checkpoint, mode="verify", report=report)
    checkpoint_store.save(checkpoint)
    return report, 0 if report["ok"] else 2


def validate_cleanup_evidence(
    record: dict[str, Any],
    *,
    now: dt.datetime,
    grace_hours: float,
    verify_max_age_hours: float,
) -> None:
    if record.get("source_kind") != "local_upload":
        raise MigrationError(
            "cleanup_non_local_forbidden",
            "Cleanup is restricted to checkpointed legacy local uploads.",
        )
    if record.get("cleaned_at"):
        raise MigrationError("already_cleaned", "Legacy original was already cleaned.")
    if not record.get("original_retained"):
        raise MigrationError(
            "original_retention_unproven",
            "Checkpoint does not prove original retention after apply.",
        )
    applied_at_raw = record.get("applied_at")
    verified_at_raw = record.get("verified_at")
    verification = record.get("verification")
    if not isinstance(applied_at_raw, str):
        raise MigrationError(
            "apply_evidence_missing",
            "Cleanup requires a successful ready apply checkpoint.",
        )
    if (
        not isinstance(verified_at_raw, str)
        or not isinstance(verification, dict)
        or verification.get("ok") is not True
        or not isinstance(verification.get("descriptor_fingerprint"), str)
    ):
        raise MigrationError(
            "verify_evidence_missing",
            "Cleanup requires successful DB/R2 verification evidence.",
        )
    applied_at = parse_utc(applied_at_raw)
    verified_at = parse_utc(verified_at_raw)
    if verified_at < applied_at:
        raise MigrationError(
            "verify_evidence_stale",
            "Verification evidence predates successful apply.",
        )
    if (now - applied_at).total_seconds() < grace_hours * 3600:
        raise MigrationError(
            "cleanup_grace_not_elapsed",
            "Legacy original cleanup grace period has not elapsed.",
        )
    if (now - verified_at).total_seconds() > verify_max_age_hours * 3600:
        raise MigrationError(
            "verify_evidence_stale",
            "Verification evidence is too old for destructive cleanup.",
        )


def delete_exact_local_original(
    record: dict[str, Any],
    *,
    settings: PlatformSettings,
) -> None:
    candidate = candidate_from_checkpoint(record)
    path = resolve_local_original(candidate, upload_root=settings.platform_upload_dir)
    descriptor = open_regular_file(path)
    try:
        opened_metadata = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            source_bytes, source_sha256 = hash_bounded_stream(
                source,
                max_bytes=settings.platform_media_max_input_bytes,
            )
        current_metadata = path.lstat()
        if (
            not stat.S_ISREG(current_metadata.st_mode)
            or current_metadata.st_dev != opened_metadata.st_dev
            or current_metadata.st_ino != opened_metadata.st_ino
        ):
            raise MigrationError(
                "cleanup_source_changed",
                "Legacy original changed while cleanup evidence was collected.",
            )
    finally:
        os.close(descriptor)
    if (
        source_bytes != record.get("source_bytes")
        or source_sha256 != record.get("source_sha256")
    ):
        raise MigrationError(
            "cleanup_source_mismatch",
            "Legacy original changed after apply and will not be deleted.",
        )
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if path.exists():
        raise MigrationError(
            "cleanup_delete_failed",
            "Legacy original still exists after exact unlink.",
        )


async def run_cleanup(
    args: argparse.Namespace,
    *,
    settings: PlatformSettings,
    checkpoint_store: CheckpointStore,
    checkpoint: dict[str, Any],
    storage: MediaStorage,
    backup_checker: Callable[..., dict[str, Any]] = check_latest_backup,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], int]:
    current_time = now or utc_now()
    all_records = checkpoint_records_after_resume(
        checkpoint,
        resume_from=args.resume_from,
    )
    pending = [record for record in all_records if not record.get("cleaned_at")]
    selected = pending[: args.limit]
    remaining = pending[args.limit :]
    if not selected:
        report = {
            "ok": bool(all_records),
            "mode": "cleanup",
            "mutated": False,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "last_cursor": None,
            "next_resume_from": None,
            "code": "nothing_to_cleanup" if all_records else "no_checkpointed_assets",
            "operations": {
                "class_a": 0,
                "class_b_head_objects": 0,
                "local_original_deletes": 0,
                "r2_deletes": 0,
                "list_objects": 0,
            },
            "checkpoint": str(checkpoint_store.path),
        }
        record_run(checkpoint, mode="cleanup", report=report)
        checkpoint_store.save(checkpoint)
        return report, 0 if report["ok"] else 2

    try:
        backup = backup_checker(
            args.backup_dir,
            max_age_hours=args.backup_max_age_hours,
        )
    except Exception as exc:
        raise MigrationError(
            "fresh_restore_verified_backup_required",
            "Cleanup requires a fresh restore-verified backup with a matching checksum.",
        ) from exc
    if backup.get("restore_verified") is not True:
        raise MigrationError(
            "fresh_restore_verified_backup_required",
            "Cleanup backup evidence is not restore-verified.",
        )
    if (
        int(backup.get("format_version") or 1) < 2
        or backup.get("alembic_revision_verified") is not True
    ):
        raise MigrationError(
            "fresh_restore_verified_backup_required",
            "Cleanup requires backup format 2 with verified Alembic state.",
        )

    results: list[dict[str, Any]] = []
    for record in selected:
        cursor = str(record.get("cursor") or "")
        try:
            validate_cleanup_evidence(
                record,
                now=current_time,
                grace_hours=args.cleanup_grace_hours,
                verify_max_age_hours=args.verify_max_age_hours,
            )
            current_verification = await verify_checkpoint_record(
                record,
                settings=settings,
                storage=storage,
                verify_cdn=args.verify_cdn,
                cdn_timeout=args.cdn_timeout,
            )
            if (
                current_verification["descriptor_fingerprint"]
                != record["verification"]["descriptor_fingerprint"]
            ):
                raise MigrationError(
                    "verification_fingerprint_changed",
                    "Current DB/R2 verification differs from checkpoint evidence.",
                )
            delete_exact_local_original(record, settings=settings)
            record["cleaned_at"] = utc_iso(current_time)
            record["cleanup_backup"] = {
                "metadata_file": Path(str(backup.get("metadata_file") or "")).name,
                "age_hours": backup.get("age_hours"),
                "restore_verified": True,
            }
            record.pop("cleanup_error_code", None)
            result = {
                "cursor": cursor,
                "asset_id": record.get("asset_id"),
                "ok": True,
                "head_objects": current_verification["head_objects"],
                "cdn_gets": current_verification["cdn_gets"],
                "local_original_deleted": True,
            }
        except (MigrationError, MediaError) as exc:
            result = {
                "cursor": cursor,
                "asset_id": record.get("asset_id"),
                "ok": False,
                "code": getattr(exc, "code", "cleanup_failed"),
                "local_original_deleted": False,
            }
            record["cleanup_error_code"] = result["code"]
        except Exception:
            result = {
                "cursor": cursor,
                "asset_id": record.get("asset_id"),
                "ok": False,
                "code": "cleanup_failed",
                "local_original_deleted": False,
            }
            record["cleanup_error_code"] = result["code"]
        results.append(result)
        checkpoint_store.save(checkpoint)

    failed = sum(not result["ok"] for result in results)
    deleted = sum(bool(result["local_original_deleted"]) for result in results)
    report = {
        "ok": failed == 0,
        "mode": "cleanup",
        "mutated": deleted > 0,
        "processed": len(results),
        "succeeded": len(results) - failed,
        "failed": failed,
        "last_cursor": results[-1]["cursor"] if results else None,
        "next_resume_from": next_resume_cursor(
            selected,
            remaining,
            lambda record: str(record.get("cursor") or ""),
        ),
        "backup": {
            "format_version": 2,
            "restore_verified": True,
            "alembic_revision_verified": True,
            "age_hours": backup.get("age_hours"),
            "metadata_file": Path(str(backup.get("metadata_file") or "")).name,
        },
        "operations": {
            "class_a": 0,
            "class_b_head_objects": sum(
                int(result.get("head_objects") or 0) for result in results
            ),
            "cdn_gets": sum(int(result.get("cdn_gets") or 0) for result in results),
            "local_original_deletes": deleted,
            "r2_deletes": 0,
            "list_objects": 0,
        },
        "results": results,
        "checkpoint": str(checkpoint_store.path),
    }
    record_run(checkpoint, mode="cleanup", report=report)
    checkpoint_store.save(checkpoint)
    return report, 0 if report["ok"] else 2


def validate_database_boundary(settings: PlatformSettings) -> None:
    database_url = settings.platform_database_url
    normalized = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    parsed = urllib.parse.urlsplit(normalized)
    database = urllib.parse.unquote(parsed.path.lstrip("/"))
    if parsed.scheme not in {"postgresql", "postgres"} or database != "platformdb":
        raise MigrationError(
            "wrong_database",
            "Media migration is restricted to the isolated platformdb database.",
        )
    if settings.platform_db_schema != PLATFORM_SCHEMA:
        raise MigrationError(
            "wrong_schema",
            "Media migration is restricted to the platform schema.",
        )


def validate_r2_mutation_boundary(settings: PlatformSettings) -> None:
    if settings.platform_object_storage_backend.strip().lower() != "r2":
        raise MigrationError(
            "r2_required",
            "Apply, verify, and cleanup require PLATFORM_OBJECT_STORAGE_BACKEND=r2.",
        )
    try:
        R2Storage.from_settings(settings)
    except Exception as exc:
        raise MigrationError(
            "r2_configuration_invalid",
            "R2 configuration is incomplete or invalid.",
        ) from exc


def print_report(report: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, sort_keys=True))
        return
    mode = report.get("mode")
    ok = report.get("ok")
    processed = report.get("processed")
    if processed is None:
        processed = report.get("selection", {}).get("selected_local_uploads", 0)
    print(
        f"Media migration: mode={mode}; ok={str(bool(ok)).lower()}; "
        f"processed={processed}; mutated={str(bool(report.get('mutated'))).lower()}."
    )
    operations = report.get("operations") or report.get("estimate")
    if operations:
        print(f"Operations/estimate: {json.dumps(operations, sort_keys=True)}")
    next_cursor = report.get("next_resume_from") or report.get("selection", {}).get(
        "next_resume_from"
    )
    if next_cursor:
        print(f"Resume with: --resume-from {next_cursor}")
    if report.get("checkpoint"):
        print(f"Checkpoint: {report['checkpoint']}")


async def async_main(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    configured_env_file = args.env_file or (
        Path(os.environ["PLATFORM_ENV_FILE"])
        if os.environ.get("PLATFORM_ENV_FILE")
        else None
    )
    if configured_env_file is not None:
        load_env_file(configured_env_file)
    settings = get_settings()
    try:
        validate_platform_settings(settings)
    except Exception as exc:
        raise MigrationError(
            "platform_configuration_invalid",
            "Platform production configuration validation failed.",
        ) from exc
    validate_database_boundary(settings)

    if args.dry_run:
        async with session_factory()() as db_session:
            candidates = await load_inventory(db_session)
        return await run_dry_run(args, settings=settings, candidates=candidates)

    validate_r2_mutation_boundary(settings)
    checkpoint_store = CheckpointStore(args.checkpoint)
    with checkpoint_store:
        checkpoint = checkpoint_store.load()
        if args.apply:
            async with session_factory()() as db_session:
                candidates = await load_inventory(db_session)
            return await run_apply(
                args,
                settings=settings,
                candidates=candidates,
                checkpoint_store=checkpoint_store,
                checkpoint=checkpoint,
            )
        storage = R2Storage.from_settings(settings)
        if args.verify:
            return await run_verify(
                args,
                settings=settings,
                checkpoint_store=checkpoint_store,
                checkpoint=checkpoint,
                storage=storage,
            )
        return await run_cleanup(
            args,
            settings=settings,
            checkpoint_store=checkpoint_store,
            checkpoint=checkpoint,
            storage=storage,
        )


async def run_with_cleanup(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    try:
        return await async_main(args)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        report, exit_code = asyncio.run(run_with_cleanup(args))
        print_report(report, as_json=args.as_json)
        return exit_code
    except MigrationError as exc:
        print(f"Media migration blocked [{exc.code}]: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Media migration interrupted; originals were not cleanup-eligible.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            f"Media migration failed safely: {type(exc).__name__}; "
            "no credentials were printed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
