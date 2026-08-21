from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import UserResponse
from apps.platform_api.app.services.media import (
    compatibility_media_url,
    load_media_descriptors,
)
from python_packages.platform_infra.models import (
    ExternalIdentity,
    PasswordCredential,
    PlayerProfile,
    User,
)
from python_packages.platform_infra.security import role_slugs_for_user


async def serialize_current_user(
    db_session: AsyncSession,
    user: User,
    *,
    role_slugs: frozenset[str] | None = None,
    private_tournament_monthly_remaining: int = 1,
    private_tournament_monthly_limit: int = 1,
) -> UserResponse:
    """Build the authoritative auth snapshot consumed by the web provider."""

    resolved_roles = (
        role_slugs
        if role_slugs is not None
        else await role_slugs_for_user(db_session, user.id)
    )
    profile = await db_session.scalar(
        select(PlayerProfile).where(PlayerProfile.user_id == user.id)
    )
    steam_identity = await db_session.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.user_id == user.id,
            ExternalIdentity.provider == "steam",
        )
    )
    has_password = (
        await db_session.scalar(
            select(PasswordCredential.user_id).where(
                PasswordCredential.user_id == user.id
            )
        )
    ) is not None
    avatar_media = None
    avatar_url = None
    if profile is not None:
        descriptors = await load_media_descriptors(
            db_session,
            (profile.avatar_asset_id,),
        )
        avatar_media = (
            descriptors.get(profile.avatar_asset_id)
            if profile.avatar_asset_id
            else None
        )
        avatar_url = compatibility_media_url(
            avatar_media,
            preferred_variant="avatar-256",
            legacy_url=profile.avatar_url,
        )
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        created_at=user.created_at,
        roles=sorted(resolved_roles),
        can_create_public_tournaments=(
            "admin" in resolved_roles
            or "superadmin" in resolved_roles
            or int(user.public_tournament_credits or 0) > 0
        ),
        public_tournament_credits=int(user.public_tournament_credits or 0),
        private_tournament_credits=int(user.private_tournament_credits or 0),
        private_tournament_monthly_remaining=private_tournament_monthly_remaining,
        private_tournament_monthly_limit=private_tournament_monthly_limit,
        avatar_url=avatar_url,
        avatar_media=avatar_media,
        steam_id=steam_identity.subject if steam_identity is not None else None,
        steam_linked=steam_identity is not None,
        has_password=has_password,
    )
