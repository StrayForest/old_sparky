from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.profile_schemas import (
    CaptainProfileResponse,
    CaptainProfileUpdateRequest,
    ProfileBootstrapResponse,
    ProfileWorkspaceResponse,
)
from apps.platform_api.app.api.schemas import (
    DeadlockDreamSlotResponse,
    DeadlockProfileResponse,
    MyProfileResponse,
)
from apps.platform_api.app.services.home_content import get_supported_deadlock_hero_names
from apps.platform_api.app.services.media import compatibility_media_url, load_media_descriptors
from python_packages.platform_domain.deadlock import validate_dream_slot_payload
from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.models import (
    DeadlockDreamSlot,
    DeadlockProfile,
    ExternalIdentity,
    PlayerProfile,
    User,
)
from python_packages.platform_infra.security import invalidate_user_session_cache


async def _profile_media_descriptors(db_session: AsyncSession, profile: PlayerProfile):
    descriptors = await load_media_descriptors(
        db_session,
        (profile.avatar_asset_id, profile.banner_asset_id),
    )
    return (
        descriptors.get(profile.avatar_asset_id) if profile.avatar_asset_id else None,
        descriptors.get(profile.banner_asset_id) if profile.banner_asset_id else None,
    )


async def _verified_steam_id(
    db_session: AsyncSession,
    *,
    user_id: str,
) -> str | None:
    return await db_session.scalar(
        select(ExternalIdentity.subject).where(
            ExternalIdentity.user_id == user_id,
            ExternalIdentity.provider == "steam",
        )
    )


def _serialize_profile(
    profile: PlayerProfile,
    *,
    account_email: str | None,
    avatar_media,
    banner_media,
    steam_id: str | None,
) -> MyProfileResponse:
    return MyProfileResponse(
        user_id=profile.user_id,
        account_email=account_email,
        display_name=profile.display_name,
        handle=profile.handle,
        avatar_url=compatibility_media_url(
            avatar_media,
            preferred_variant="avatar-256",
        ),
        banner_url=compatibility_media_url(
            banner_media,
            preferred_variant="banner-1920",
        ),
        avatar_media=avatar_media,
        banner_media=banner_media,
        bio=profile.bio,
        contact_email=profile.contact_email,
        region=profile.region,
        steam_id=steam_id,
        steam_linked=steam_id is not None,
        discord_account=profile.discord_account,
        captain_team_name=profile.captain_team_name,
        updated_at=profile.updated_at,
    )


def _serialize_dream_slot(
    *,
    user_id: str,
    slot_number: int,
    allowed_roles: list[str],
    desired_heroes: list[str],
    updated_at,
) -> DeadlockDreamSlotResponse:
    return DeadlockDreamSlotResponse(
        user_id=user_id,
        slot_number=slot_number,
        allowed_roles=allowed_roles,
        desired_heroes=desired_heroes,
        updated_at=updated_at,
    )


async def _list_dream_slots(
    db_session: AsyncSession,
    *,
    user_id: str,
) -> list[DeadlockDreamSlotResponse]:
    rows = (
        await db_session.scalars(
            select(DeadlockDreamSlot)
            .where(DeadlockDreamSlot.user_id == user_id)
            .order_by(DeadlockDreamSlot.slot_number.asc())
        )
    ).all()
    stored_by_slot = {row.slot_number: row for row in rows}
    slots: list[DeadlockDreamSlotResponse] = []
    for slot_number in range(1, 7):
        row = stored_by_slot.get(slot_number)
        slots.append(
            _serialize_dream_slot(
                user_id=user_id,
                slot_number=slot_number,
                allowed_roles=list(row.allowed_roles or []) if row is not None else [],
                desired_heroes=list(row.desired_heroes or []) if row is not None else [],
                updated_at=row.updated_at if row is not None else None,
            )
        )
    return slots


async def load_profile_workspace(
    db_session: AsyncSession,
    *,
    user,
) -> ProfileWorkspaceResponse:
    profile, deadlock_profile, dream_slots = await _load_profile_workspace_parts(
        db_session,
        user=user,
    )
    return ProfileWorkspaceResponse(
        profile=profile,
        deadlock_profile=deadlock_profile,
        dream_slots=dream_slots,
    )


async def _load_profile_workspace_parts(
    db_session: AsyncSession,
    *,
    user,
):
    row = (
        await db_session.execute(
            select(PlayerProfile, DeadlockProfile)
            .outerjoin(
                DeadlockProfile,
                DeadlockProfile.user_id == PlayerProfile.user_id,
            )
            .where(PlayerProfile.user_id == user.id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile not found.",
        )
    profile, deadlock_profile = row

    avatar_media, banner_media = await _profile_media_descriptors(db_session, profile)
    steam_id = await _verified_steam_id(db_session, user_id=user.id)
    dream_slots = await _list_dream_slots(db_session, user_id=user.id)

    return (
        _serialize_profile(
            profile,
            account_email=user.email,
            avatar_media=avatar_media,
            banner_media=banner_media,
            steam_id=steam_id,
        ),
        (
            DeadlockProfileResponse.model_validate(deadlock_profile)
            if deadlock_profile is not None
            else None
        ),
        dream_slots,
    )


async def load_profile_bootstrap(
    db_session: AsyncSession,
    *,
    user,
    role_slugs: frozenset[str],
) -> ProfileBootstrapResponse:
    profile, deadlock_profile, dream_slots = await _load_profile_workspace_parts(
        db_session,
        user=user,
    )
    return ProfileBootstrapResponse(
        account={
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "status": user.status,
            "created_at": user.created_at,
            "roles": sorted(role_slugs),
            "steam_id": profile.steam_id,
            "steam_linked": profile.steam_id is not None,
        },
        profile=profile,
        deadlock_profile=deadlock_profile,
        dream_slots=dream_slots,
    )


async def update_captain_profile(
    db_session: AsyncSession,
    *,
    user,
    payload: CaptainProfileUpdateRequest,
) -> CaptainProfileResponse:
    supported_heroes = await get_supported_deadlock_hero_names()
    normalized_by_slot: dict[int, dict[str, list[str]]] = {}

    for slot in payload.slots:
        try:
            normalized = validate_dream_slot_payload(
                {
                    "allowed_roles": slot.allowed_roles,
                    "desired_heroes": slot.desired_heroes,
                },
                supported_heroes=supported_heroes,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        normalized_by_slot[slot.slot_number] = {
            "allowed_roles": list(normalized["allowed_roles"]),
            "desired_heroes": list(normalized["desired_heroes"]),
        }

    # The dedicated dream-slots endpoint uses this same parent-row lock.
    # It serializes replace-all updates even while no child rows exist.
    await db_session.scalar(
        select(User).where(User.id == user.id).with_for_update()
    )
    profile = await db_session.scalar(
        select(PlayerProfile).where(PlayerProfile.user_id == user.id)
    )
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile not found.",
        )

    normalized_team_name = payload.captain_team_name.strip()
    profile.captain_team_name = normalized_team_name or None

    await db_session.execute(
        delete(DeadlockDreamSlot).where(DeadlockDreamSlot.user_id == user.id)
    )
    for slot_number in range(1, 7):
        slot = normalized_by_slot.get(
            slot_number,
            {"allowed_roles": [], "desired_heroes": []},
        )
        if not slot["allowed_roles"] and not slot["desired_heroes"]:
            continue
        db_session.add(
            DeadlockDreamSlot(
                user_id=user.id,
                slot_number=slot_number,
                allowed_roles=list(slot["allowed_roles"]),
                desired_heroes=list(slot["desired_heroes"]),
            )
        )

    await write_audit_log(
        db_session,
        actor_user_id=user.id,
        action="profile.captain.update",
        subject_type="profile",
        subject_id=user.id,
        payload={
            "captain_team_name": normalized_team_name,
            "configured_slots": [
                slot_number
                for slot_number, slot in normalized_by_slot.items()
                if slot["allowed_roles"] or slot["desired_heroes"]
            ],
        },
    )

    try:
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise

    invalidate_user_session_cache(user.id)
    return CaptainProfileResponse(
        captain_team_name=normalized_team_name,
        dream_slots=await _list_dream_slots(db_session, user_id=user.id),
    )
