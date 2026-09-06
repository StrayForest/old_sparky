from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import AuthBootstrapResponse
from apps.platform_api.app.services.profile_read_models import (
    get_profile_avatar_read_model,
)


async def build_auth_bootstrap(
    auth_session,
    *,
    db_session: AsyncSession | None = None,
) -> AuthBootstrapResponse:
    """Build the global-shell identity without full account hydration."""

    if db_session is None:
        # The API route always supplies the authoritative request session. Keep
        # direct service callers safe without reviving the full profile
        # read-model aggregate that this shell intentionally avoids.
        avatar_url = None
        avatar_media = None
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
