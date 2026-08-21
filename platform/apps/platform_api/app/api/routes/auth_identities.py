from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import UserResponse
from apps.platform_api.app.services.current_user import serialize_current_user
from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.models import ExternalIdentity, PasswordCredential, User
from python_packages.platform_infra.security import (
    get_authenticated_session,
    invalidate_user_session_cache,
)

router = APIRouter()


@router.delete("/identities/steam", response_model=UserResponse)
async def unlink_steam_identity(
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    user = await db_session.scalar(
        select(User)
        .where(User.id == auth_session.user.id)
        .with_for_update()
    )
    if user is None or user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    identity = await db_session.scalar(
        select(ExternalIdentity)
        .where(
            ExternalIdentity.user_id == user.id,
            ExternalIdentity.provider == "steam",
        )
        .with_for_update()
    )
    if identity is None:
        return await serialize_current_user(
            db_session,
            user,
            role_slugs=auth_session.role_slugs,
        )

    password_credential = await db_session.scalar(
        select(PasswordCredential.user_id).where(PasswordCredential.user_id == user.id)
    )
    has_password_login = bool(
        user.email
        and user.email_verified_at is not None
        and password_credential is not None
    )
    if not has_password_login:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Сначала привяжите подтвержденную почту и установите пароль.",
        )

    steam_subject = identity.subject
    await db_session.delete(identity)
    await write_audit_log(
        db_session,
        actor_user_id=user.id,
        action="auth.steam.unlink",
        subject_type="user",
        subject_id=user.id,
        payload={"provider": "steam", "subject": steam_subject},
    )
    await db_session.commit()
    invalidate_user_session_cache(user.id)

    return await serialize_current_user(
        db_session,
        user,
        role_slugs=auth_session.role_slugs,
    )
