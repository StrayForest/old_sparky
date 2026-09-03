from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Path as ApiPath,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import (
    DeadlockDreamSlotResponse,
    DeadlockDreamSlotsBulkUpdateRequest,
    DeadlockProfileResponse,
    DeadlockProfileUpdateRequest,
    MediaAcceptedResponse,
    MediaDeleteAcceptedResponse,
    MediaDescriptorResponse,
    MyProfileResponse,
    ProfileUpdateRequest,
    PublicProfileResponse,
)
from apps.platform_api.app.services.home_content import get_supported_deadlock_hero_names
from apps.platform_api.app.services.tournament_catalog_read_models import (
    refresh_tournament_list_read_models_for_organizer_after_commit,
)
from apps.platform_api.app.services.profile_read_models import refresh_profile_read_model
from apps.platform_api.app.services.user_account_read_models import (
    delete_user_account_read_model,
)
from apps.platform_api.app.services.media import (
    accepted_media_response,
    api_media_service,
    compatibility_media_url,
    enqueue_media_asset,
    load_media_descriptors,
    raise_media_http_error,
    upload_file_chunks,
    upload_size_hint,
)
from python_packages.platform_domain.deadlock import (
    RegistrationPayload,
    normalize_pool,
    normalize_roles,
    validate_dream_slot_payload,
    validate_registration_payload,
)
from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.media.errors import MediaError
from python_packages.platform_infra.media.source_store import StagedSource
from python_packages.platform_infra.media_rate_limit import check_media_upload_rate_limit
from python_packages.platform_infra.models import (
    DeadlockDreamSlot,
    DeadlockProfile,
    ExternalIdentity,
    PlayerProfile,
    User,
)
from python_packages.platform_infra.security import (
    get_authenticated_session,
    invalidate_user_session_cache,
)

router = APIRouter()
logger = logging.getLogger(__name__)


async def _refresh_profile_read_model_after_commit(user_id: str) -> None:
    """Refresh the shared profile projection without failing a committed write."""

    try:
        await refresh_profile_read_model(user_id)
        await delete_user_account_read_model(user_id)
        await refresh_tournament_list_read_models_for_organizer_after_commit(user_id)
    except Exception:
        logger.exception(
            "Post-commit profile read-model refresh failed user_id=%s",
            user_id,
        )


def serialize_my_profile(
    profile: PlayerProfile,
    *,
    account_email: str | None,
    avatar_media: MediaDescriptorResponse | None = None,
    banner_media: MediaDescriptorResponse | None = None,
    verified_steam_id: str | None = None,
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
        steam_id=verified_steam_id,
        steam_linked=verified_steam_id is not None,
        discord_account=profile.discord_account,
        captain_team_name=profile.captain_team_name,
        updated_at=profile.updated_at,
    )


async def lock_profile_owner(db_session: AsyncSession, user_id: str) -> None:
    locked_user_id = await db_session.scalar(
        select(User.id).where(User.id == user_id).with_for_update()
    )
    if locked_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile owner not found.",
        )


async def profile_media_descriptors(
    db_session: AsyncSession,
    profile: PlayerProfile,
) -> tuple[MediaDescriptorResponse | None, MediaDescriptorResponse | None]:
    descriptors = await load_media_descriptors(
        db_session,
        (profile.avatar_asset_id, profile.banner_asset_id),
    )
    return (
        descriptors.get(profile.avatar_asset_id) if profile.avatar_asset_id else None,
        descriptors.get(profile.banner_asset_id) if profile.banner_asset_id else None,
    )


async def verified_steam_id_for_user(
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


def serialize_profile_dream_slot(
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


async def list_profile_dream_slots(
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
        if row is None:
            slots.append(
                serialize_profile_dream_slot(
                    user_id=user_id,
                    slot_number=slot_number,
                    allowed_roles=[],
                    desired_heroes=[],
                    updated_at=None,
                )
            )
            continue
        slots.append(
            serialize_profile_dream_slot(
                user_id=user_id,
                slot_number=slot_number,
                allowed_roles=list(row.allowed_roles or []),
                desired_heroes=list(row.desired_heroes or []),
                updated_at=row.updated_at,
            )
        )
    return slots


@router.get("/me", response_model=MyProfileResponse)
async def get_my_profile(
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> MyProfileResponse:
    profile = await db_session.scalar(
        select(PlayerProfile).where(PlayerProfile.user_id == auth_session.user.id)
    )
    avatar_media, banner_media = await profile_media_descriptors(db_session, profile)
    verified_steam_id = await verified_steam_id_for_user(
        db_session,
        user_id=auth_session.user.id,
    )
    return serialize_my_profile(
        profile,
        account_email=auth_session.user.email,
        avatar_media=avatar_media,
        banner_media=banner_media,
        verified_steam_id=verified_steam_id,
    )


@router.put("/me", response_model=MyProfileResponse)
async def update_my_profile(
    payload: ProfileUpdateRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> MyProfileResponse:
    await lock_profile_owner(db_session, auth_session.user.id)
    profile = await db_session.scalar(
        select(PlayerProfile)
        .where(PlayerProfile.user_id == auth_session.user.id)
        .execution_options(populate_existing=True)
    )
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")
    fields_set = payload.model_fields_set
    if "display_name" in fields_set and payload.display_name is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="display_name cannot be cleared.",
        )
    if "display_name" in fields_set:
        profile.display_name = payload.display_name.strip()
        auth_session.user.display_name = profile.display_name
    if "handle" in fields_set:
        profile.handle = payload.handle.strip() if payload.handle else None
    if "bio" in fields_set:
        profile.bio = payload.bio.strip() if payload.bio else None
    if "contact_email" in fields_set:
        profile.contact_email = payload.contact_email.strip() if payload.contact_email else None
    if "region" in fields_set:
        profile.region = payload.region.strip() if payload.region else None
    if "discord_account" in fields_set:
        profile.discord_account = payload.discord_account.strip() if payload.discord_account else None
    if "captain_team_name" in fields_set:
        profile.captain_team_name = payload.captain_team_name.strip() if payload.captain_team_name else None
    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="profile.update",
        subject_type="profile",
        subject_id=profile.user_id,
        payload=payload.model_dump(exclude_unset=True),
    )
    try:
        await db_session.commit()
        invalidate_user_session_cache(auth_session.user.id)
    except IntegrityError as exc:
        await db_session.rollback()
        if "uq_player_profiles_handle" in str(exc.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Handle is already in use.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Profile update conflicts with existing data.",
        ) from exc
    await _refresh_profile_read_model_after_commit(auth_session.user.id)
    await db_session.refresh(profile)
    avatar_media, banner_media = await profile_media_descriptors(db_session, profile)
    verified_steam_id = await verified_steam_id_for_user(
        db_session,
        user_id=auth_session.user.id,
    )
    return serialize_my_profile(
        profile,
        account_email=auth_session.user.email,
        avatar_media=avatar_media,
        banner_media=banner_media,
        verified_steam_id=verified_steam_id,
    )


@router.get("/public/{handle}", response_model=PublicProfileResponse)
async def get_public_profile(
    handle: str = ApiPath(min_length=2, max_length=40),
    db_session: AsyncSession = Depends(get_db_session),
) -> PublicProfileResponse:
    row = (
        await db_session.execute(
            select(PlayerProfile, DeadlockProfile)
            .outerjoin(DeadlockProfile, DeadlockProfile.user_id == PlayerProfile.user_id)
            .where(PlayerProfile.handle == handle)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    profile, deadlock_profile = row
    avatar_media, banner_media = await profile_media_descriptors(db_session, profile)
    return PublicProfileResponse(
        user_id=profile.user_id,
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
        # Email is account/private data and is never exposed by public profiles.
        contact_email=None,
        region=profile.region,
        # Auth identities are private account credentials. Public exposure can
        # be added later only as an explicit profile privacy choice.
        steam_id=None,
        steam_linked=False,
        discord_account=profile.discord_account,
        captain_team_name=profile.captain_team_name,
        deadlock_profile=(
            DeadlockProfileResponse.model_validate(deadlock_profile)
            if deadlock_profile is not None
            else None
        ),
    )


async def accept_profile_media_upload(
    *,
    request: Request,
    file: UploadFile,
    purpose: str,
    audit_action: str,
    auth_session,
    db_session: AsyncSession,
) -> MediaAcceptedResponse:
    profile = await db_session.scalar(
        select(PlayerProfile).where(PlayerProfile.user_id == auth_session.user.id)
    )
    try:
        await check_media_upload_rate_limit(
            request,
            user_id=auth_session.user.id,
            upload_bytes=upload_size_hint(request, file),
        )
        service = api_media_service(db_session)

        async def audit_acceptance(
            staged: StagedSource,
            superseded_asset_ids: tuple[str, ...],
        ) -> None:
            await write_audit_log(
                db_session,
                actor_user_id=auth_session.user.id,
                action=audit_action,
                subject_type="profile",
                subject_id=profile.user_id,
                payload={
                    "asset_id": staged.asset_id,
                    "content_type": staged.mime_type,
                    "size": staged.byte_size,
                    "superseded_asset_ids": list(superseded_asset_ids),
                },
            )

        accepted = await service.accept_upload(
            chunks=upload_file_chunks(file),
            declared_mime=file.content_type,
            purpose=purpose,
            owner_user_id=auth_session.user.id,
            enqueue=enqueue_media_asset,
            before_commit=audit_acceptance,
        )
    except MediaError as exc:
        raise_media_http_error(exc)
    finally:
        await file.close()
    return accepted_media_response(accepted)


@router.post(
    "/me/avatar",
    response_model=MediaAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_my_profile_avatar(
    request: Request,
    file: UploadFile = File(...),
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> MediaAcceptedResponse:
    return await accept_profile_media_upload(
        request=request,
        file=file,
        purpose="profile_avatar",
        audit_action="profile.avatar.upload.accepted",
        auth_session=auth_session,
        db_session=db_session,
    )


@router.post(
    "/me/banner",
    response_model=MediaAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_my_profile_banner(
    request: Request,
    file: UploadFile = File(...),
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> MediaAcceptedResponse:
    return await accept_profile_media_upload(
        request=request,
        file=file,
        purpose="profile_banner",
        audit_action="profile.banner.upload.accepted",
        auth_session=auth_session,
        db_session=db_session,
    )


async def delete_profile_media(
    *,
    purpose: str,
    audit_action: str,
    auth_session,
    db_session: AsyncSession,
) -> MediaDeleteAcceptedResponse:
    service = api_media_service(db_session)

    async def audit_unlink(asset_id: str | None) -> None:
        await write_audit_log(
            db_session,
            actor_user_id=auth_session.user.id,
            action=audit_action,
            subject_type="profile",
            subject_id=auth_session.user.id,
            payload={"asset_id": asset_id},
        )

    asset_id = await service.unlink_active(
        purpose=purpose,
        owner_id=auth_session.user.id,
        before_commit=audit_unlink,
    )
    await _refresh_profile_read_model_after_commit(auth_session.user.id)
    invalidate_user_session_cache(auth_session.user.id)
    return MediaDeleteAcceptedResponse(
        asset_id=asset_id,
        status="cleanup_pending" if asset_id else "deleted",
    )


@router.delete(
    "/me/avatar",
    response_model=MediaDeleteAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_my_profile_avatar(
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> MediaDeleteAcceptedResponse:
    return await delete_profile_media(
        purpose="profile_avatar",
        audit_action="profile.avatar.delete.accepted",
        auth_session=auth_session,
        db_session=db_session,
    )


@router.delete(
    "/me/banner",
    response_model=MediaDeleteAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_my_profile_banner(
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> MediaDeleteAcceptedResponse:
    return await delete_profile_media(
        purpose="profile_banner",
        audit_action="profile.banner.delete.accepted",
        auth_session=auth_session,
        db_session=db_session,
    )


@router.get("/me/deadlock", response_model=DeadlockProfileResponse)
async def get_my_deadlock_profile(
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> DeadlockProfileResponse:
    profile = await db_session.scalar(
        select(DeadlockProfile).where(DeadlockProfile.user_id == auth_session.user.id)
    )
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deadlock profile not found.")
    return DeadlockProfileResponse.model_validate(profile)


@router.put("/me/deadlock", response_model=DeadlockProfileResponse)
async def upsert_my_deadlock_profile(
    payload: DeadlockProfileUpdateRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> DeadlockProfileResponse:
    normalized_roles = normalize_roles(payload.roles)
    normalized_pool = normalize_pool(payload.pool)
    supported_heroes = await get_supported_deadlock_hero_names()
    if any(hero not in supported_heroes for hero in normalized_pool):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unknown hero in Deadlock profile pool.",
        )

    registration_payload = RegistrationPayload(
        rank=payload.rank.strip(),
        subrank=payload.subrank,
        playtime=payload.playtime,
        roles=normalized_roles,
        pool=normalized_pool,
        captain_priority=payload.captain_priority,
    )
    try:
        validate_registration_payload(registration_payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    await lock_profile_owner(db_session, auth_session.user.id)
    profile = await db_session.scalar(
        select(DeadlockProfile)
        .where(DeadlockProfile.user_id == auth_session.user.id)
        .execution_options(populate_existing=True)
    )
    if profile is None:
        profile = DeadlockProfile(
            user_id=auth_session.user.id,
            rank=registration_payload.rank,
            subrank=registration_payload.subrank,
            playtime=registration_payload.playtime,
            roles=list(registration_payload.roles),
            pool=list(registration_payload.pool or []),
            captain_priority=registration_payload.captain_priority,
        )
        db_session.add(profile)
    else:
        profile.rank = registration_payload.rank
        profile.subrank = registration_payload.subrank
        profile.playtime = registration_payload.playtime
        profile.roles = list(registration_payload.roles)
        profile.pool = list(registration_payload.pool or [])
        profile.captain_priority = registration_payload.captain_priority

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="profile.deadlock.upsert",
        subject_type="deadlock_profile",
        subject_id=auth_session.user.id,
        payload={
            "rank": registration_payload.rank,
            "subrank": registration_payload.subrank,
            "playtime": registration_payload.playtime,
            "roles": list(registration_payload.roles),
            "pool_size": len(registration_payload.pool or []),
            "captain_priority": registration_payload.captain_priority,
        },
    )
    await db_session.commit()
    await _refresh_profile_read_model_after_commit(auth_session.user.id)
    await db_session.refresh(profile)
    return DeadlockProfileResponse.model_validate(profile)


@router.get("/me/deadlock/dream-slots", response_model=list[DeadlockDreamSlotResponse])
async def get_my_deadlock_dream_slots(
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[DeadlockDreamSlotResponse]:
    return await list_profile_dream_slots(db_session, user_id=auth_session.user.id)


@router.put("/me/deadlock/dream-slots", response_model=list[DeadlockDreamSlotResponse])
async def upsert_my_deadlock_dream_slots(
    payload: DeadlockDreamSlotsBulkUpdateRequest,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[DeadlockDreamSlotResponse]:
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
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        normalized_by_slot[slot.slot_number] = {
            "allowed_roles": list(normalized["allowed_roles"]),
            "desired_heroes": list(normalized["desired_heroes"]),
        }

    # Serialize replace-all slot writes from the profile and captain workspace
    # endpoints on the stable parent row.  Locking only existing slot rows
    # permits concurrent writes to an empty slot set to merge unintentionally.
    await lock_profile_owner(db_session, auth_session.user.id)
    await db_session.execute(
        delete(DeadlockDreamSlot).where(DeadlockDreamSlot.user_id == auth_session.user.id)
    )
    for slot_number in range(1, 7):
        slot = normalized_by_slot.get(slot_number, {"allowed_roles": [], "desired_heroes": []})
        if not slot["allowed_roles"] and not slot["desired_heroes"]:
            continue
        db_session.add(
            DeadlockDreamSlot(
                user_id=auth_session.user.id,
                slot_number=slot_number,
                allowed_roles=list(slot["allowed_roles"]),
                desired_heroes=list(slot["desired_heroes"]),
            )
        )

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="profile.deadlock.dream_slots.upsert",
        subject_type="deadlock_dream_slots",
        subject_id=auth_session.user.id,
        payload={
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
    return await list_profile_dream_slots(db_session, user_id=auth_session.user.id)
