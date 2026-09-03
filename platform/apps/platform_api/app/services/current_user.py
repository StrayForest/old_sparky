from __future__ import annotations

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import MediaDescriptorResponse, UserResponse
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


def serialize_current_user_from_account_read_model(
    user: User,
    *,
    role_slugs: frozenset[str],
    account: dict[str, object],
) -> UserResponse:
    """Merge authoritative identity/roles with cached supplemental account data."""

    raw_avatar_media = account.get("avatar_media")
    avatar_media = (
        MediaDescriptorResponse.model_validate(raw_avatar_media)
        if isinstance(raw_avatar_media, dict)
        else None
    )
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        created_at=user.created_at,
        roles=sorted(role_slugs),
        can_create_public_tournaments=(
            "admin" in role_slugs
            or "superadmin" in role_slugs
            or int(user.public_tournament_credits or 0) > 0
        ),
        public_tournament_credits=int(user.public_tournament_credits or 0),
        private_tournament_credits=int(user.private_tournament_credits or 0),
        private_tournament_monthly_remaining=int(
            account.get("private_tournament_monthly_remaining", 1) or 0
        ),
        private_tournament_monthly_limit=int(
            account.get("private_tournament_monthly_limit", 1) or 1
        ),
        avatar_url=(
            str(account["avatar_url"])
            if account.get("avatar_url") is not None
            else None
        ),
        avatar_media=avatar_media,
        steam_id=(
            str(account["steam_id"]) if account.get("steam_id") is not None else None
        ),
        steam_linked=bool(account.get("steam_linked", False)),
        has_password=bool(account.get("has_password", False)),
        can_unlink_steam=bool(account.get("can_unlink_steam", False)),
    )


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
    snapshot = (
        await db_session.execute(
            select(PlayerProfile, ExternalIdentity, PasswordCredential.user_id)
            .select_from(User)
            .outerjoin(PlayerProfile, PlayerProfile.user_id == User.id)
            .outerjoin(
                ExternalIdentity,
                and_(
                    ExternalIdentity.user_id == User.id,
                    ExternalIdentity.provider == "steam",
                ),
            )
            .outerjoin(PasswordCredential, PasswordCredential.user_id == User.id)
            .where(User.id == user.id)
        )
    ).first()
    profile = snapshot[0] if snapshot is not None else None
    steam_identity = snapshot[1] if snapshot is not None else None
    has_password = snapshot is not None and snapshot[2] is not None
    can_unlink_steam = bool(
        user.email
        and user.email_verified_at is not None
        and has_password
    )
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
        can_unlink_steam=can_unlink_steam,
    )
