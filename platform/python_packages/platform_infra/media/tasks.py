from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.media.image_processor import (
    ImagePolicy,
    ImageProcessor,
)
from python_packages.platform_infra.media.r2_storage import R2Storage
from python_packages.platform_infra.media.service import (
    MediaProcessResult,
    MediaReconciliationResult,
    MediaService,
    MediaServicePolicy,
)
from python_packages.platform_infra.media.source_store import MediaSourceStore


def media_runtime_enabled(settings: object | None = None) -> bool:
    resolved = settings or get_settings()
    return (
        str(getattr(resolved, "platform_object_storage_backend", "local")).lower()
        == "r2"
    )


def build_media_service(
    db_session: AsyncSession,
    *,
    settings: object | None = None,
) -> MediaService:
    resolved = settings or get_settings()
    if not media_runtime_enabled(resolved):
        raise RuntimeError(
            "Production media processing requires the R2 object-storage backend"
        )
    if int(getattr(resolved, "platform_media_processing_concurrency", 1)) != 1:
        raise RuntimeError(
            "Media processing concurrency must remain 1 until a dedicated queue is benchmarked"
        )
    upload_root = Path(getattr(resolved, "platform_upload_dir")).resolve()
    configured_staging = getattr(resolved, "platform_media_staging_dir", None)
    staging_root = Path(
        configured_staging or upload_root.parent / "media-staging"
    ).resolve()
    if staging_root == upload_root or upload_root in staging_root.parents:
        raise RuntimeError("Media staging must be outside PLATFORM_UPLOAD_DIR")

    source_store = MediaSourceStore(
        staging_root,
        max_input_bytes=int(
            getattr(resolved, "platform_media_max_input_bytes", 5 * 1024 * 1024)
        ),
        max_staged_bytes=int(
            getattr(resolved, "platform_media_max_staged_bytes", 512 * 1024 * 1024)
        ),
        max_staged_files=int(getattr(resolved, "platform_media_max_staged_files", 256)),
    )
    if not source_store.is_shared_staging_root:
        raise RuntimeError(
            "Production media staging must be a root-owned setgid "
            "oldsparky-media directory"
        )
    processor = ImageProcessor(
        ImagePolicy(
            max_pixels=int(getattr(resolved, "platform_media_max_pixels", 25_000_000)),
            max_dimension=int(
                getattr(resolved, "platform_media_max_dimension", 10_000)
            ),
            max_variant_bytes=int(
                getattr(resolved, "platform_media_max_variant_bytes", 512 * 1024)
            ),
            processing_timeout_seconds=float(
                getattr(resolved, "platform_media_processing_timeout_seconds", 60.0)
            ),
        )
    )
    policy = MediaServicePolicy(
        max_attempts=int(
            getattr(resolved, "platform_media_processing_max_attempts", 3)
        ),
        processing_stale_seconds=int(
            getattr(resolved, "platform_media_processing_stale_seconds", 300)
        ),
        retry_base_seconds=int(
            getattr(resolved, "platform_media_retry_base_seconds", 10)
        ),
        retry_max_seconds=int(
            getattr(resolved, "platform_media_retry_max_seconds", 300)
        ),
        cleanup_grace_seconds=int(
            getattr(resolved, "platform_media_cleanup_grace_seconds", 86_400)
        ),
        reconciliation_batch_size=int(
            getattr(resolved, "platform_media_reconciliation_batch_size", 32)
        ),
        staging_orphan_grace_seconds=int(
            getattr(resolved, "platform_media_staging_orphan_grace_seconds", 3_600)
        ),
    )
    return MediaService(
        db_session=db_session,
        source_store=source_store,
        processor=processor,
        storage=R2Storage.from_settings(resolved),
        policy=policy,
    )


async def process_media_asset_once(
    db_session: AsyncSession,
    asset_id: str,
    *,
    settings: object | None = None,
) -> MediaProcessResult:
    return await build_media_service(db_session, settings=settings).process_asset(
        asset_id
    )


async def reconcile_media_once(
    db_session: AsyncSession,
    *,
    settings: object | None = None,
) -> MediaReconciliationResult:
    return await build_media_service(db_session, settings=settings).reconcile()
