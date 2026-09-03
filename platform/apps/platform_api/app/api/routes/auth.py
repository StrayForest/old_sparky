from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.platform_api.app.api.schemas import (
    AccountSecurityUpdateRequest,
    AuthActionAcceptedResponse,
    AuthBootstrapResponse,
    AuthSecurityConfigResponse,
    AuthSessionResponse,
    AuthSessionListItemResponse,
    CsrfTokenResponse,
    EmailVerificationConfirmRequest,
    EmailVerificationResendRequest,
    EmailChangeRequest,
    EmailLinkConfirmRequest,
    EmailLinkRequest,
    LoginRequest,
    PasswordResetCodeVerifyRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    SteamAuthStartRequest,
    SteamAuthStartResponse,
    UserResponse,
)
from apps.platform_api.app.services.auth_mail import (
    send_email_verification_email,
    send_password_reset_email,
)
from apps.platform_api.app.services.current_user import serialize_current_user
from apps.platform_api.app.services.auth_bootstrap import build_auth_bootstrap
from apps.platform_api.app.services.user_account_read_models import (
    delete_user_account_read_model,
)
from apps.platform_api.app.services.steam_openid import (
    SteamOpenIDError,
    SteamOpenIDVerificationError,
    build_openid_authorization_url,
    build_steam_callback_return_to,
    digest_flow_secret,
    flow_secret_matches,
    new_flow_secret,
    normalize_return_path,
    verify_openid_assertion,
)
from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.auth_lifecycle import (
    consume_email_verification_token,
    consume_email_link_intent,
    consume_password_reset_token,
    consume_user_email_verification_tokens,
    consume_user_password_reset_tokens,
    email_delivery_configured,
    email_verification_required,
    issue_email_verification_token,
    issue_email_link_intent,
    issue_password_reset_token,
    validate_password_reset_code,
)
from python_packages.platform_infra.auth_rate_limit import (
    check_email_verification_confirm_rate_limit,
    check_email_verification_resend_rate_limit,
    check_email_link_rate_limit,
    check_login_rate_limit,
    check_password_reset_rate_limit,
    check_steam_auth_rate_limit,
    clear_login_failures,
    record_login_failure,
    reserve_auth_delivery_cooldown,
)
from python_packages.platform_infra.config import PlatformSettings, get_settings
from python_packages.platform_infra.csrf import issue_csrf_token
from python_packages.platform_infra.db import get_db_session, release_db_connection
from python_packages.platform_infra.models import (
    PasswordCredential,
    ExternalIdentity,
    PlayerProfile,
    Role,
    SteamAuthFlow,
    User,
    UserRole,
    UserSession,
)
from python_packages.platform_infra.security import (
    auth_flow_cookie_name,
    clear_auth_flow_cookie,
    clear_session_cookies,
    create_user_session,
    get_authenticated_session,
    get_authenticated_session_for_auth_bootstrap,
    hash_password,
    has_valid_auth_flow_cookie,
    invalidate_session_cache,
    invalidate_user_sessions,
    invalidate_user_session_cache,
    issue_auth_flow_cookie,
    public_registration_enabled,
    set_session_cookie,
    session_token_digest,
    verify_login_password,
    verify_password,
)
from python_packages.platform_infra.turnstile import (
    normalized_turnstile_mode,
    verify_turnstile_token,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/bootstrap", response_model=AuthBootstrapResponse)
async def get_auth_bootstrap(
    auth_session=Depends(get_authenticated_session_for_auth_bootstrap),
    db_session: AsyncSession = Depends(get_db_session),
) -> AuthBootstrapResponse:
    """Return the small identity snapshot used by every authenticated SSR shell."""

    # A profile read-model miss owns its own short-lived DB session. Release
    # the authoritative auth session first so a cache fill cannot pin two
    # connections for the duration of one request.
    try:
        await release_db_connection(db_session)
        return await build_auth_bootstrap(auth_session)
    finally:
        await release_db_connection(db_session)


async def _deliver_password_reset_email(
    settings: PlatformSettings,
    *,
    recipient_email: str,
    code: str,
) -> None:
    try:
        await send_password_reset_email(
            settings,
            recipient_email=recipient_email,
            code=code,
        )
    except Exception as exc:
        logger.warning("Password reset delivery failed: %s", type(exc).__name__)


async def _deliver_email_verification(
    settings: PlatformSettings,
    *,
    recipient_email: str,
    code: str,
) -> None:
    try:
        await send_email_verification_email(
            settings,
            recipient_email=recipient_email,
            code=code,
        )
    except Exception as exc:
        logger.warning("Email verification delivery failed: %s", type(exc).__name__)


async def _wait_for_generic_auth_response(
    started_at: float,
    settings: PlatformSettings,
) -> None:
    remaining = (
        settings.platform_auth_generic_response_min_seconds
        - (time.monotonic() - started_at)
    )
    if remaining > 0:
        await asyncio.sleep(remaining)


def _steam_callback_url(request: Request, settings: PlatformSettings) -> str:
    configured = (settings.platform_steam_callback_url or "").strip()
    if configured:
        return configured
    return str(request.url_for("steam_callback"))


def _web_auth_redirect(
    settings: PlatformSettings,
    return_path: str,
    *,
    result: str,
) -> RedirectResponse:
    normalized_path = normalize_return_path(return_path)
    parsed = urlsplit(normalized_path)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("steam_auth", result))
    destination = (
        settings.platform_web_origin.rstrip("/")
        + urlunsplit(("", "", parsed.path, urlencode(query), ""))
    )
    return RedirectResponse(
        destination,
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/security-config", response_model=AuthSecurityConfigResponse)
async def auth_security_config(response: Response) -> AuthSecurityConfigResponse:
    settings = get_settings()
    response.headers["Cache-Control"] = "no-store"
    return AuthSecurityConfigResponse(
        public_registration_enabled=public_registration_enabled(settings),
        email_verification_required=email_verification_required(settings),
        turnstile_mode=normalized_turnstile_mode(settings),
        turnstile_site_key=(settings.platform_turnstile_site_key or "").strip() or None,
        steam_login_enabled=settings.platform_steam_login_enabled,
    )


@router.post("/login", response_model=AuthSessionResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db_session: AsyncSession = Depends(get_db_session),
) -> AuthSessionResponse:
    settings = get_settings()
    normalized_email = payload.email.lower()
    rate_limit_state = await check_login_rate_limit(
        request,
        normalized_email,
        settings=settings,
    )
    await verify_turnstile_token(
        payload.turnstile_token,
        expected_action="login",
        remote_ip=request.client.host if request.client else None,
        adaptive_required=rate_limit_state.adaptive_turnstile_required,
        settings=settings,
    )
    user = await db_session.scalar(
        select(User).where(User.email == normalized_email).with_for_update()
    )
    credential = (
        await db_session.scalar(
            select(PasswordCredential)
            .where(PasswordCredential.user_id == user.id)
            .with_for_update()
        )
        if user is not None
        else None
    )
    password_is_valid = verify_login_password(
        payload.password,
        credential.password_hash if credential is not None else None,
    )
    if (
        user is None
        or credential is None
        or not password_is_valid
        or user.status != "active"
        or (
            email_verification_required(settings)
            and user.email_verified_at is None
        )
    ):
        await db_session.rollback()
        await record_login_failure(request, normalized_email, settings=settings)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials.")

    await clear_login_failures(request, normalized_email, settings=settings)

    auth_session = await create_user_session(
        db_session=db_session,
        user=user,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await write_audit_log(
        db_session,
        actor_user_id=user.id,
        action="auth.login",
        subject_type="user",
        subject_id=user.id,
        payload={"email": user.email},
    )
    await db_session.commit()
    response.headers["Cache-Control"] = "no-store"
    set_session_cookie(response, auth_session.token)
    issue_csrf_token(response, auth_session.token, settings)
    return AuthSessionResponse(
        user=await serialize_current_user(db_session, user),
        expires_at=auth_session.session.expires_at,
    )


async def _start_steam_auth(
    *,
    purpose: str,
    payload: SteamAuthStartRequest,
    request: Request,
    response: Response,
    db_session: AsyncSession,
    auth_session=None,
) -> SteamAuthStartResponse:
    settings = get_settings()
    if not settings.platform_steam_login_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Steam authentication is currently unavailable.",
        )
    if purpose == "login":
        rate_limit_state = await check_steam_auth_rate_limit(
            request,
            "public",
            operation="login",
            settings=settings,
        )
        await verify_turnstile_token(
            payload.turnstile_token,
            expected_action="steam_login",
            remote_ip=request.client.host if request.client else None,
            adaptive_required=rate_limit_state.adaptive_turnstile_required,
            settings=settings,
        )
    elif purpose == "link" and auth_session is not None:
        await check_steam_auth_rate_limit(
            request,
            auth_session.user.id,
            operation="link",
            settings=settings,
        )
    else:
        raise ValueError("Steam authentication flow purpose is invalid.")

    state = new_flow_secret()
    browser_grant = issue_auth_flow_cookie(
        response,
        purpose="steam",
        account_key=state,
        settings=settings,
    )
    expires_at = datetime.now(UTC) + timedelta(
        minutes=settings.platform_auth_flow_ttl_minutes
    )
    db_session.add(
        SteamAuthFlow(
            state_digest=digest_flow_secret(
                state,
                settings.platform_secret_key,
                purpose="steam-state",
            ),
            browser_grant_digest=digest_flow_secret(
                browser_grant,
                settings.platform_secret_key,
                purpose="steam-browser-grant",
            ),
            purpose=purpose,
            user_id=auth_session.user.id if auth_session is not None else None,
            session_id=auth_session.session.id if auth_session is not None else None,
            return_path=normalize_return_path(payload.return_to),
            expires_at=expires_at,
        )
    )
    await db_session.commit()
    response.headers["Cache-Control"] = "no-store"
    return SteamAuthStartResponse(
        authorization_url=build_openid_authorization_url(
            _steam_callback_url(request, settings),
            state,
        ),
        expires_at=expires_at,
    )


@router.post("/steam/login/start", response_model=SteamAuthStartResponse)
async def start_steam_login(
    payload: SteamAuthStartRequest,
    request: Request,
    response: Response,
    db_session: AsyncSession = Depends(get_db_session),
) -> SteamAuthStartResponse:
    return await _start_steam_auth(
        purpose="login",
        payload=payload,
        request=request,
        response=response,
        db_session=db_session,
    )


@router.post("/steam/link/start", response_model=SteamAuthStartResponse)
async def start_steam_link(
    payload: SteamAuthStartRequest,
    request: Request,
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> SteamAuthStartResponse:
    return await _start_steam_auth(
        purpose="link",
        payload=payload,
        request=request,
        response=response,
        db_session=db_session,
        auth_session=auth_session,
    )


@router.get("/steam/callback", name="steam_callback")
async def steam_callback(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    settings = get_settings()
    state_values = request.query_params.getlist("state")
    if len(state_values) != 1 or not state_values[0] or len(state_values[0]) > 512:
        redirect = _web_auth_redirect(settings, "/auth/login", result="error")
        clear_auth_flow_cookie(redirect, purpose="steam", settings=settings)
        return redirect
    state = state_values[0]
    flow = await db_session.scalar(
        select(SteamAuthFlow)
        .where(
            SteamAuthFlow.state_digest
            == digest_flow_secret(
                state,
                settings.platform_secret_key,
                purpose="steam-state",
            )
        )
        .with_for_update()
    )
    now = datetime.now(UTC)
    browser_grant = request.cookies.get(auth_flow_cookie_name("steam", settings), "")
    if (
        flow is None
        or flow.consumed_at is not None
        or flow.expires_at <= now
        or not has_valid_auth_flow_cookie(
            request,
            purpose="steam",
            account_key=state,
            settings=settings,
        )
        or not flow_secret_matches(
            flow.browser_grant_digest,
            browser_grant,
            settings.platform_secret_key,
            purpose="steam-browser-grant",
        )
    ):
        return_path = flow.return_path if flow is not None else "/auth/login"
        if flow is not None and flow.consumed_at is None:
            flow.consumed_at = now
            await db_session.commit()
        else:
            await db_session.rollback()
        redirect = _web_auth_redirect(settings, return_path, result="error")
        clear_auth_flow_cookie(redirect, purpose="steam", settings=settings)
        return redirect
    try:
        await check_steam_auth_rate_limit(
            request,
            state,
            operation="callback",
            settings=settings,
        )
    except HTTPException:
        flow.consumed_at = now
        await db_session.commit()
        redirect = _web_auth_redirect(settings, flow.return_path, result="error")
        clear_auth_flow_cookie(redirect, purpose="steam", settings=settings)
        return redirect
    expected_return_to = build_steam_callback_return_to(
        _steam_callback_url(request, settings),
        state,
    )
    flow_purpose = flow.purpose
    flow_user_id = flow.user_id
    flow_session_id = flow.session_id
    flow_return_path = flow.return_path
    # Consume and release the DB row before the bounded provider network call.
    # A transient Steam failure requires a fresh start, but cannot tie up a DB
    # connection or allow a concurrent replay of the same assertion flow.
    flow.consumed_at = now
    await db_session.commit()
    try:
        steam_id = await verify_openid_assertion(
            request.query_params,
            expected_return_to,
            settings,
        )
    except SteamOpenIDVerificationError as exc:
        logger.warning("Steam OpenID verification unavailable: %s", type(exc).__name__)
        redirect = _web_auth_redirect(settings, flow_return_path, result="error")
        clear_auth_flow_cookie(redirect, purpose="steam", settings=settings)
        return redirect
    except SteamOpenIDError as exc:
        logger.info("Steam OpenID assertion rejected: %s", type(exc).__name__)
        redirect = _web_auth_redirect(settings, flow_return_path, result="error")
        clear_auth_flow_cookie(redirect, purpose="steam", settings=settings)
        return redirect

    identity = await db_session.scalar(
        select(ExternalIdentity)
        .where(
            ExternalIdentity.provider == "steam",
            ExternalIdentity.subject == steam_id,
        )
        .with_for_update()
    )
    created_user = False
    action = "auth.steam.login"
    if flow_purpose == "link":
        session_token = request.cookies.get(settings.platform_session_cookie_name, "")
        bound_session = await db_session.scalar(
            select(UserSession)
            .where(
                UserSession.id == flow_session_id,
                UserSession.user_id == flow_user_id,
                UserSession.token_digest == session_token_digest(session_token),
                UserSession.invalidated_at.is_(None),
                UserSession.expires_at > now,
            )
            .with_for_update()
        )
        user = await db_session.scalar(
            select(User)
            .where(User.id == flow_user_id, User.status == "active")
            .with_for_update()
        )
        current_identity = await db_session.scalar(
            select(ExternalIdentity)
            .where(
                ExternalIdentity.user_id == flow_user_id,
                ExternalIdentity.provider == "steam",
            )
            .with_for_update()
        )
        if (
            bound_session is None
            or user is None
            or (identity is not None and identity.user_id != flow_user_id)
            or (
                current_identity is not None
                and current_identity.subject != steam_id
            )
        ):
            await write_audit_log(
                db_session,
                actor_user_id=flow_user_id,
                action="auth.steam.link.rejected",
                subject_type="user",
                subject_id=flow_user_id,
                payload={"reason": "identity_conflict_or_session_invalid"},
            )
            await db_session.commit()
            redirect = _web_auth_redirect(
                settings,
                flow_return_path,
                result="error",
            )
            clear_auth_flow_cookie(
                redirect,
                purpose="steam",
                settings=settings,
            )
            return redirect
        if current_identity is None:
            identity = ExternalIdentity(
                user_id=user.id,
                provider="steam",
                subject=steam_id,
                linked_at=now,
                last_authenticated_at=now,
            )
            db_session.add(identity)
        else:
            identity = current_identity
            identity.last_authenticated_at = now
        action = "auth.steam.link"
        invalidated_sessions = await invalidate_user_sessions(
            db_session,
            user_id=user.id,
            now=now,
        )
    elif flow_purpose == "login":
        if identity is not None:
            user = await db_session.scalar(
                select(User).where(User.id == identity.user_id).with_for_update()
            )
            if user is None or user.status != "active":
                await db_session.commit()
                redirect = _web_auth_redirect(
                    settings,
                    flow_return_path,
                    result="error",
                )
                clear_auth_flow_cookie(
                    redirect,
                    purpose="steam",
                    settings=settings,
                )
                return redirect
            identity.last_authenticated_at = now
        else:
            user = User(
                email=None,
                display_name=f"Steam {steam_id[-6:]}",
                status="active",
                email_verified_at=None,
                public_tournament_credits=0,
                private_tournament_credits=0,
            )
            db_session.add(user)
            await db_session.flush()
            db_session.add(
                PlayerProfile(
                    user_id=user.id,
                    display_name=user.display_name,
                    contact_email=None,
                )
            )
            roles = list(
                (
                    await db_session.scalars(
                        select(Role).where(
                            Role.slug.in_(["authenticated_user", "player"])
                        )
                    )
                ).all()
            )
            if {role.slug for role in roles} != {"authenticated_user", "player"}:
                await db_session.rollback()
                redirect = _web_auth_redirect(
                    settings,
                    flow_return_path,
                    result="error",
                )
                clear_auth_flow_cookie(redirect, purpose="steam", settings=settings)
                return redirect
            for role in roles:
                db_session.add(UserRole(user_id=user.id, role_id=role.id))
            identity = ExternalIdentity(
                user_id=user.id,
                provider="steam",
                subject=steam_id,
                linked_at=now,
                last_authenticated_at=now,
            )
            db_session.add(identity)
            created_user = True
            action = "auth.steam.register"
        invalidated_sessions = 0
    else:
        await db_session.rollback()
        redirect = _web_auth_redirect(settings, flow_return_path, result="error")
        clear_auth_flow_cookie(redirect, purpose="steam", settings=settings)
        return redirect

    auth_session = await create_user_session(
        db_session=db_session,
        user=user,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await write_audit_log(
        db_session,
        actor_user_id=user.id,
        action=action,
        subject_type="user",
        subject_id=user.id,
        payload={
            "provider": "steam",
            "created_user": created_user,
            "sessions_invalidated": invalidated_sessions,
            "session_rotated": flow_purpose == "link",
        },
    )
    try:
        await db_session.commit()
    except IntegrityError as exc:
        await db_session.rollback()
        logger.info("Steam identity completion conflicted: %s", type(exc).__name__)
        redirect = _web_auth_redirect(settings, flow_return_path, result="error")
        clear_auth_flow_cookie(redirect, purpose="steam", settings=settings)
        return redirect
    invalidate_user_session_cache(user.id)
    await delete_user_account_read_model(user.id)
    redirect = _web_auth_redirect(settings, flow_return_path, result="success")
    clear_auth_flow_cookie(redirect, purpose="steam", settings=settings)
    set_session_cookie(redirect, auth_session.token)
    issue_csrf_token(redirect, auth_session.token, settings)
    return redirect


@router.post(
    "/password-reset/request",
    response_model=AuthActionAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    db_session: AsyncSession = Depends(get_db_session),
) -> AuthActionAcceptedResponse:
    settings = get_settings()
    started_at = time.monotonic()
    if not email_delivery_configured(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email delivery is temporarily unavailable.",
        )
    normalized_email = payload.email.lower()
    rate_limit_state = await check_password_reset_rate_limit(
        request,
        normalized_email,
        operation="request",
        settings=settings,
    )
    has_flow_grant = has_valid_auth_flow_cookie(
        request,
        purpose="password-reset",
        account_key=normalized_email,
        settings=settings,
    )
    if not has_flow_grant:
        await verify_turnstile_token(
            payload.turnstile_token,
            expected_action="reset_request",
            remote_ip=request.client.host if request.client else None,
            adaptive_required=rate_limit_state.adaptive_turnstile_required,
            settings=settings,
        )
    await reserve_auth_delivery_cooldown(
        normalized_email,
        scope="password-reset",
        settings=settings,
    )
    user = await db_session.scalar(
        select(User).where(User.email == normalized_email).with_for_update()
    )
    if (
        user is not None
        and user.status in {"active", "pending_verification"}
        and (
            user.status == "pending_verification"
            or not email_verification_required(settings)
            or user.email_verified_at is not None
        )
    ):
        issued = await issue_password_reset_token(
            db_session,
            user_id=user.id,
            secret_key=settings.platform_secret_key,
            ttl_minutes=settings.platform_password_reset_ttl_minutes,
        )
        await write_audit_log(
            db_session,
            actor_user_id=None,
            action="auth.password_reset.request",
            subject_type="user",
            subject_id=user.id,
            payload={"expires_at": issued.expires_at.isoformat()},
        )
        await db_session.commit()
        background_tasks.add_task(
            _deliver_password_reset_email,
            settings,
            recipient_email=user.email,
            code=issued.code,
        )
    else:
        await db_session.rollback()
    response.headers["Cache-Control"] = "no-store"
    issue_auth_flow_cookie(
        response,
        purpose="password-reset",
        account_key=normalized_email,
        settings=settings,
    )
    await _wait_for_generic_auth_response(started_at, settings)
    return AuthActionAcceptedResponse(
        retry_after_seconds=settings.platform_auth_delivery_cooldown_seconds
    )


@router.post(
    "/password-reset/verify-code",
    response_model=AuthActionAcceptedResponse,
)
async def verify_password_reset_code(
    payload: PasswordResetCodeVerifyRequest,
    request: Request,
    response: Response,
    db_session: AsyncSession = Depends(get_db_session),
) -> AuthActionAcceptedResponse:
    settings = get_settings()
    normalized_email = str(payload.email).lower()
    await check_password_reset_rate_limit(
        request,
        normalized_email,
        operation="verify",
        settings=settings,
    )
    user = await db_session.scalar(select(User).where(User.email == normalized_email))
    valid = bool(
        user is not None
        and user.status in {"active", "pending_verification"}
        and await validate_password_reset_code(
            db_session,
            user_id=user.id,
            code=payload.code,
            secret_key=settings.platform_secret_key,
        )
    )
    await db_session.rollback()
    response.headers["Cache-Control"] = "no-store"
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password reset code is invalid or expired.",
        )
    return AuthActionAcceptedResponse()


@router.post(
    "/password-reset/confirm",
    response_model=AuthSessionResponse,
)
async def confirm_password_reset(
    payload: PasswordResetConfirmRequest,
    request: Request,
    response: Response,
    db_session: AsyncSession = Depends(get_db_session),
) -> AuthSessionResponse:
    settings = get_settings()
    normalized_email = str(payload.email).lower()
    await check_password_reset_rate_limit(
        request,
        normalized_email,
        operation="confirm",
        settings=settings,
    )
    user = await db_session.scalar(
        select(User).where(User.email == normalized_email).with_for_update()
    )
    if user is None or user.status not in {"active", "pending_verification"}:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password reset code is invalid or expired.",
        )
    reset_code_record = await consume_password_reset_token(
        db_session,
        user_id=user.id,
        code=payload.code,
        secret_key=settings.platform_secret_key,
    )
    if reset_code_record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password reset code is invalid or expired.",
        )
    credential = await db_session.scalar(
        select(PasswordCredential)
        .where(PasswordCredential.user_id == reset_code_record.user_id)
        .with_for_update()
    )
    if credential is not None and verify_password(
        payload.new_password,
        credential.password_hash,
    ):
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="New password must differ from the current password.",
        )
    recovered_pending_account = user.status == "pending_verification"
    if credential is None:
        credential = PasswordCredential(
            user_id=user.id,
            password_hash=hash_password(payload.new_password),
            password_version="argon2id",
        )
        db_session.add(credential)
    else:
        credential.password_hash = hash_password(payload.new_password)
        credential.password_version = "argon2id"
    if recovered_pending_account:
        user.status = "active"
        user.email_verified_at = datetime.now(UTC)
    await consume_user_password_reset_tokens(
        db_session,
        user_id=user.id,
    )
    invalidated_verification_tokens = await consume_user_email_verification_tokens(
        db_session,
        user_id=user.id,
    )
    invalidated_sessions = await invalidate_user_sessions(
        db_session,
        user_id=user.id,
    )
    auth_session = await create_user_session(
        db_session=db_session,
        user=user,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await write_audit_log(
        db_session,
        actor_user_id=None,
        action=(
            "auth.pending_account.recover"
            if recovered_pending_account
            else "auth.password_reset.confirm"
        ),
        subject_type="user",
        subject_id=user.id,
        payload={
            "sessions_invalidated": invalidated_sessions,
            "session_rotated": True,
            "pending_account_recovered": recovered_pending_account,
            "email_verification_tokens_invalidated": invalidated_verification_tokens,
        },
    )
    await db_session.commit()
    response.headers["Cache-Control"] = "no-store"
    invalidate_user_session_cache(user.id)
    await delete_user_account_read_model(user.id)
    clear_auth_flow_cookie(
        response,
        purpose="password-reset",
        settings=settings,
    )
    set_session_cookie(response, auth_session.token)
    issue_csrf_token(response, auth_session.token, settings)
    return AuthSessionResponse(
        user=await serialize_current_user(db_session, user),
        expires_at=auth_session.session.expires_at,
    )


@router.post(
    "/email-verification/resend",
    response_model=AuthActionAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resend_email_verification(
    payload: EmailVerificationResendRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    db_session: AsyncSession = Depends(get_db_session),
) -> AuthActionAcceptedResponse:
    settings = get_settings()
    started_at = time.monotonic()
    if not email_delivery_configured(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email delivery is temporarily unavailable.",
        )
    normalized_email = payload.email.lower()
    rate_limit_state = await check_email_verification_resend_rate_limit(
        request,
        normalized_email,
        settings=settings,
    )
    has_flow_grant = has_valid_auth_flow_cookie(
        request,
        purpose="verification",
        account_key=normalized_email,
        settings=settings,
    )
    if not has_flow_grant:
        await verify_turnstile_token(
            payload.turnstile_token,
            expected_action="verification_resend",
            remote_ip=request.client.host if request.client else None,
            adaptive_required=rate_limit_state.adaptive_turnstile_required,
            settings=settings,
        )
    await reserve_auth_delivery_cooldown(
        normalized_email,
        scope="email-verification",
        settings=settings,
    )
    user = await db_session.scalar(
        select(User).where(User.email == normalized_email).with_for_update()
    )
    if (
        has_flow_grant
        and
        user is not None
        and user.status == "pending_verification"
        and user.email_verified_at is None
    ):
        issued = await issue_email_verification_token(
            db_session,
            user_id=user.id,
            secret_key=settings.platform_secret_key,
            ttl_minutes=settings.platform_email_verification_ttl_minutes,
        )
        await write_audit_log(
            db_session,
            actor_user_id=None,
            action="auth.email_verification.resend",
            subject_type="user",
            subject_id=user.id,
            payload={"expires_at": issued.expires_at.isoformat()},
        )
        await db_session.commit()
        background_tasks.add_task(
            _deliver_email_verification,
            settings,
            recipient_email=user.email,
            code=issued.code,
        )
    else:
        await db_session.rollback()
    response.headers["Cache-Control"] = "no-store"
    if has_flow_grant:
        issue_auth_flow_cookie(
            response,
            purpose="verification",
            account_key=normalized_email,
            settings=settings,
        )
    await _wait_for_generic_auth_response(started_at, settings)
    return AuthActionAcceptedResponse(
        retry_after_seconds=settings.platform_auth_delivery_cooldown_seconds
    )


@router.post(
    "/email-verification/confirm",
    response_model=AuthSessionResponse,
)
async def confirm_email_verification(
    payload: EmailVerificationConfirmRequest,
    request: Request,
    response: Response,
    db_session: AsyncSession = Depends(get_db_session),
) -> AuthSessionResponse:
    settings = get_settings()
    normalized_email = str(payload.email).lower()
    await check_email_verification_confirm_rate_limit(
        request,
        normalized_email,
        settings=settings,
    )
    if not has_valid_auth_flow_cookie(
        request,
        purpose="verification",
        account_key=normalized_email,
        settings=settings,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email verification code is invalid or expired.",
        )
    user = await db_session.scalar(
        select(User).where(User.email == normalized_email).with_for_update()
    )
    if user is None or user.status != "pending_verification":
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email verification code is invalid or expired.",
        )
    verification_code_record = await consume_email_verification_token(
        db_session,
        user_id=user.id,
        code=payload.code,
        secret_key=settings.platform_secret_key,
    )
    if verification_code_record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email verification code is invalid or expired.",
        )
    user.status = "active"
    user.email_verified_at = datetime.now(UTC)
    await consume_user_email_verification_tokens(
        db_session,
        user_id=user.id,
    )
    auth_session = await create_user_session(
        db_session=db_session,
        user=user,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await write_audit_log(
        db_session,
        actor_user_id=None,
        action="auth.email_verification.confirm",
        subject_type="user",
        subject_id=user.id,
        payload={},
    )
    await db_session.commit()
    response.headers["Cache-Control"] = "no-store"
    clear_auth_flow_cookie(
        response,
        purpose="verification",
        settings=settings,
    )
    set_session_cookie(response, auth_session.token)
    issue_csrf_token(response, auth_session.token, settings)
    return AuthSessionResponse(
        user=await serialize_current_user(db_session, user),
        expires_at=auth_session.session.expires_at,
    )


async def _request_email_link(
    *,
    payload: EmailLinkRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    auth_session,
    db_session: AsyncSession,
    is_resend: bool,
) -> AuthActionAcceptedResponse:
    settings = get_settings()
    started_at = time.monotonic()
    if not email_delivery_configured(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email delivery is temporarily unavailable.",
        )
    normalized_email = str(payload.email).lower()
    if is_resend and not has_valid_auth_flow_cookie(
        request,
        purpose="email-link",
        account_key=normalized_email,
        settings=settings,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email link flow is invalid or expired.",
        )
    await check_email_link_rate_limit(
        request,
        f"{auth_session.user.id}:{normalized_email}",
        operation="resend" if is_resend else "request",
        settings=settings,
    )
    await reserve_auth_delivery_cooldown(
        normalized_email,
        scope="email-link",
        settings=settings,
    )
    browser_grant = issue_auth_flow_cookie(
        response,
        purpose="email-link",
        account_key=normalized_email,
        settings=settings,
    )
    user = await db_session.scalar(
        select(User).where(User.id == auth_session.user.id).with_for_update()
    )
    steam_identity = await db_session.scalar(
        select(ExternalIdentity.id).where(
            ExternalIdentity.user_id == auth_session.user.id,
            ExternalIdentity.provider == "steam",
        )
    )
    email_owner = await db_session.scalar(
        select(User.id).where(User.email == normalized_email)
    )
    if (
        user is not None
        and user.status == "active"
        and user.email is None
        and steam_identity is not None
        and email_owner is None
    ):
        issued = await issue_email_link_intent(
            db_session,
            user_id=user.id,
            candidate_email=normalized_email,
            browser_grant=browser_grant,
            secret_key=settings.platform_secret_key,
            ttl_minutes=settings.platform_email_verification_ttl_minutes,
        )
        await write_audit_log(
            db_session,
            actor_user_id=user.id,
            action="auth.email_link.resend" if is_resend else "auth.email_link.request",
            subject_type="user",
            subject_id=user.id,
            payload={"expires_at": issued.expires_at.isoformat()},
        )
        await db_session.commit()
        background_tasks.add_task(
            _deliver_email_verification,
            settings,
            recipient_email=normalized_email,
            code=issued.code,
        )
    else:
        await db_session.rollback()
    response.headers["Cache-Control"] = "no-store"
    await _wait_for_generic_auth_response(started_at, settings)
    return AuthActionAcceptedResponse(
        retry_after_seconds=settings.platform_auth_delivery_cooldown_seconds
    )


@router.post(
    "/email-link/request",
    response_model=AuthActionAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_email_link(
    payload: EmailLinkRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AuthActionAcceptedResponse:
    return await _request_email_link(
        payload=payload,
        request=request,
        response=response,
        background_tasks=background_tasks,
        auth_session=auth_session,
        db_session=db_session,
        is_resend=False,
    )


@router.post(
    "/email-link/resend",
    response_model=AuthActionAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resend_email_link(
    payload: EmailLinkRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AuthActionAcceptedResponse:
    return await _request_email_link(
        payload=payload,
        request=request,
        response=response,
        background_tasks=background_tasks,
        auth_session=auth_session,
        db_session=db_session,
        is_resend=True,
    )


@router.post("/email-link/confirm", response_model=UserResponse)
async def confirm_email_link(
    payload: EmailLinkConfirmRequest,
    request: Request,
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    settings = get_settings()
    normalized_email = str(payload.email).lower()
    await check_email_link_rate_limit(
        request,
        f"{auth_session.user.id}:{normalized_email}",
        operation="confirm",
        settings=settings,
    )
    browser_grant = request.cookies.get(
        auth_flow_cookie_name("email-link", settings),
        "",
    )
    if not has_valid_auth_flow_cookie(
        request,
        purpose="email-link",
        account_key=normalized_email,
        settings=settings,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email link code is invalid or expired.",
        )
    user = await db_session.scalar(
        select(User).where(User.id == auth_session.user.id).with_for_update()
    )
    intent = (
        await consume_email_link_intent(
            db_session,
            user_id=auth_session.user.id,
            candidate_email=normalized_email,
            code=payload.code,
            browser_grant=browser_grant,
            secret_key=settings.platform_secret_key,
        )
        if user is not None and user.status == "active" and user.email is None
        else None
    )
    email_owner = await db_session.scalar(
        select(User.id).where(User.email == normalized_email).with_for_update()
    )
    if intent is None or email_owner is not None or user is None:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email link code is invalid or expired.",
        )
    user.email = normalized_email
    user.email_verified_at = datetime.now(UTC)
    invalidated_sessions = await invalidate_user_sessions(
        db_session,
        user_id=user.id,
        now=auth_session.now,
    )
    rotated_session = await create_user_session(
        db_session=db_session,
        user=user,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await write_audit_log(
        db_session,
        actor_user_id=user.id,
        action="auth.email_link.confirm",
        subject_type="user",
        subject_id=user.id,
        payload={
            "sessions_invalidated": invalidated_sessions,
            "session_rotated": True,
        },
    )
    try:
        await db_session.commit()
    except IntegrityError as exc:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email link code is invalid or expired.",
        ) from exc
    invalidate_user_session_cache(user.id)
    await delete_user_account_read_model(user.id)
    response.headers["Cache-Control"] = "no-store"
    clear_auth_flow_cookie(response, purpose="email-link", settings=settings)
    set_session_cookie(response, rotated_session.token)
    issue_csrf_token(response, rotated_session.token, settings)
    return await serialize_current_user(db_session, user)


async def _request_email_change(
    *,
    email: str,
    current_password: str | None,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    auth_session,
    db_session: AsyncSession,
    is_resend: bool,
) -> AuthActionAcceptedResponse:
    settings = get_settings()
    started_at = time.monotonic()
    normalized_email = email.strip().lower()

    if not email_delivery_configured(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email delivery is temporarily unavailable.",
        )

    user = await db_session.scalar(
        select(User).where(User.id == auth_session.user.id).with_for_update()
    )
    if user is None or user.status != "active" or user.email is None:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email change is not available for this account.",
        )

    if normalized_email == user.email.strip().lower():
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Новая почта должна отличаться от текущей.",
        )

    if is_resend:
        if not has_valid_auth_flow_cookie(
            request,
            purpose="email-change",
            account_key=normalized_email,
            settings=settings,
        ):
            await db_session.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email change flow is invalid or expired.",
            )
    else:
        credential = await db_session.scalar(
            select(PasswordCredential)
            .where(PasswordCredential.user_id == user.id)
            .with_for_update()
        )
        if credential is None:
            await db_session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Для изменения почты сначала установите пароль.",
            )
        if (
            current_password is None
            or not verify_password(current_password, credential.password_hash)
        ):
            await db_session.rollback()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Текущий пароль указан неверно.",
            )

    email_owner = await db_session.scalar(
        select(User.id).where(
            User.email == normalized_email,
            User.id != user.id,
        )
    )
    if email_owner is not None:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Эта почта уже используется.",
        )

    await check_email_link_rate_limit(
        request,
        f"{user.id}:{normalized_email}",
        operation="resend" if is_resend else "request",
        settings=settings,
    )
    await reserve_auth_delivery_cooldown(
        normalized_email,
        scope="email-change",
        settings=settings,
    )

    browser_grant = issue_auth_flow_cookie(
        response,
        purpose="email-change",
        account_key=normalized_email,
        settings=settings,
    )

    issued = await issue_email_link_intent(
        db_session,
        user_id=user.id,
        candidate_email=normalized_email,
        browser_grant=browser_grant,
        secret_key=settings.platform_secret_key,
        ttl_minutes=settings.platform_email_verification_ttl_minutes,
        purpose="email-change",
    )

    await write_audit_log(
        db_session,
        actor_user_id=user.id,
        action=(
            "auth.email_change.resend"
            if is_resend
            else "auth.email_change.request"
        ),
        subject_type="user",
        subject_id=user.id,
        payload={
            "candidate_email": normalized_email,
            "expires_at": issued.expires_at.isoformat(),
        },
    )
    await db_session.commit()

    background_tasks.add_task(
        _deliver_email_verification,
        settings,
        recipient_email=normalized_email,
        code=issued.code,
    )

    response.headers["Cache-Control"] = "no-store"
    await _wait_for_generic_auth_response(started_at, settings)
    return AuthActionAcceptedResponse(
        retry_after_seconds=settings.platform_auth_delivery_cooldown_seconds
    )


@router.post(
    "/email-change/request",
    response_model=AuthActionAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_email_change(
    payload: EmailChangeRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AuthActionAcceptedResponse:
    return await _request_email_change(
        email=str(payload.email),
        current_password=payload.current_password,
        request=request,
        response=response,
        background_tasks=background_tasks,
        auth_session=auth_session,
        db_session=db_session,
        is_resend=False,
    )


@router.post(
    "/email-change/resend",
    response_model=AuthActionAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resend_email_change(
    payload: EmailLinkRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AuthActionAcceptedResponse:
    return await _request_email_change(
        email=str(payload.email),
        current_password=None,
        request=request,
        response=response,
        background_tasks=background_tasks,
        auth_session=auth_session,
        db_session=db_session,
        is_resend=True,
    )


@router.post("/email-change/confirm", response_model=UserResponse)
async def confirm_email_change(
    payload: EmailLinkConfirmRequest,
    request: Request,
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    settings = get_settings()
    normalized_email = str(payload.email).strip().lower()

    await check_email_link_rate_limit(
        request,
        f"{auth_session.user.id}:{normalized_email}",
        operation="confirm",
        settings=settings,
    )

    browser_grant = request.cookies.get(
        auth_flow_cookie_name("email-change", settings),
        "",
    )
    if not has_valid_auth_flow_cookie(
        request,
        purpose="email-change",
        account_key=normalized_email,
        settings=settings,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email change code is invalid or expired.",
        )

    user = await db_session.scalar(
        select(User).where(User.id == auth_session.user.id).with_for_update()
    )
    if user is None or user.status != "active" or user.email is None:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email change code is invalid or expired.",
        )

    previous_email = user.email
    if normalized_email == previous_email.strip().lower():
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email change code is invalid or expired.",
        )

    intent = await consume_email_link_intent(
        db_session,
        user_id=user.id,
        candidate_email=normalized_email,
        code=payload.code,
        browser_grant=browser_grant,
        secret_key=settings.platform_secret_key,
        purpose="email-change",
    )

    email_owner = await db_session.scalar(
        select(User.id)
        .where(
            User.email == normalized_email,
            User.id != user.id,
        )
        .with_for_update()
    )

    if intent is None or email_owner is not None:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email change code is invalid or expired.",
        )

    user.email = normalized_email
    user.email_verified_at = datetime.now(UTC)
    # Changing a verified login identifier must never demote an
    # already-active account back into pending verification.
    user.status = "active"

    profile = await db_session.scalar(
        select(PlayerProfile)
        .where(PlayerProfile.user_id == user.id)
        .with_for_update()
    )
    if profile is not None:
        profile.contact_email = normalized_email

    invalidated_reset_tokens = await consume_user_password_reset_tokens(
        db_session,
        user_id=user.id,
    )
    invalidated_sessions = await invalidate_user_sessions(
        db_session,
        user_id=user.id,
        now=auth_session.now,
    )
    rotated_session = await create_user_session(
        db_session=db_session,
        user=user,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    await write_audit_log(
        db_session,
        actor_user_id=user.id,
        action="auth.email_change.confirm",
        subject_type="user",
        subject_id=user.id,
        payload={
            "previous_email": previous_email,
            "email": normalized_email,
            "password_reset_tokens_invalidated": invalidated_reset_tokens,
            "sessions_invalidated": invalidated_sessions,
            "session_rotated": True,
        },
    )

    try:
        await db_session.commit()
    except IntegrityError as exc:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email change code is invalid or expired.",
        ) from exc

    invalidate_user_session_cache(user.id)
    await delete_user_account_read_model(user.id)
    response.headers["Cache-Control"] = "no-store"
    clear_auth_flow_cookie(
        response,
        purpose="email-change",
        settings=settings,
    )
    set_session_cookie(response, rotated_session.token)
    issue_csrf_token(response, rotated_session.token, settings)

    return await serialize_current_user(db_session, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    auth_session.session.invalidated_at = auth_session.now
    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="auth.logout",
        subject_type="session",
        subject_id=auth_session.session.id,
        payload={},
    )
    await db_session.commit()
    invalidate_session_cache(auth_session.session.token_digest)
    response.status_code = status.HTTP_204_NO_CONTENT
    clear_session_cookies(response)
    return response


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all_sessions(
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    invalidated = await invalidate_user_sessions(
        db_session,
        user_id=auth_session.user.id,
        now=auth_session.now,
    )
    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="auth.logout_all",
        subject_type="user",
        subject_id=auth_session.user.id,
        payload={"sessions_invalidated": invalidated},
    )
    await db_session.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    clear_session_cookies(response)
    return response


@router.get("/sessions", response_model=list[AuthSessionListItemResponse])
async def list_sessions(
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[AuthSessionListItemResponse]:
    response.headers["Cache-Control"] = "no-store"
    sessions = list(
        (
            await db_session.scalars(
                select(UserSession)
                .where(
                    UserSession.user_id == auth_session.user.id,
                    UserSession.invalidated_at.is_(None),
                    UserSession.expires_at > auth_session.now,
                )
                .order_by(UserSession.last_seen_at.desc(), UserSession.created_at.desc())
            )
        ).all()
    )
    return [
        AuthSessionListItemResponse(
            id=session.id,
            ip_address=session.ip_address,
            user_agent=session.user_agent,
            created_at=session.created_at,
            last_seen_at=session.last_seen_at,
            expires_at=session.expires_at,
            is_current=session.id == auth_session.session.id,
        )
        for session in sessions
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: str,
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    session = await db_session.scalar(
        select(UserSession)
        .where(
            UserSession.id == session_id,
            UserSession.user_id == auth_session.user.id,
            UserSession.invalidated_at.is_(None),
            UserSession.expires_at > auth_session.now,
        )
        .with_for_update()
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    session.invalidated_at = auth_session.now
    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="auth.session.revoke",
        subject_type="session",
        subject_id=session.id,
        payload={"current_session": session.id == auth_session.session.id},
    )
    await db_session.commit()
    invalidate_session_cache(session.token_digest)
    response.status_code = status.HTTP_204_NO_CONTENT
    if session.id == auth_session.session.id:
        clear_session_cookies(response)
    return response


@router.get("/csrf", response_model=CsrfTokenResponse)
async def csrf_token(
    request: Request,
    response: Response,
    db_session: AsyncSession = Depends(get_db_session),
) -> CsrfTokenResponse | Response:
    settings = get_settings()
    try:
        await get_authenticated_session(request, db_session)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_401_UNAUTHORIZED:
            raise
        invalid_response = JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": str(exc.detail)},
            headers={"Cache-Control": "no-store"},
        )
        clear_session_cookies(invalid_response)
        return invalid_response
    session_token = request.cookies[settings.platform_session_cookie_name]
    return CsrfTokenResponse(
        csrf_token=issue_csrf_token(response, session_token, settings)
    )


@router.get("/session", response_model=UserResponse)
async def current_session(
    response: Response,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    response.headers["Cache-Control"] = "no-store"
    return await serialize_current_user(
        db_session,
        auth_session.user,
        role_slugs=auth_session.role_slugs,
    )


@router.patch("/account", response_model=UserResponse)
async def update_account_security(
    payload: AccountSecurityUpdateRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    auth_session=Depends(get_authenticated_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    settings = get_settings()
    locked_user = await db_session.scalar(
        select(User).where(User.id == auth_session.user.id).with_for_update()
    )
    if locked_user is None or locked_user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    auth_session.user = locked_user
    credential = await db_session.scalar(
        select(PasswordCredential)
        .where(PasswordCredential.user_id == auth_session.user.id)
        .with_for_update()
    )
    if (
        credential is None
        or payload.current_password is None
        or not verify_password(payload.current_password, credential.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Текущий пароль указан неверно.",
        )

    normalized_email = (
        str(payload.email).strip().lower()
        if payload.email is not None
        else None
    )

    if (
        normalized_email is not None
        and normalized_email != auth_session.user.email
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Изменение почты требует подтверждения кодом.",
        )

    if payload.new_password is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Данные аккаунта не изменились.",
        )

    if verify_password(payload.new_password, credential.password_hash):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Новый пароль должен отличаться от текущего.",
        )

    credential.password_hash = hash_password(payload.new_password)
    credential.password_version = "argon2id"
    await db_session.flush()

    invalidated_reset_tokens = await consume_user_password_reset_tokens(
        db_session,
        user_id=auth_session.user.id,
    )
    invalidated_sessions = await invalidate_user_sessions(
        db_session,
        user_id=auth_session.user.id,
        now=auth_session.now,
    )
    rotated_session = None
    if auth_session.user.status == "active":
        rotated_session = await create_user_session(
            db_session=db_session,
            user=auth_session.user,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )

    await write_audit_log(
        db_session,
        actor_user_id=auth_session.user.id,
        action="auth.account.update",
        subject_type="user",
        subject_id=auth_session.user.id,
        payload={
            "password_changed": True,
            "password_reset_tokens_invalidated": invalidated_reset_tokens,
            "sessions_invalidated": invalidated_sessions,
            "session_rotated": rotated_session is not None,
        },
    )
    await db_session.commit()
    invalidate_user_session_cache(auth_session.user.id)
    await delete_user_account_read_model(auth_session.user.id)
    await db_session.refresh(auth_session.user)
    response.headers["Cache-Control"] = "no-store"
    if rotated_session is None:
        clear_session_cookies(response)
    else:
        set_session_cookie(response, rotated_session.token)
        issue_csrf_token(response, rotated_session.token, settings)
    return await serialize_current_user(db_session, auth_session.user)
