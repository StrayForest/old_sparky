from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import logging
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.media.errors import (
    MediaError,
    MediaProcessingError,
    MediaStateError,
    MediaStorageError,
)
from python_packages.platform_infra.media.image_processor import (
    ImageProcessor,
    ProcessedVariant,
    VARIANT_SPECS,
    expected_object_keys,
)
from python_packages.platform_infra.media.repository import (
    MediaRepository,
    TERMINAL_MEDIA_STATUSES,
    VariantRecord,
)
from python_packages.platform_infra.media.r2_storage import MediaStorage
from python_packages.platform_infra.media.source_store import (
    MediaSourceStore,
    StagedSource,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaServicePolicy:
    max_attempts: int = 3
    processing_stale_seconds: int = 300
    retry_base_seconds: int = 10
    retry_max_seconds: int = 300
    cleanup_grace_seconds: int = 86_400
    reconciliation_batch_size: int = 32
    staging_orphan_grace_seconds: int = 3_600

    def __post_init__(self) -> None:
        values = (
            self.max_attempts,
            self.processing_stale_seconds,
            self.retry_base_seconds,
            self.retry_max_seconds,
            self.cleanup_grace_seconds,
            self.reconciliation_batch_size,
            self.staging_orphan_grace_seconds,
        )
        if any(value <= 0 for value in values):
            raise ValueError("Media service limits must be positive")
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("Media retry base cannot exceed retry maximum")


@dataclass(frozen=True)
class AcceptedMedia:
    asset_id: str
    status: str
    enqueued: bool
    superseded_asset_ids: tuple[str, ...]


@dataclass(frozen=True)
class MediaProcessResult:
    asset_id: str
    status: str
    variants: int = 0
    error_code: str | None = None
    owner_user_id: str | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "asset_id": self.asset_id,
            "status": self.status,
            "variants": self.variants,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class MediaReconciliationResult:
    process_asset_ids: tuple[str, ...]
    failed_exhausted: int
    cleaned_assets: int
    cleaned_sources: int

    def as_dict(self) -> dict[str, int]:
        return {
            "queued": len(self.process_asset_ids),
            "failed_exhausted": self.failed_exhausted,
            "cleaned_assets": self.cleaned_assets,
            "cleaned_sources": self.cleaned_sources,
        }


class MediaService:
    def __init__(
        self,
        *,
        db_session: AsyncSession,
        source_store: MediaSourceStore,
        processor: ImageProcessor,
        storage: MediaStorage,
        policy: MediaServicePolicy | None = None,
    ) -> None:
        self.db_session = db_session
        self.repository = MediaRepository(db_session)
        self.source_store = source_store
        self.processor = processor
        self.storage = storage
        self.policy = policy or MediaServicePolicy()

    async def accept_upload(
        self,
        *,
        chunks: Iterable[bytes],
        declared_mime: str | None,
        purpose: str,
        owner_user_id: str | None = None,
        tournament_id: str | None = None,
        enqueue: Callable[[str], object] | None = None,
        before_commit: Callable[[StagedSource, tuple[str, ...]], Awaitable[None]]
        | None = None,
    ) -> AcceptedMedia:
        asset_id = str(uuid4())
        staged = await asyncio.to_thread(
            self.source_store.stage,
            asset_id,
            chunks,
            declared_mime=declared_mime,
        )
        return await self.accept_staged(
            staged=staged,
            purpose=purpose,
            owner_user_id=owner_user_id,
            tournament_id=tournament_id,
            enqueue=enqueue,
            before_commit=before_commit,
        )

    async def accept_staged(
        self,
        *,
        staged: StagedSource,
        purpose: str,
        owner_user_id: str | None = None,
        tournament_id: str | None = None,
        enqueue: Callable[[str], object] | None = None,
        before_commit: Callable[[StagedSource, tuple[str, ...]], Awaitable[None]]
        | None = None,
    ) -> AcceptedMedia:
        now = datetime.now(UTC)
        commit_error: Exception | None = None
        try:
            superseded = await self.repository.supersede_inflight_for_new_asset(
                purpose=purpose,
                owner_user_id=owner_user_id,
                tournament_id=tournament_id,
                now=now,
                cleanup_grace=timedelta(seconds=self.policy.cleanup_grace_seconds),
            )
            self.repository.add_pending_asset(
                asset_id=staged.asset_id,
                purpose=purpose,
                owner_user_id=owner_user_id,
                tournament_id=tournament_id,
                source_mime=staged.mime_type,
                source_bytes=staged.byte_size,
                source_sha256=staged.sha256,
            )
            if before_commit is not None:
                await before_commit(staged, superseded)
            await self.db_session.commit()
        except Exception as exc:
            commit_error = exc
            await self.db_session.rollback()
            try:
                durable_asset = await self.repository.get_asset(staged.asset_id)
                commit_was_durable = bool(
                    durable_asset is not None
                    and durable_asset.status == "pending"
                    and durable_asset.source_mime == staged.mime_type
                    and durable_asset.source_bytes == staged.byte_size
                    and durable_asset.source_sha256 == staged.sha256
                )
                await self.db_session.rollback()
            except Exception:
                logger.exception(
                    "Could not resolve ambiguous pending-media commit for asset %s; "
                    "the private source is retained for reconciliation.",
                    staged.asset_id,
                )
                raise commit_error
            if not commit_was_durable:
                self.source_store.delete(staged.asset_id)
                raise commit_error

        enqueued = False
        if enqueue is not None:
            try:
                enqueue(staged.asset_id)
                enqueued = True
            except Exception:
                logger.warning(
                    "Media enqueue failed for asset %s; reconciliation will retry.",
                    staged.asset_id,
                )
        return AcceptedMedia(
            asset_id=staged.asset_id,
            status="pending",
            enqueued=enqueued,
            superseded_asset_ids=superseded,
        )

    async def process_asset(self, asset_id: str) -> MediaProcessResult:
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=self.policy.processing_stale_seconds)
        asset = await self.repository.claim_for_processing(
            asset_id,
            now=now,
            stale_before=stale_before,
            max_attempts=self.policy.max_attempts,
        )
        await self.db_session.commit()
        if asset is None:
            existing = await self.repository.get_asset(asset_id)
            status = existing.status if existing is not None else "missing"
            return MediaProcessResult(
                asset_id=asset_id,
                status=status,
                owner_user_id=existing.owner_user_id if existing is not None else None,
            )

        purpose = asset.purpose
        owner_id = asset.owner_user_id or asset.tournament_id
        assert owner_id is not None
        try:
            staged = self.source_store.describe(asset_id)
            if (
                staged.mime_type != asset.source_mime
                or staged.byte_size != asset.source_bytes
                or staged.sha256 != asset.source_sha256
            ):
                raise MediaStateError(
                    "media_source_mismatch",
                    "Staged source does not match durable media metadata",
                )
            variants = self.processor.process(
                staged.path,
                purpose=purpose,
                asset_id=asset.id,
                owner_id=owner_id,
            )
            self._validate_prepared_variants(
                variants,
                purpose=purpose,
                owner_id=owner_id,
                asset_id=asset.id,
            )
        except MediaError as exc:
            retriable = isinstance(exc, MediaProcessingError) and exc.code in {
                "media_processing_timeout",
                "media_processor_unavailable",
            }
            return await self._record_processing_failure(
                asset_id,
                error_code=exc.code,
                retriable=retriable,
            )
        except Exception:
            logger.exception(
                "Unexpected media processing failure for asset %s", asset_id
            )
            return await self._record_processing_failure(
                asset_id,
                error_code="media_processing_error",
                retriable=True,
            )

        uploaded_keys: list[str] = []
        try:
            for variant in variants:
                uploaded_keys.append(variant.object_key)
                self.storage.put(
                    variant.object_key,
                    variant.content,
                    content_type=variant.mime_type,
                    content_sha256=variant.sha256,
                )
        except MediaStorageError as exc:
            self._best_effort_delete(uploaded_keys)
            return await self._record_processing_failure(
                asset_id,
                error_code=exc.code,
                retriable=exc.retriable,
            )
        except Exception:
            self._best_effort_delete(uploaded_keys)
            logger.exception("Unexpected media storage failure for asset %s", asset_id)
            return await self._record_processing_failure(
                asset_id,
                error_code="media_storage_error",
                retriable=True,
            )

        records = tuple(self._variant_record(variant) for variant in variants)
        try:
            await self.repository.finalize_ready(
                asset_id,
                records,
                now=datetime.now(UTC),
                cleanup_grace=timedelta(seconds=self.policy.cleanup_grace_seconds),
            )
            await self.db_session.commit()
        except Exception:
            logger.exception("Media DB finalization failed for asset %s", asset_id)
            try:
                await self.db_session.rollback()
                durable_asset = await self.repository.get_asset(asset_id)
                if durable_asset is not None and durable_asset.status == "ready":
                    self.source_store.delete(asset_id)
                    return MediaProcessResult(
                        asset_id=asset_id,
                        status="ready",
                        variants=len(variants),
                        owner_user_id=asset.owner_user_id,
                    )
            except Exception:
                logger.exception(
                    "Could not resolve ambiguous media commit for asset %s; "
                    "objects are retained to avoid breaking a possible DB reference.",
                    asset_id,
                )
                return MediaProcessResult(
                    asset_id=asset_id,
                    status="processing",
                    variants=len(variants),
                    error_code="media_commit_unknown",
                )
            self._best_effort_delete(uploaded_keys)
            return await self._record_processing_failure(
                asset_id,
                error_code="media_database_error",
                retriable=True,
            )

        self.source_store.delete(asset_id)
        return MediaProcessResult(
            asset_id=asset_id,
            status="ready",
            variants=len(variants),
            owner_user_id=asset.owner_user_id,
        )

    async def unlink_active(
        self,
        *,
        purpose: str,
        owner_id: str,
        before_commit: Callable[[str | None], Awaitable[None]] | None = None,
    ) -> str | None:
        try:
            asset_id = await self.repository.unlink_active_asset(
                purpose=purpose,
                owner_id=owner_id,
                now=datetime.now(UTC),
                cleanup_grace=timedelta(seconds=self.policy.cleanup_grace_seconds),
            )
            if before_commit is not None:
                await before_commit(asset_id)
            await self.db_session.commit()
            return asset_id
        except Exception:
            await self.db_session.rollback()
            raise

    async def reconcile(self) -> MediaReconciliationResult:
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=self.policy.processing_stale_seconds)
        failed_exhausted = await self.repository.fail_exhausted_processing(
            stale_before=stale_before,
            max_attempts=self.policy.max_attempts,
            now=now,
        )
        process_asset_ids = await self.repository.reconcilable_asset_ids(
            now=now,
            stale_before=stale_before,
            max_attempts=self.policy.max_attempts,
            limit=self.policy.reconciliation_batch_size,
        )
        cleanup_ids = await self.repository.cleanup_candidate_ids(
            now=now,
            limit=self.policy.reconciliation_batch_size,
        )
        await self.db_session.commit()

        cleaned_assets = 0
        for asset_id in cleanup_ids:
            if await self._cleanup_replaced_asset(asset_id):
                cleaned_assets += 1

        cleaned_sources = await self._cleanup_terminal_and_orphan_sources(now=now)
        return MediaReconciliationResult(
            process_asset_ids=process_asset_ids,
            failed_exhausted=failed_exhausted,
            cleaned_assets=cleaned_assets,
            cleaned_sources=cleaned_sources,
        )

    async def _record_processing_failure(
        self,
        asset_id: str,
        *,
        error_code: str,
        retriable: bool,
    ) -> MediaProcessResult:
        try:
            await self.db_session.rollback()
            status = await self.repository.mark_retry_or_failed(
                asset_id,
                error_code=error_code,
                retriable=retriable,
                now=datetime.now(UTC),
                max_attempts=self.policy.max_attempts,
                retry_base_seconds=self.policy.retry_base_seconds,
                retry_max_seconds=self.policy.retry_max_seconds,
            )
            await self.db_session.commit()
        except Exception:
            await self.db_session.rollback()
            logger.exception(
                "Could not persist media failure state for asset %s", asset_id
            )
            return MediaProcessResult(
                asset_id=asset_id,
                status="processing",
                error_code=error_code,
            )
        if status in TERMINAL_MEDIA_STATUSES:
            self.source_store.delete(asset_id)
        return MediaProcessResult(
            asset_id=asset_id, status=status, error_code=error_code
        )

    async def _cleanup_replaced_asset(self, asset_id: str) -> bool:
        asset = await self.repository.get_asset(asset_id)
        if asset is None or asset.status != "replaced":
            return False
        owner_id = asset.owner_user_id or asset.tournament_id
        if owner_id is None:
            return False
        stored_keys = await self.repository.variant_object_keys(asset_id)
        expected_keys = expected_object_keys(
            purpose=asset.purpose,
            owner_id=owner_id,
            asset_id=asset.id,
        )
        keys = tuple(dict.fromkeys((*stored_keys, *expected_keys)))
        await self.db_session.rollback()
        try:
            self.storage.delete_many(keys)
        except MediaStorageError:
            logger.exception("Media cleanup failed for asset %s", asset_id)
            await self.db_session.rollback()
            return False
        try:
            marked = await self.repository.mark_deleted(asset_id, now=datetime.now(UTC))
            await self.db_session.commit()
        except Exception:
            await self.db_session.rollback()
            logger.exception("Could not persist media cleanup for asset %s", asset_id)
            return False
        self.source_store.delete(asset_id)
        return marked

    async def _cleanup_terminal_and_orphan_sources(self, *, now: datetime) -> int:
        orphan_before = now.timestamp() - self.policy.staging_orphan_grace_seconds
        cleaned = self.source_store.cleanup_stale_temporary_files(
            older_than_epoch=orphan_before,
            limit=self.policy.reconciliation_batch_size,
        )
        asset_ids = tuple(
            self.source_store.staged_asset_ids(
                limit=self.policy.reconciliation_batch_size * 2,
            )
        )
        statuses = await self.repository.asset_statuses(asset_ids)
        for asset_id in asset_ids:
            status = statuses.get(asset_id)
            source_path = self.source_store.path_for(asset_id)
            try:
                source_mtime = source_path.stat().st_mtime
            except FileNotFoundError:
                continue
            is_old_orphan = status is None and source_mtime <= orphan_before
            if status in TERMINAL_MEDIA_STATUSES or is_old_orphan:
                self.source_store.delete(asset_id)
                cleaned += 1
        await self.db_session.rollback()
        return cleaned

    def _best_effort_delete(self, object_keys: list[str] | tuple[str, ...]) -> None:
        if not object_keys:
            return
        try:
            self.storage.delete_many(object_keys)
        except Exception:
            logger.exception(
                "Partial media cleanup failed; deterministic keys remain safe to retry."
            )

    @staticmethod
    def _variant_record(variant: ProcessedVariant) -> VariantRecord:
        return VariantRecord(
            variant_name=variant.variant_name,
            object_key=variant.object_key,
            mime_type=variant.mime_type,
            width=variant.width,
            height=variant.height,
            byte_size=variant.byte_size,
            sha256=variant.sha256,
        )

    @staticmethod
    def _validate_prepared_variants(
        variants: tuple[ProcessedVariant, ...],
        *,
        purpose: str,
        owner_id: str,
        asset_id: str,
    ) -> None:
        specs = VARIANT_SPECS[purpose]
        object_keys = expected_object_keys(
            purpose=purpose,
            owner_id=owner_id,
            asset_id=asset_id,
        )
        expected = {
            spec.name: (
                spec.width,
                spec.height,
                object_keys[index],
            )
            for index, spec in enumerate(specs)
        }
        if len(variants) != len(expected) or len(
            {item.variant_name for item in variants}
        ) != len(variants):
            raise MediaProcessingError(
                "invalid_media_variants",
                "Processor did not produce the complete unique variant set",
            )
        for variant in variants:
            expected_metadata = expected.get(variant.variant_name)
            if (
                expected_metadata is None
                or (variant.width, variant.height, variant.object_key)
                != expected_metadata
                or variant.mime_type != "image/webp"
                or not variant.content
                or sha256(variant.content).hexdigest() != variant.sha256
            ):
                raise MediaProcessingError(
                    "invalid_media_variants",
                    "Processor returned invalid prepared variant metadata",
                )
