from __future__ import annotations

import json
from typing import Any

from apps.platform_api.app.api.schemas import AuthBootstrapResponse
from apps.platform_api.app.services.profile_read_models import (
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


async def build_auth_bootstrap(auth_session) -> AuthBootstrapResponse:
    """Build the global-shell identity without full account hydration."""

    profile = _cached_profile_fields(
        await get_or_build_profile_read_model(auth_session.user.id)
    )
    avatar_media = profile.get("avatar_media")
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
        avatar_url=(
            profile.get("avatar_url")
            if isinstance(profile.get("avatar_url"), str)
            else None
        ),
        avatar_media=avatar_media if isinstance(avatar_media, dict) else None,
    )
