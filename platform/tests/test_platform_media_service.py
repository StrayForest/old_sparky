from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from sqlalchemy import delete, select, update

from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.errors import MediaStorageError
from python_packages.platform_infra.media.image_processor import (
    ProcessedVariant,
    VARIANT_SPECS,
    media_object_key,
)
from python_packages.platform_infra.media.r2_storage import StoredObjectMetadata
from python_packages.platform_infra.media.repository import MediaRepository
from python_packages.platform_infra.media.service import MediaService, MediaServicePolicy
from python_packages.platform_infra.media.source_store import MediaSourceStore
from python_packages.platform_infra.models import MediaAsset, MediaVariant, PlayerProfile, User


TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeProcessor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def process(
        self,
        source_path: Path,
        *,
        purpose: str,
        asset_id: str,
        owner_id: str,
    ) -> tuple[ProcessedVariant, ...]:
        self.calls.append(asset_id)
        self.assert_private_source(source_path)
        variants: list[ProcessedVariant] = []
        for spec in VARIANT_SPECS[purpose]:
            content = f"webp:{asset_id}:{spec.name}".encode()
            variants.append(
                ProcessedVariant(
                    variant_name=spec.name,
                    object_key=media_object_key(
                        purpose=purpose,
                        owner_id=owner_id,
                        asset_id=asset_id,
                        variant_name=spec.name,
                    ),
                    mime_type="image/webp",
                    width=spec.width,
                    height=spec.height,
                    content=content,
                    sha256=sha256(content).hexdigest(),
                )
            )
        return tuple(variants)

    @staticmethod
    def assert_private_source(source_path: Path) -> None:
        if not source_path.is_file() or not source_path.name.endswith(".source"):
            raise AssertionError("processor did not receive a private staged source")


class FakeMediaStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_attempts = 0
        self.fail_on_attempt: int | None = None
        self.deleted_batches: list[tuple[str, ...]] = []

    def put(
        self,
        object_key: str,
        content: bytes,
        *,
        content_type: str,
        content_sha256: str,
    ) -> None:
        self.put_attempts += 1
        if self.fail_on_attempt == self.put_attempts:
            raise MediaStorageError("put", retriable=True)
        if content_type != "image/webp" or sha256(content).hexdigest() != content_sha256:
            raise AssertionError("invalid prepared variant metadata")
        self.objects[object_key] = content

    def delete(self, object_key: str) -> None:
        self.objects.pop(object_key, None)

    def delete_many(self, object_keys: list[str] | tuple[str, ...]) -> None:
        keys = tuple(object_keys)
        self.deleted_batches.append(keys)
        for object_key in keys:
            self.objects.pop(object_key, None)

    def head(self, object_key: str) -> StoredObjectMetadata | None:
        content = self.objects.get(object_key)
        if content is None:
            return None
        return StoredObjectMetadata(
            object_key=object_key,
            content_type="image/webp",
            byte_size=len(content),
            sha256=sha256(content).hexdigest(),
            cache_control=None,
        )


class MediaServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.user_id = str(uuid4())
        self.db_session = session_factory()()
        self.temporary = TemporaryDirectory()
        self.source_store = MediaSourceStore(Path(self.temporary.name) / "private")
        self.processor = FakeProcessor()
        self.storage = FakeMediaStorage()
        self.policy = MediaServicePolicy(
            max_attempts=3,
            processing_stale_seconds=60,
            retry_base_seconds=1,
            retry_max_seconds=4,
            cleanup_grace_seconds=60,
            reconciliation_batch_size=16,
            staging_orphan_grace_seconds=60,
        )
        self.service = MediaService(
            db_session=self.db_session,
            source_store=self.source_store,
            processor=self.processor,
            storage=self.storage,
            policy=self.policy,
        )
        self.db_session.add(
            User(
                id=self.user_id,
                email=f"media-{self.user_id}@example.com",
                display_name="media-test",
            )
        )
        await self.db_session.flush()
        self.db_session.add(
            PlayerProfile(user_id=self.user_id, display_name="media-test")
        )
        await self.db_session.commit()

    async def asyncTearDown(self) -> None:
        await self.db_session.close()
        async with session_factory()() as cleanup:
            await cleanup.execute(
                update(PlayerProfile)
                .where(PlayerProfile.user_id == self.user_id)
                .values(avatar_asset_id=None, banner_asset_id=None)
            )
            await cleanup.execute(
                delete(MediaAsset).where(MediaAsset.owner_user_id == self.user_id)
            )
            await cleanup.execute(delete(User).where(User.id == self.user_id))
            await cleanup.commit()
        self.temporary.cleanup()
        await dispose_engine()

    async def _accept_avatar(self, *, enqueue=None, before_commit=None):
        return await self.service.accept_upload(
            chunks=(TINY_PNG[:16], TINY_PNG[16:]),
            declared_mime="image/png",
            purpose="profile_avatar",
            owner_user_id=self.user_id,
            enqueue=enqueue,
            before_commit=before_commit,
        )

    async def _profile(self) -> PlayerProfile:
        profile = await self.db_session.scalar(
            select(PlayerProfile).where(PlayerProfile.user_id == self.user_id)
        )
        assert profile is not None
        await self.db_session.refresh(profile)
        return profile

    async def test_enqueue_failure_remains_pending_and_reconciliation_recovers(self) -> None:
        def unavailable_broker(_: str) -> None:
            raise RuntimeError("broker unavailable")

        accepted = await self._accept_avatar(enqueue=unavailable_broker)
        self.assertFalse(accepted.enqueued)
        asset = await self.db_session.get(MediaAsset, accepted.asset_id)
        self.assertIsNotNone(asset)
        self.assertEqual(asset.status, "pending")
        self.assertTrue(self.source_store.path_for(accepted.asset_id).is_file())

        reconciliation = await self.service.reconcile()

        self.assertIn(accepted.asset_id, reconciliation.process_asset_ids)

    async def test_ready_commit_binds_variants_and_removes_private_source(self) -> None:
        queued: list[str] = []
        accepted = await self._accept_avatar(enqueue=queued.append)
        self.assertEqual(queued, [accepted.asset_id])

        result = await self.service.process_asset(accepted.asset_id)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.variants, 3)
        profile = await self._profile()
        self.assertEqual(profile.avatar_asset_id, accepted.asset_id)
        self.assertIsNone(profile.avatar_url)
        variants = list(
            (
                await self.db_session.scalars(
                    select(MediaVariant).where(MediaVariant.asset_id == accepted.asset_id)
                )
            ).all()
        )
        self.assertEqual(len(variants), 3)
        self.assertEqual(len(self.storage.objects), 3)
        self.assertFalse(self.source_store.path_for(accepted.asset_id).exists())
        descriptors = await MediaRepository(self.db_session).descriptors(
            (accepted.asset_id, accepted.asset_id)
        )
        self.assertEqual(tuple(descriptors), (accepted.asset_id,))
        self.assertEqual(descriptors[accepted.asset_id].status, "ready")
        self.assertEqual(len(descriptors[accepted.asset_id].variants), 3)

    async def test_partial_storage_failure_keeps_old_active_and_cleans_new_keys(self) -> None:
        old = await self._accept_avatar()
        self.assertEqual((await self.service.process_asset(old.asset_id)).status, "ready")
        old_keys = set(self.storage.objects)
        self.storage.fail_on_attempt = self.storage.put_attempts + 2

        replacement = await self._accept_avatar()
        result = await self.service.process_asset(replacement.asset_id)

        self.assertEqual(result.status, "pending")
        profile = await self._profile()
        self.assertEqual(profile.avatar_asset_id, old.asset_id)
        self.assertTrue(old_keys.issubset(self.storage.objects))
        self.assertFalse(
            any(f"/{replacement.asset_id}/" in key for key in self.storage.objects)
        )
        replacement_row = await self.db_session.get(MediaAsset, replacement.asset_id)
        self.assertEqual(replacement_row.attempt_count, 1)
        self.assertIsNotNone(replacement_row.next_retry_at)

    async def test_later_pending_upload_supersedes_earlier_before_processing(self) -> None:
        earlier = await self._accept_avatar()
        later = await self._accept_avatar()
        earlier_row = await self.db_session.get(MediaAsset, earlier.asset_id)
        await self.db_session.refresh(earlier_row)
        self.assertEqual(earlier_row.status, "replaced")
        self.assertIn(earlier.asset_id, later.superseded_asset_ids)

        earlier_result = await self.service.process_asset(earlier.asset_id)
        later_result = await self.service.process_asset(later.asset_id)

        self.assertEqual(earlier_result.status, "replaced")
        self.assertEqual(later_result.status, "ready")
        self.assertEqual((await self._profile()).avatar_asset_id, later.asset_id)
        self.assertEqual(self.processor.calls, [later.asset_id])

    async def test_acceptance_hook_failure_rolls_back_asset_and_private_source(self) -> None:
        async def reject_acceptance(_staged, _superseded) -> None:
            raise RuntimeError("audit unavailable")

        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            await self._accept_avatar(before_commit=reject_acceptance)

        assets = tuple(
            await self.db_session.scalars(
                select(MediaAsset).where(MediaAsset.owner_user_id == self.user_id)
            )
        )
        self.assertEqual(assets, ())
        self.assertEqual(tuple(self.source_store.staged_asset_ids(limit=8)), ())

    async def test_unlink_cancels_inflight_upload_before_it_can_activate(self) -> None:
        accepted = await self._accept_avatar()
        unlinked_id = await self.service.unlink_active(
            purpose="profile_avatar",
            owner_id=self.user_id,
        )

        self.assertEqual(unlinked_id, accepted.asset_id)
        row = await self.db_session.get(MediaAsset, accepted.asset_id)
        self.assertEqual(row.status, "replaced")
        result = await self.service.process_asset(accepted.asset_id)
        self.assertEqual(result.status, "replaced")
        self.assertIsNone((await self._profile()).avatar_asset_id)


if __name__ == "__main__":
    unittest.main()
