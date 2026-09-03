from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import UserResponse
from apps.platform_api.app.services.current_user import (
    serialize_current_user_from_account_read_model,
)
from apps.platform_api.app.services.user_account_read_models import (
    get_or_build_user_account_read_model,
)
from python_packages.platform_infra.db import get_db_session, release_db_connection
from python_packages.platform_infra.security import get_authenticated_session

router = APIRouter()


@router.get("/me", response_model=UserResponse)
async def get_me(
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    try:
        account = await get_or_build_user_account_read_model(
            db_session,
            user=auth_session.user,
            now=auth_session.now,
        )
        return serialize_current_user_from_account_read_model(
            auth_session.user,
            role_slugs=auth_session.role_slugs,
            account=account,
        )
    finally:
        await release_db_connection(db_session)
