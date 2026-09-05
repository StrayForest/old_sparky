from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete, select, update

from python_packages.platform_infra.config import PlatformSettings, get_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.image_processor import ImageProcessor
from python_packages.platform_infra.media.r2_storage import (
    IMMUTABLE_CACHE_CONTROL,
    StoredObjectMetadata,
)
from python_packages.platform_infra.media.service import (
    MediaService,
    MediaServicePolicy,
)
from python_packages.platform_infra.media.source_store import MediaSourceStore
from python_packages.platform_infra.models import (
    AuditLog,
    MediaAsset,
    PlayerProfile,
    User,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase
from tools import platform_migrate_media as migration

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeMediaStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.head_calls: list[str] = []
        self.delete_calls: list[str] = []

    def put(
        self,
        object_key: str,
        content: bytes,
        *,
        content_type: str,
        content_sha256: str,
    ) -> None:
        if content_type != "image/webp" or sha256(content).hexdigest() != content_sha256:
            raise AssertionError("invalid immutable media put")
        self.objects[object_key] = (content, content_sha256)

    def head(self, object_key: str) -> StoredObjectMetadata | None:
        self.head_calls.append(object_key)
        stored = self.objects.get(object_key)
        if stored is None:
            return None
        content, digest = stored
        return StoredObjectMetadata(
            object_key=object_key,
            content_type="image/webp",
            byte_size=len(content),
            sha256=digest,
            cache_control=IMMUTABLE_CACHE_CONTROL,
        )

    def delete(self, object_key: str) -> None:
        self.delete_calls.append(object_key)
        self.objects.pop(object_key, None)

    def delete_many(self, object_keys: list[str] | tuple[str, ...]) -> None:
        for object_key in object_keys:
            self.delete(object_key)


def migration_settings(upload_root: Path) -> PlatformSettings:
    return get_settings().model_copy(
        update={
            "platform_upload_dir": upload_root,
            "platform_media_staging_dir": upload_root.parent / "private-staging",
            "platform_media_public_base_url": "https://cdn.example.test",
            "platform_media_max_input_bytes": 5 * 1024 * 1024,
        }
    )


class PlatformMediaMigrationUnitTests(PlatformIsolatedAsyncioTestCase):
    def test_env_file_must_be_private_and_loads_without_printing_values(self) -> None:
        with TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env.platform"
            env_file.write_text("PLATFORM_TEST_MIGRATION_VALUE=loaded\n", encoding="utf-8")
            env_file.chmod(0o600)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PLATFORM_SHARED_DIR", None)
                migration.load_env_file(env_file)
                self.assertEqual(os.environ["PLATFORM_TEST_MIGRATION_VALUE"], "loaded")
                self.assertEqual(os.environ["PLATFORM_SHARED_DIR"], directory)
            env_file.chmod(0o604)
            with self.assertRaises(migration.MigrationError) as unsafe:
                migration.load_env_file(env_file)
            self.assertEqual(unsafe.exception.code, "unsafe_env_file_permissions")

    def test_classifies_only_exact_local_uploads_and_packaged_fallbacks(self) -> None:
        owner_id = str(uuid4())
        local = migration.build_candidate(
            purpose="profile_avatar",
            owner_id=owner_id,
            subject_type="profile",
            legacy_url="/api/v1/uploads/avatars/avatar.png",
            active_asset_id=None,
        )
        packaged = migration.build_candidate(
            purpose="tournament_banner",
            owner_id=str(uuid4()),
            subject_type="tournament",
            legacy_url="/assets/tournament-covers/template.webp",
            active_asset_id=None,
        )
        traversal = migration.build_candidate(
            purpose="profile_avatar",
            owner_id=str(uuid4()),
            subject_type="profile",
            legacy_url="/api/v1/uploads/avatars/../secret.png",
            active_asset_id=None,
        )
        remote = migration.build_candidate(
            purpose="profile_avatar",
            owner_id=str(uuid4()),
            subject_type="profile",
            legacy_url="https://attacker.invalid/avatar.png",
            active_asset_id=None,
        )
        active_conflict = migration.build_candidate(
            purpose="profile_avatar",
            owner_id=str(uuid4()),
            subject_type="profile",
            legacy_url="/api/v1/uploads/avatars/avatar.png",
            active_asset_id=str(uuid4()),
        )

        self.assertEqual(local.source_kind, "local_upload")
        self.assertEqual(local.source_key, "avatars/avatar.png")
        self.assertEqual(packaged.source_kind, "packaged_fallback")
        self.assertEqual(traversal.source_kind, "manual_conflict")
        self.assertEqual(remote.source_kind, "manual_conflict")
        self.assertEqual(
            active_conflict.conflict_code,
            "active_asset_and_legacy_reference",
        )

    async def test_dry_run_estimates_variants_duplicates_and_does_not_write_checkpoint(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            uploads = root / "uploads"
            (uploads / "avatars").mkdir(parents=True)
            (uploads / "avatars" / "one.png").write_bytes(TINY_PNG)
            (uploads / "avatars" / "two.png").write_bytes(TINY_PNG)
            settings = migration_settings(uploads)
            candidates = [
                migration.build_candidate(
                    purpose="profile_avatar",
                    owner_id=str(uuid4()),
                    subject_type="profile",
                    legacy_url="/api/v1/uploads/avatars/one.png",
                    active_asset_id=None,
                ),
                migration.build_candidate(
                    purpose="profile_avatar",
                    owner_id=str(uuid4()),
                    subject_type="profile",
                    legacy_url="/api/v1/uploads/avatars/two.png",
                    active_asset_id=None,
                ),
                migration.build_candidate(
                    purpose="tournament_banner",
                    owner_id=str(uuid4()),
                    subject_type="tournament",
                    legacy_url="/assets/tournament-covers/template.webp",
                    active_asset_id=None,
                ),
            ]
            checkpoint = root / "checkpoint.json"
            args = migration.parse_args(
                ["--dry-run", "--limit", "10", "--checkpoint", str(checkpoint)]
            )

            report, exit_code = await migration.run_dry_run(
                args,
                settings=settings,
                candidates=sorted(candidates, key=lambda item: item.cursor),
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(report["ok"])
            self.assertEqual(report["inventory"]["packaged_fallback_retained"], 1)
            self.assertEqual(report["estimate"]["valid_sources"], 2)
            self.assertEqual(report["estimate"]["projected_variants"], 6)
            self.assertEqual(report["estimate"]["projected_class_a_puts"], 6)
            self.assertEqual(report["estimate"]["sha256_duplicate_files"], 1)
            self.assertGreater(report["estimate"]["projected_storage_bytes"], 0)
            self.assertFalse(checkpoint.exists())

    def test_checkpoint_is_private_atomic_and_forbidden_inside_repository(self) -> None:
        with TemporaryDirectory() as directory:
            checkpoint_path = Path(directory) / "state" / "checkpoint.json"
            store = migration.CheckpointStore(checkpoint_path)
            with store:
                payload = store.load()
                payload["records"]["profile_avatar:test"] = {"asset_id": "test"}
                store.save(payload)
                loaded = store.load()
            self.assertEqual(
                loaded["records"]["profile_avatar:test"]["asset_id"],
                "test",
            )
            self.assertEqual(checkpoint_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(checkpoint_path.parent.stat().st_mode & 0o777, 0o700)

        with self.assertRaises(migration.MigrationError) as blocked:
            migration.CheckpointStore(
                migration.REPOSITORY_ROOT / "tmp-media-checkpoint.json"
            )
        self.assertEqual(blocked.exception.code, "checkpoint_inside_repository")

    def test_cleanup_cli_and_evidence_fail_closed(self) -> None:
        with self.assertRaises(migration.MigrationError) as confirmation:
            migration.parse_args(["--cleanup"])
        self.assertEqual(confirmation.exception.code, "cleanup_confirmation_required")

        now = datetime.now(UTC)
        record = {
            "source_kind": "local_upload",
            "original_retained": True,
            "applied_at": migration.utc_iso(now - timedelta(hours=48)),
            "verified_at": migration.utc_iso(now - timedelta(hours=1)),
            "verification": {"ok": True, "descriptor_fingerprint": "a" * 64},
        }
        migration.validate_cleanup_evidence(
            record,
            now=now,
            grace_hours=24,
            verify_max_age_hours=24,
        )
        record["applied_at"] = migration.utc_iso(now - timedelta(hours=1))
        with self.assertRaises(migration.MigrationError) as grace:
            migration.validate_cleanup_evidence(
                record,
                now=now,
                grace_hours=24,
                verify_max_age_hours=24,
            )
        self.assertEqual(grace.exception.code, "cleanup_grace_not_elapsed")


class PlatformMediaMigrationIntegrationTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.user_id = str(uuid4())
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.uploads = self.root / "uploads"
        self.source_path = self.uploads / "avatars" / "legacy.png"
        self.source_path.parent.mkdir(parents=True)
        self.source_path.write_bytes(TINY_PNG)
        self.settings = migration_settings(self.uploads)
        self.storage = FakeMediaStorage()
        self.source_store = MediaSourceStore(self.root / "private-staging")
        self.processor = migration.production_image_processor(self.settings)
        self.policy = MediaServicePolicy(
            max_attempts=3,
            processing_stale_seconds=60,
            retry_base_seconds=1,
            retry_max_seconds=4,
            cleanup_grace_seconds=60,
            reconciliation_batch_size=16,
            staging_orphan_grace_seconds=60,
        )
        async with session_factory()() as db_session:
            db_session.add(
                User(
                    id=self.user_id,
                    email=f"migration-{self.user_id}@example.com",
                    display_name="migration-test",
                )
            )
            await db_session.flush()
            db_session.add(
                PlayerProfile(
                    user_id=self.user_id,
                    display_name="migration-test",
                    avatar_url="/api/v1/uploads/avatars/legacy.png",
                )
            )
            await db_session.commit()

    async def asyncTearDown(self) -> None:
        async with session_factory()() as db_session:
            await db_session.execute(
                update(PlayerProfile)
                .where(PlayerProfile.user_id == self.user_id)
                .values(avatar_asset_id=None, banner_asset_id=None)
            )
            await db_session.execute(
                delete(AuditLog).where(AuditLog.subject_id == self.user_id)
            )
            await db_session.execute(
                delete(MediaAsset).where(MediaAsset.owner_user_id == self.user_id)
            )
            await db_session.execute(delete(User).where(User.id == self.user_id))
            await db_session.commit()
        await dispose_engine()
        self.temporary.cleanup()

    def service_builder(self, db_session) -> MediaService:
        return MediaService(
            db_session=db_session,
            source_store=self.source_store,
            processor=self.processor,
            storage=self.storage,
            policy=self.policy,
        )

    async def test_apply_verify_and_gated_cleanup_preserve_r2_variants(self) -> None:
        candidate = migration.build_candidate(
            purpose="profile_avatar",
            owner_id=self.user_id,
            subject_type="profile",
            legacy_url="/api/v1/uploads/avatars/legacy.png",
            active_asset_id=None,
        )
        analysis = migration.analyze_candidate(
            candidate,
            settings=self.settings,
            processor=ImageProcessor(self.processor.policy),
        )
        self.assertTrue(analysis.ok)
        checkpoint_path = self.root / "checkpoint" / "state.json"
        store = migration.CheckpointStore(checkpoint_path)
        with store:
            checkpoint = store.load()
            applied = await migration.apply_candidate(
                analysis,
                checkpoint=checkpoint,
                checkpoint_store=store,
                service_builder=self.service_builder,
                lock_factory=migration.no_processing_lock,
            )
            self.assertTrue(applied["ok"], applied)
            self.assertTrue(self.source_path.is_file())
            self.assertEqual(self.source_path.read_bytes(), TINY_PNG)
            self.assertEqual(len(self.storage.objects), 3)

            record = checkpoint["records"][candidate.identity]
            verified = await migration.verify_checkpoint_record(
                record,
                settings=self.settings,
                storage=self.storage,
                verify_cdn=False,
                cdn_timeout=1,
            )
            self.assertTrue(verified["ok"])
            self.assertEqual(verified["head_objects"], 3)
            now = datetime.now(UTC)
            record["applied_at"] = migration.utc_iso(now - timedelta(hours=48))
            record["verified_at"] = migration.utc_iso(now - timedelta(hours=1))
            record["verification"] = {
                "ok": True,
                "descriptor_fingerprint": verified["descriptor_fingerprint"],
                "variants": verified["variants"],
                "head_objects": verified["head_objects"],
                "cdn_gets": 0,
            }
            store.save(checkpoint)

            args = migration.parse_args(
                [
                    "--cleanup",
                    "--confirm-cleanup",
                    migration.CLEANUP_CONFIRMATION,
                    "--checkpoint",
                    str(checkpoint_path),
                    "--cleanup-grace-hours",
                    "24",
                    "--verify-max-age-hours",
                    "24",
                ]
            )
            with self.assertRaises(migration.MigrationError) as old_backup:
                await migration.run_cleanup(
                    args,
                    settings=self.settings,
                    checkpoint_store=store,
                    checkpoint=checkpoint,
                    storage=self.storage,
                    backup_checker=lambda _path, *, max_age_hours: {
                        "format_version": 1,
                        "restore_verified": True,
                        "metadata_file": "/safe/legacy-platformdb-test.json",
                        "age_hours": min(max_age_hours, 1),
                    },
                    now=now,
                )
            self.assertEqual(
                old_backup.exception.code,
                "fresh_restore_verified_backup_required",
            )
            report, exit_code = await migration.run_cleanup(
                args,
                settings=self.settings,
                checkpoint_store=store,
                checkpoint=checkpoint,
                storage=self.storage,
                backup_checker=lambda _path, *, max_age_hours: {
                    "format_version": 2,
                    "restore_verified": True,
                    "alembic_revision_verified": True,
                    "metadata_file": "/safe/platformdb-test.json",
                    "age_hours": min(max_age_hours, 1),
                },
                now=now,
            )

        self.assertEqual(exit_code, 0, report)
        self.assertTrue(report["ok"])
        self.assertFalse(self.source_path.exists())
        self.assertEqual(report["operations"]["local_original_deletes"], 1)
        self.assertEqual(report["operations"]["r2_deletes"], 0)
        self.assertEqual(self.storage.delete_calls, [])
        self.assertEqual(len(self.storage.objects), 3)

        async with session_factory()() as db_session:
            profile = await db_session.scalar(
                select(PlayerProfile).where(PlayerProfile.user_id == self.user_id)
            )
            self.assertIsNotNone(profile.avatar_asset_id)
            self.assertIsNone(profile.avatar_url)
            asset = await db_session.get(MediaAsset, profile.avatar_asset_id)
            self.assertEqual(asset.status, "ready")
            audit_action = await db_session.scalar(
                select(AuditLog.action).where(
                    AuditLog.subject_id == self.user_id,
                    AuditLog.action == "media.migration.accepted",
                )
            )
            self.assertEqual(audit_action, "media.migration.accepted")


if __name__ == "__main__":
    unittest.main()
