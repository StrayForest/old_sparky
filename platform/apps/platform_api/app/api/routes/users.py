from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import UserResponse
from apps.platform_api.app.services.current_user import serialize_current_user
from apps.platform_api.app.services.tournament_allowances import (
    PRIVATE_TOURNAMENT_MONTHLY_LIMIT,
    private_tournament_monthly_remaining,
)
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.security import get_authenticated_session

router = APIRouter()


@router.get("/me", response_model=UserResponse)
async def get_me(
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    monthly_remaining = await private_tournament_monthly_remaining(
        db_session,
        organizer_user_id=auth_session.user.id,
        now=auth_session.now,
    )
    return await serialize_current_user(
        db_session,
        auth_session.user,
        role_slugs=auth_session.role_slugs,
        private_tournament_monthly_remaining=monthly_remaining,
        private_tournament_monthly_limit=PRIVATE_TOURNAMENT_MONTHLY_LIMIT,
    )
