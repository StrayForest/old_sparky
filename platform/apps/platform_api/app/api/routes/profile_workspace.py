from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.profile_schemas import (
    CaptainProfileResponse,
    CaptainProfileUpdateRequest,
    ProfileWorkspaceResponse,
)
from apps.platform_api.app.services.profile_workspace import (
    load_profile_workspace,
    update_captain_profile,
)
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.security import get_authenticated_session


router = APIRouter()


@router.get("/me/workspace", response_model=ProfileWorkspaceResponse)
async def get_my_profile_workspace(
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> ProfileWorkspaceResponse:
    return await load_profile_workspace(
        db_session,
        user=auth_session.user,
    )


@router.put("/me/captain", response_model=CaptainProfileResponse)
async def put_my_captain_profile(
    payload: CaptainProfileUpdateRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> CaptainProfileResponse:
    return await update_captain_profile(
        db_session,
        user=auth_session.user,
        payload=payload,
    )
