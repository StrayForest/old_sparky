from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.routes import auth as auth_routes
from apps.platform_api.app.api.schemas import RegisterRequest, RegistrationResponse
from apps.platform_api.app.services.current_user import serialize_current_user
from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.auth_lifecycle import (
    consume_user_email_verification_tokens,
    consume_user_password_reset_tokens,
    email_delivery_configured,
    email_verification_required,
    issue_email_verification_token,
)
from python_packages.platform_infra.csrf import issue_csrf_token
from python_packages.platform_infra.db import get_db_session
from python_packages.platform_infra.models import (
    PasswordCredential,
    PlayerProfile,
    Role,
    User,
    UserRole,
)
from python_packages.platform_infra.security import (
    clear_session_cookies,
    create_user_session,
    hash_password,
    issue_auth_flow_cookie,
    public_registration_enabled,
    set_session_cookie,
)

router = APIRouter()


@router.post("/register", response_model=RegistrationResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    db_session: AsyncSession = Depends(get_db_session),
) -> RegistrationResponse:
    # Keep registration wired through the established auth module for shared
    # settings, protection hooks and mail delivery semantics.
    settings = auth_routes.get_settings()
    if not public_registration_enabled(settings):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Public registration is currently closed.",
        )

    verification_required = email_verification_required(settings)
    if verification_required and not email_delivery_configured(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email delivery is temporarily unavailable.",
        )

    normalized_email = payload.email.lower()
    display_name = payload.display_name.strip()
    rate_limit_state = await auth_routes.check_registration_rate_limit(
        request,
        settings=settings,
    )
    await auth_routes.verify_turnstile_token(
        payload.turnstile_token,
        expected_action="register",
        remote_ip=request.client.host if request.client else None,
        adaptive_required=rate_limit_state.adaptive_turnstile_required,
        settings=settings,
    )

    existing_user = await db_session.scalar(
        select(User).where(User.email == normalized_email).with_for_update()
    )
    if existing_user is not None and (
        not verification_required
        or existing_user.status != "pending_verification"
        or existing_user.email_verified_at is not None
    ):
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email is already registered.",
        )

    issued_verification = None
    auth_session = None

    if existing_user is not None:
        # Password replacement is allowed only together with a fresh delivery
        # slot and fresh verification code. This prevents an old code from
        # activating credentials written by a later registration attempt.
        await auth_routes.reserve_auth_delivery_cooldown(
            normalized_email,
            scope="email-verification",
            settings=settings,
        )

        user = existing_user
        user.display_name = display_name
        user.status = "pending_verification"
        user.email_verified_at = None

        credential = await db_session.scalar(
            select(PasswordCredential)
            .where(PasswordCredential.user_id == user.id)
            .with_for_update()
        )
        if credential is None:
            credential = PasswordCredential(
                user_id=user.id,
                password_hash=hash_password(payload.password),
                password_version="argon2id",
            )
            db_session.add(credential)
        else:
            credential.password_hash = hash_password(payload.password)
            credential.password_version = "argon2id"

        profile = await db_session.scalar(
            select(PlayerProfile)
            .where(PlayerProfile.user_id == user.id)
            .with_for_update()
        )
        if profile is None:
            db_session.add(
                PlayerProfile(
                    user_id=user.id,
                    display_name=display_name,
                    contact_email=normalized_email,
                )
            )
        else:
            profile.display_name = display_name
            profile.contact_email = normalized_email

        invalidated_verification_tokens = await consume_user_email_verification_tokens(
            db_session,
            user_id=user.id,
        )
        invalidated_password_reset_tokens = await consume_user_password_reset_tokens(
            db_session,
            user_id=user.id,
        )
        issued_verification = await issue_email_verification_token(
            db_session,
            user_id=user.id,
            secret_key=settings.platform_secret_key,
            ttl_minutes=settings.platform_email_verification_ttl_minutes,
        )
        await write_audit_log(
            db_session,
            actor_user_id=None,
            action="auth.register.restart",
            subject_type="user",
            subject_id=user.id,
            payload={
                "email": user.email,
                "verification_required": True,
                "email_verification_tokens_invalidated": invalidated_verification_tokens,
                "password_reset_tokens_invalidated": invalidated_password_reset_tokens,
            },
        )
    else:
        user = User(
            email=normalized_email,
            display_name=display_name,
            status="pending_verification" if verification_required else "active",
            email_verified_at=None if verification_required else datetime.now(UTC),
            public_tournament_credits=0,
            private_tournament_credits=0,
        )
        db_session.add(user)
        try:
            await db_session.flush()
        except IntegrityError as exc:
            await db_session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email is already registered.",
            ) from exc

        db_session.add(
            PasswordCredential(
                user_id=user.id,
                password_hash=hash_password(payload.password),
                password_version="argon2id",
            )
        )
        db_session.add(
            PlayerProfile(
                user_id=user.id,
                display_name=user.display_name,
                contact_email=user.email,
            )
        )

        role_slugs = ["authenticated_user", "player"]
        roles = list(
            (
                await db_session.scalars(
                    select(Role).where(Role.slug.in_(role_slugs))
                )
            ).all()
        )
        if {role.slug for role in roles} != set(role_slugs):
            await db_session.rollback()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Registration is temporarily unavailable.",
            )
        for role in roles:
            db_session.add(UserRole(user_id=user.id, role_id=role.id))

        if verification_required:
            await auth_routes.reserve_auth_delivery_cooldown(
                normalized_email,
                scope="email-verification",
                settings=settings,
            )
            issued_verification = await issue_email_verification_token(
                db_session,
                user_id=user.id,
                secret_key=settings.platform_secret_key,
                ttl_minutes=settings.platform_email_verification_ttl_minutes,
            )
        else:
            auth_session = await create_user_session(
                db_session=db_session,
                user=user,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )

        await write_audit_log(
            db_session,
            actor_user_id=None,
            action="auth.register",
            subject_type="user",
            subject_id=user.id,
            payload={
                "email": user.email,
                "verification_required": verification_required,
            },
        )

    await db_session.commit()
    response.headers["Cache-Control"] = "no-store"

    if issued_verification is not None:
        background_tasks.add_task(
            auth_routes._deliver_email_verification,
            settings,
            recipient_email=user.email,
            code=issued_verification.code,
        )

    if auth_session is None:
        clear_session_cookies(response)
        issue_auth_flow_cookie(
            response,
            purpose="verification",
            account_key=normalized_email,
            settings=settings,
        )
    else:
        set_session_cookie(response, auth_session.token)
        issue_csrf_token(response, auth_session.token, settings)

    return RegistrationResponse(
        user=await serialize_current_user(db_session, user),
        expires_at=auth_session.session.expires_at if auth_session is not None else None,
        verification_required=verification_required,
        retry_after_seconds=(
            settings.platform_auth_delivery_cooldown_seconds
            if verification_required
            else None
        ),
    )
