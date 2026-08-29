from __future__ import annotations

from collections.abc import Iterator
import logging
from urllib.parse import quote

from fastapi import HTTPException, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import (
    MediaAcceptedResponse,
    MediaDescriptorResponse,
    MediaVariantResponse,
)
from python_packages.platform_infra.config import PlatformSettings, get_settings
from python_packages.platform_infra.media.errors import MediaError
from python_packages.platform_infra.media.repository import (
    AssetDescriptor,
    MediaRepository,
)
from python_packages.platform_infra.media.service import AcceptedMedia, MediaService
from python_packages.platform_infra.media.tasks import build_media_service


logger = logging.getLogger(__name__)


MEDIA_ERROR_HTTP_STATUS = {
    "media_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
    "unsupported_media_type": status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    "media_type_mismatch": status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    "invalid_media": status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    "animated_media_not_allowed": status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    "empty_media": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "media_staging_full": status.HTTP_503_SERVICE_UNAVAILABLE,
    "staged_source_conflict": status.HTTP_409_CONFLICT,
}

MEDIA_ERROR_MESSAGES = {
    "media_too_large": "Media source is too large.",
    "unsupported_media_type": "Only JPEG, PNG, and WebP images are accepted.",
    "media_type_mismatch": "Media content does not match its declared type.",
    "invalid_media": "Media source is invalid.",
    "animated_media_not_allowed": "Animated images are not accepted.",
    "empty_media": "Media source is empty.",
    "media_staging_full": "Media uploads are temporarily at capacity.",
    "staged_source_conflict": "Media upload conflicts with existing staged data.",
}


def upload_file_chunks(file: UploadFile) -> Iterator[bytes]:
    return iter(lambda: file.file.read(64 * 1024), b"")


def upload_size_hint(request: Request, file: UploadFile) -> int:
    file_size = getattr(file, "size", None)
    if isinstance(file_size, int) and file_size >= 0:
        return file_size
    try:
        return max(0, int(request.headers.get("content-length", "0")))
    except ValueError:
        return 0


def api_media_service(db_session: AsyncSession) -> MediaService:
    try:
        return build_media_service(db_session)
    except Exception as exc:
        logger.exception("Media API runtime is unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "media_unavailable",
                "message": "Media uploads are temporarily unavailable.",
            },
        ) from exc


def enqueue_media_asset(asset_id: str) -> object:
    from apps.platform_worker.worker import media_process_asset

    return media_process_asset.delay(asset_id)


def raise_media_http_error(exc: MediaError) -> None:
    status_code = MEDIA_ERROR_HTTP_STATUS.get(
        exc.code,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    )
    headers = {"Retry-After": "60"} if exc.code == "media_staging_full" else None
    raise HTTPException(
        status_code=status_code,
        detail={
            "code": exc.code,
            "message": MEDIA_ERROR_MESSAGES.get(exc.code, "Media upload was rejected."),
        },
        headers=headers,
    ) from exc


def accepted_media_response(accepted: AcceptedMedia) -> MediaAcceptedResponse:
    return MediaAcceptedResponse(
        asset_id=accepted.asset_id,
        status=accepted.status,
        status_url=f"/api/v1/media/{accepted.asset_id}/status",
    )


def public_media_url(
    object_key: str,
    *,
    settings: PlatformSettings | None = None,
) -> str:
    resolved = settings or get_settings()
    base_url = (resolved.platform_media_public_base_url or "").rstrip("/")
    if not base_url:
        raise RuntimeError("PLATFORM_MEDIA_PUBLIC_BASE_URL is not configured")
    return f"{base_url}/{quote(object_key, safe='/')}"


def media_descriptor_response(
    descriptor: AssetDescriptor | None,
    *,
    settings: PlatformSettings | None = None,
) -> MediaDescriptorResponse | None:
    if descriptor is None:
        return None
    variants = []
    if descriptor.status == "ready":
        variants = [
            MediaVariantResponse(
                name=variant.variant_name,
                width=variant.width,
                height=variant.height,
                byte_size=variant.byte_size,
                url=public_media_url(variant.object_key, settings=settings),
            )
            for variant in descriptor.variants
        ]
    return MediaDescriptorResponse(
        asset_id=descriptor.asset_id,
        purpose=descriptor.purpose,
        status=descriptor.status,
        error_code=descriptor.error_code,
        variants=variants,
    )


def compatibility_media_url(
    descriptor: MediaDescriptorResponse | None,
    *,
    preferred_variant: str,
) -> str | None:
    """Resolve a runtime image URL exclusively from a ready CDN media descriptor.
    """
    if descriptor is None or descriptor.status != "ready":
        return None
    preferred = next(
        (
            variant.url
            for variant in descriptor.variants
            if variant.name == preferred_variant
        ),
        None,
    )
    if preferred is not None:
        return preferred
    return descriptor.variants[-1].url if descriptor.variants else None


async def load_media_descriptors(
    db_session: AsyncSession,
    asset_ids: tuple[str | None, ...],
) -> dict[str, MediaDescriptorResponse]:
    requested_ids = tuple(asset_id for asset_id in asset_ids if asset_id)
    descriptors = await MediaRepository(db_session).descriptors(requested_ids)
    responses: dict[str, MediaDescriptorResponse] = {}
    for asset_id, descriptor in descriptors.items():
        response = media_descriptor_response(descriptor)
        if response is not None:
            responses[asset_id] = response
    return responses
