from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import PublicAppearanceResponse
from apps.platform_api.app.services.appearance_settings import load_appearance_settings
from python_packages.platform_domain.appearance_palette import DEFAULT_APPEARANCE_COLORS
from python_packages.platform_infra.db import get_db_session

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("", response_model=PublicAppearanceResponse)
async def public_appearance(
    response: Response,
    db_session: AsyncSession = Depends(get_db_session),
) -> PublicAppearanceResponse:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        payload = await load_appearance_settings(db_session)
    except Exception as exc:
        logger.error("Appearance settings read failed error=%s", type(exc).__name__)
        payload = {"colors": dict(DEFAULT_APPEARANCE_COLORS), "revision": 1}
    return PublicAppearanceResponse.model_validate(payload)
