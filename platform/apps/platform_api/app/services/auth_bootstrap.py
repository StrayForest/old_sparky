from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import AuthBootstrapResponse
from apps.platform_api.app.services.profile_read_models import (
    get_profile_avatar_read_model,
    get_or_build_profile_read_model,
)


def _cached_profile_fields(payload: bytes | None) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        decoded = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    profile = decoded.get("profile") if isinstance(decoded, dict) else None
    return profile if isinstance(profile, dict) else {}


async def build_auth_bootstrap(
    auth_session,
    *,
    db_session: AsyncSession | None = None,
) -> AuthBootstrapResponse:
    """Build the global-shell identity without full account hydration."""

    if db_session is None:
        profile = _cached_profile_fields(
            await get_or_build_profile_read_model(auth_session.user.id)
        )
        avatar_url = (
            profile.get("avatar_url")
            if isinstance(profile.get("avatar_url"), str)
            else None
        )
        avatar_media = (
            profile.get("avatar_media")
            if isinstance(profile.get("avatar_media"), dict)
            else None
        )
    else:
        avatar_projection = await get_profile_avatar_read_model(
            db_session,
            auth_session.user.id,
        )
        avatar_url = avatar_projection.avatar_url if avatar_projection else None
        avatar_media = avatar_projection.avatar_media if avatar_projection else None
    return AuthBootstrapResponse(
        id=auth_session.user.id,
        email=auth_session.user.email,
        display_name=auth_session.user.display_name,
        status=auth_session.user.status,
        created_at=auth_session.user.created_at,
        roles=sorted(auth_session.role_slugs),
        can_create_public_tournaments=(
            "admin" in auth_session.role_slugs
            or "superadmin" in auth_session.role_slugs
            or int(auth_session.user.public_tournament_credits or 0) > 0
        ),
        public_tournament_credits=int(
            auth_session.user.public_tournament_credits or 0
        ),
        private_tournament_credits=int(
            auth_session.user.private_tournament_credits or 0
        ),
        avatar_url=avatar_url,
        avatar_media=avatar_media,
    )
