from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path as ApiPath, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import MediaDescriptorResponse
from apps.platform_api.app.services.media import media_descriptor_response
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.media.repository import MediaRepository
from python_packages.platform_infra.models import MediaAsset, Tournament
from python_packages.platform_infra.security import get_authenticated_session


router = APIRouter()


def _is_platform_admin(auth_session) -> bool:
    return bool({"admin", "superadmin"}.intersection(auth_session.role_slugs))


@router.get("/{asset_id}/status", response_model=MediaDescriptorResponse)
async def get_owned_media_status(
    asset_id: str = ApiPath(min_length=36, max_length=36),
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> MediaDescriptorResponse:
    asset = await db_session.scalar(select(MediaAsset).where(MediaAsset.id == asset_id))
    allowed = False
    if asset is not None and asset.owner_user_id is not None:
        allowed = asset.owner_user_id == auth_session.user.id or _is_platform_admin(
            auth_session
        )
    elif asset is not None and asset.tournament_id is not None:
        organizer_user_id = await db_session.scalar(
            select(Tournament.organizer_user_id).where(
                Tournament.id == asset.tournament_id
            )
        )
        allowed = organizer_user_id == auth_session.user.id or _is_platform_admin(
            auth_session
        )
    if asset is None or not allowed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "media_not_found", "message": "Media asset was not found."},
        )

    descriptor = await MediaRepository(db_session).descriptor(asset.id)
    response = media_descriptor_response(descriptor)
    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "media_not_found", "message": "Media asset was not found."},
        )
    return response
