from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hmac
from hashlib import sha256
from hmac import compare_digest

from fastapi import Depends, HTTPException, Request, Response, status
from pwdlib import PasswordHash
from sqlalchemy import or_, select, update
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.auth_lifecycle import (
    email_delivery_configured,
    email_verification_required,
)
from python_packages.platform_infra.config import PlatformSettings, get_settings
from python_packages.platform_infra.csrf import clear_csrf_cookie
from python_packages.platform_infra.db import get_db_session, session_factory
from python_packages.platform_infra.models import Role, User, UserRole, UserSession
from python_packages.platform_infra.turnstile import (
    normalized_turnstile_mode,
    validate_turnstile_settings,
)

password_hasher = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = password_hasher.hash("oldsparky-dummy-login-credential")
OPTIONAL_AUTH_SESSION_CACHE_TTL_SECONDS = 15 * 60.0
OPTIONAL_AUTH_SESSION_CACHE_MAX_ENTRIES = 30_000


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def verify_login_password(password: str, password_hash: str | None) -> bool:
    return verify_password(password, password_hash or DUMMY_PASSWORD_HASH)


def new_session_token() -> str:
    return secrets.token_urlsafe(48)


def session_token_digest(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


AUTH_FLOW_PURPOSES = frozenset(
    {"email-change", "email-link", "password-reset", "steam", "verification"}
)


def auth_flow_cookie_name(
    purpose: str,
    settings: PlatformSettings | None = None,
) -> str:
    if purpose not in AUTH_FLOW_PURPOSES:
        raise ValueError("Unsupported authentication flow purpose.")
    resolved = settings or get_settings()
    return f"{resolved.platform_session_cookie_name}_{purpose.replace('-', '_')}_flow"


def _auth_flow_account_digest(account_key: str) -> str:
    return sha256(account_key.strip().lower().encode("utf-8")).hexdigest()


def _auth_flow_signature(settings: PlatformSettings, purpose: str, payload: str) -> str:
    return hmac.new(
        settings.platform_secret_key.encode("utf-8"),
        f"auth-flow:v1:{purpose}:{payload}".encode("utf-8"),
        sha256,
    ).hexdigest()


def issue_auth_flow_cookie(
    response: Response,
    *,
    purpose: str,
    account_key: str,
    settings: PlatformSettings | None = None,
) -> str:
    resolved = settings or get_settings()
    expires_at = int(time.time()) + resolved.platform_auth_flow_ttl_minutes * 60
    nonce = secrets.token_urlsafe(24)
    raw_payload = f"{expires_at}:{nonce}:{_auth_flow_account_digest(account_key)}"
    encoded_payload = urlsafe_b64encode(raw_payload.encode("utf-8")).decode("ascii").rstrip("=")
    token = f"{encoded_payload}.{_auth_flow_signature(resolved, purpose, encoded_payload)}"
    response.set_cookie(
        key=auth_flow_cookie_name(purpose, resolved),
        value=token,
        httponly=True,
        secure=resolved.platform_cookie_secure,
        samesite="lax",
        path="/",
        max_age=resolved.platform_auth_flow_ttl_minutes * 60,
    )
    response.headers["Cache-Control"] = "no-store"
    return token


def has_valid_auth_flow_cookie(
    request: Request,
    *,
    purpose: str,
    account_key: str,
    settings: PlatformSettings | None = None,
    now_epoch: int | None = None,
) -> bool:
    resolved = settings or get_settings()
    token = request.cookies.get(auth_flow_cookie_name(purpose, resolved), "")
    try:
        encoded_payload, supplied_signature = token.split(".", 1)
    except ValueError:
        return False
    expected_signature = _auth_flow_signature(resolved, purpose, encoded_payload)
    if not compare_digest(supplied_signature, expected_signature):
        return False
    try:
        padding = "=" * (-len(encoded_payload) % 4)
        raw_payload = urlsafe_b64decode(encoded_payload + padding).decode("utf-8")
        expires_at_text, nonce, account_digest = raw_payload.split(":", 2)
        expires_at = int(expires_at_text)
    except (UnicodeDecodeError, ValueError):
        return False
    now = int(time.time()) if now_epoch is None else now_epoch
    if expires_at <= now or expires_at > now + resolved.platform_auth_flow_ttl_minutes * 60:
        return False
    if len(nonce) < 24:
        return False
    return compare_digest(account_digest, _auth_flow_account_digest(account_key))


def clear_auth_flow_cookie(
    response: Response,
    *,
    purpose: str,
    settings: PlatformSettings | None = None,
) -> None:
    resolved = settings or get_settings()
    response.delete_cookie(
        key=auth_flow_cookie_name(purpose, resolved),
        path="/",
        httponly=True,
        secure=resolved.platform_cookie_secure,
        samesite="lax",
    )

def validate_auth_security_settings(settings=None) -> None:
    resolved_settings = settings or get_settings()
    cookie_name = resolved_settings.platform_session_cookie_name
    if cookie_name.startswith("__Host-") and not resolved_settings.platform_cookie_secure:
        raise RuntimeError("A __Host- session cookie requires PLATFORM_COOKIE_SECURE=true.")
    if resolved_settings.platform_environment.strip().lower() == "production":
        if not resolved_settings.platform_cookie_secure:
            raise RuntimeError("Production session cookies must be Secure.")
        if not cookie_name.startswith("__Host-"):
            raise RuntimeError("Production session cookie names must use the __Host- prefix.")
        if normalized_turnstile_mode(resolved_settings) == "off":
            raise RuntimeError("Production authentication requires Turnstile protection.")
        if (
            resolved_settings.platform_public_registration_enabled is True
            and not email_delivery_configured(resolved_settings)
        ):
            raise RuntimeError(
                "Production public registration requires configured email delivery."
            )
    validate_turnstile_settings(resolved_settings)


def public_registration_enabled(settings=None) -> bool:
    resolved_settings = settings or get_settings()
    configured = resolved_settings.platform_public_registration_enabled
    if configured is not None:
        return configured
    return resolved_settings.platform_environment.strip().lower() != "production"


def set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    validate_auth_security_settings(settings)
    response.set_cookie(
        key=settings.platform_session_cookie_name,
        value=token,
        httponly=True,
        secure=settings.platform_cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings.platform_session_ttl_days * 24 * 60 * 60,
    )


def clear_session_cookies(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.platform_session_cookie_name,
        path="/",
        httponly=True,
        secure=settings.platform_cookie_secure,
        samesite="lax",
    )
    clear_csrf_cookie(response, settings)


@dataclass(slots=True)
class AuthenticatedSession:
    user: User
    session: UserSession
    role_slugs: frozenset[str]
    now: datetime


@dataclass(slots=True)
class CreatedSession:
    token: str
    session: UserSession


@dataclass(slots=True)
class CachedAuthenticatedSession:
    expires_at: float
    user_id: str
    user_email: str | None
    user_display_name: str
    user_status: str
    user_email_verified_at: datetime | None
    user_created_at: datetime
    user_updated_at: datetime
    public_tournament_credits: int
    private_tournament_credits: int
    session_id: str
    session_token_digest: str
    session_ip_address: str | None
    session_user_agent: str | None
    session_created_at: datetime
    session_last_seen_at: datetime
    session_expires_at: datetime
    session_invalidated_at: datetime | None
    role_slugs: frozenset[str]


_optional_auth_session_cache: dict[str, CachedAuthenticatedSession] = {}


def _trim_optional_auth_session_cache(now_monotonic: float) -> None:
    expired_keys = [
        token_digest
        for token_digest, entry in _optional_auth_session_cache.items()
        if entry.expires_at <= now_monotonic
    ]
    for token_digest in expired_keys:
        _optional_auth_session_cache.pop(token_digest, None)
    while len(_optional_auth_session_cache) >= OPTIONAL_AUTH_SESSION_CACHE_MAX_ENTRIES:
        oldest_key = next(iter(_optional_auth_session_cache), None)
        if oldest_key is None:
            break
        _optional_auth_session_cache.pop(oldest_key, None)


def _cached_authenticated_session(
    token_digest: str,
    *,
    now: datetime,
    settings: PlatformSettings,
) -> AuthenticatedSession | None:
    now_monotonic = time.monotonic()
    entry = _optional_auth_session_cache.get(token_digest)
    if entry is None:
        return None
    if (
        entry.expires_at <= now_monotonic
        or entry.session_expires_at <= now
        or entry.session_invalidated_at is not None
        or entry.user_status != "active"
        or (
            email_verification_required(settings)
            and entry.user_email is not None
            and entry.user_email_verified_at is None
        )
    ):
        _optional_auth_session_cache.pop(token_digest, None)
        return None
    user = User(
        id=entry.user_id,
        email=entry.user_email,
        display_name=entry.user_display_name,
        status=entry.user_status,
        email_verified_at=entry.user_email_verified_at,
        public_tournament_credits=entry.public_tournament_credits,
        private_tournament_credits=entry.private_tournament_credits,
        created_at=entry.user_created_at,
        updated_at=entry.user_updated_at,
    )
    session = UserSession(
        id=entry.session_id,
        user_id=entry.user_id,
        token_digest=entry.session_token_digest,
        ip_address=entry.session_ip_address,
        user_agent=entry.session_user_agent,
        created_at=entry.session_created_at,
        last_seen_at=entry.session_last_seen_at,
        expires_at=entry.session_expires_at,
        invalidated_at=entry.session_invalidated_at,
    )
    return AuthenticatedSession(
        user=user,
        session=session,
        role_slugs=entry.role_slugs,
        now=now,
    )


def remember_authenticated_session(token_digest: str, auth_session: AuthenticatedSession) -> None:
    now_monotonic = time.monotonic()
    _trim_optional_auth_session_cache(now_monotonic)
    _optional_auth_session_cache[token_digest] = CachedAuthenticatedSession(
        expires_at=now_monotonic + OPTIONAL_AUTH_SESSION_CACHE_TTL_SECONDS,
        user_id=auth_session.user.id,
        user_email=auth_session.user.email,
        user_display_name=auth_session.user.display_name,
        user_status=auth_session.user.status,
        user_email_verified_at=auth_session.user.email_verified_at,
        user_created_at=auth_session.user.created_at,
        user_updated_at=auth_session.user.updated_at,
        public_tournament_credits=int(auth_session.user.public_tournament_credits or 0),
        private_tournament_credits=int(auth_session.user.private_tournament_credits or 0),
        session_id=auth_session.session.id,
        session_token_digest=auth_session.session.token_digest,
        session_ip_address=auth_session.session.ip_address,
        session_user_agent=auth_session.session.user_agent,
        session_created_at=auth_session.session.created_at,
        session_last_seen_at=auth_session.session.last_seen_at,
        session_expires_at=auth_session.session.expires_at,
        session_invalidated_at=auth_session.session.invalidated_at,
        role_slugs=auth_session.role_slugs,
    )


def invalidate_session_cache(token_digest: str) -> None:
    _optional_auth_session_cache.pop(token_digest, None)


def invalidate_user_session_cache(user_id: str) -> None:
    stale_keys = [
        token_digest
        for token_digest, entry in _optional_auth_session_cache.items()
        if entry.user_id == user_id
    ]
    for token_digest in stale_keys:
        _optional_auth_session_cache.pop(token_digest, None)


async def role_slugs_for_user(db_session: AsyncSession, user_id: str) -> frozenset[str]:
    rows = await db_session.execute(
        select(Role.slug)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    return frozenset(rows.scalars().all())


async def create_user_session(
    *,
    db_session: AsyncSession,
    user: User,
    ip_address: str | None,
    user_agent: str | None,
) -> CreatedSession:
    settings = get_settings()
    if user.status != "active" or (
        email_verification_required(settings)
        and user.email is not None
        and user.email_verified_at is None
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    now = datetime.now(UTC)
    await db_session.scalar(select(User.id).where(User.id == user.id).with_for_update())
    overflow_rows = list(
        (
            await db_session.execute(
                select(UserSession.id, UserSession.token_digest)
                .where(
                    UserSession.user_id == user.id,
                    UserSession.invalidated_at.is_(None),
                    UserSession.expires_at > now,
                )
                .order_by(UserSession.created_at.desc(), UserSession.id.desc())
                .offset(max(0, settings.platform_session_max_active - 1))
            )
        ).all()
    )
    if overflow_rows:
        overflow_ids = [str(row[0]) for row in overflow_rows]
        await db_session.execute(
            update(UserSession)
            .where(UserSession.id.in_(overflow_ids))
            .values(invalidated_at=now)
        )
        for _, digest in overflow_rows:
            invalidate_session_cache(str(digest))
    token = new_session_token()
    session = UserSession(
        user_id=user.id,
        token_digest=session_token_digest(token),
        ip_address=ip_address,
        user_agent=user_agent,
        last_seen_at=now,
        expires_at=now + timedelta(days=settings.platform_session_ttl_days),
    )
    db_session.add(session)
    await db_session.flush()
    return CreatedSession(token=token, session=session)


async def invalidate_user_sessions(
    db_session: AsyncSession,
    *,
    user_id: str,
    now: datetime | None = None,
    exclude_session_id: str | None = None,
) -> int:
    invalidated_at = now or datetime.now(UTC)
    locked_user_id = await db_session.scalar(
        select(User.id).where(User.id == user_id).with_for_update()
    )
    if locked_user_id is None:
        return 0
    statement = update(UserSession).where(
        UserSession.user_id == user_id,
        UserSession.invalidated_at.is_(None),
        UserSession.expires_at > invalidated_at,
    )
    if exclude_session_id is not None:
        statement = statement.where(UserSession.id != exclude_session_id)
    result = await db_session.execute(statement.values(invalidated_at=invalidated_at))
    invalidate_user_session_cache(user_id)
    return int(result.rowcount or 0)


async def _touch_authenticated_session(
    auth_session: AuthenticatedSession,
) -> None:
    """Persist last-seen metadata without committing the caller's transaction.

    Authentication is a dependency of mutation serializers.  A session touch
    therefore must use its own short transaction so a metadata write can never
    release Tournament/Invite locks already owned by the request session.
    """

    settings = get_settings()
    touch_before = auth_session.now - timedelta(
        seconds=settings.platform_session_touch_interval_seconds
    )
    last_seen_at = auth_session.session.last_seen_at
    if last_seen_at is not None and last_seen_at > touch_before:
        return

    factory = session_factory()
    async with factory() as touch_session:
        result = await touch_session.execute(
            update(UserSession)
            .where(
                UserSession.id == auth_session.session.id,
                UserSession.invalidated_at.is_(None),
                or_(
                    UserSession.last_seen_at.is_(None),
                    UserSession.last_seen_at <= touch_before,
                ),
            )
            .values(last_seen_at=auth_session.now)
        )
        await touch_session.commit()
    if int(result.rowcount or 0) > 0:
        auth_session.session.last_seen_at = auth_session.now


async def get_authenticated_session(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> AuthenticatedSession:
    settings = get_settings()
    token = request.cookies.get(settings.platform_session_cookie_name)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")

    now = datetime.now(UTC)
    token_digest = session_token_digest(token)
    user_predicates = [User.status == "active"]
    if email_verification_required(settings):
        user_predicates.append(
            (User.email.is_(None)) | (User.email_verified_at.is_not(None))
        )
    rows = (
        await db_session.execute(
            select(UserSession, User, Role.slug)
            .join(User, User.id == UserSession.user_id)
            .outerjoin(UserRole, UserRole.user_id == User.id)
            .outerjoin(Role, Role.id == UserRole.role_id)
            .where(
                UserSession.token_digest == token_digest,
                UserSession.invalidated_at.is_(None),
                UserSession.expires_at > now,
                *user_predicates,
            )
        )
    ).all()
    if not rows:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is invalid.")

    user_session = rows[0][0]
    user = rows[0][1]
    if user is None or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session owner is missing.")

    roles = frozenset(str(role_slug) for _, _, role_slug in rows if role_slug)
    auth_session = AuthenticatedSession(user=user, session=user_session, role_slugs=roles, now=now)
    await _touch_authenticated_session(auth_session)
    remember_authenticated_session(token_digest, auth_session)
    return auth_session


async def _resolve_optional_authenticated_session(
    request: Request,
    db_session: AsyncSession,
    *,
    touch_session: bool,
) -> AuthenticatedSession | None:
    settings = get_settings()
    token = request.cookies.get(settings.platform_session_cookie_name)
    if not token:
        return None

    now = datetime.now(UTC)
    token_digest = session_token_digest(token)
    cached_session = _cached_authenticated_session(
        token_digest,
        now=now,
        settings=settings,
    )
    user_predicates = [User.status == "active"]
    if email_verification_required(settings):
        user_predicates.append(
            (User.email.is_(None)) | (User.email_verified_at.is_not(None))
        )
    if cached_session is not None:
        try:
            cached_session_is_current = await db_session.scalar(
                select(UserSession.id)
                .join(User, User.id == UserSession.user_id)
                .where(
                    UserSession.token_digest == token_digest,
                    UserSession.invalidated_at.is_(None),
                    UserSession.expires_at > now,
                    *user_predicates,
                )
            )
        except SQLAlchemyTimeoutError:
            return None
        if cached_session_is_current is not None:
            return cached_session
        invalidate_session_cache(token_digest)

    try:
        rows = (
            await db_session.execute(
                select(UserSession, User, Role.slug)
                .join(User, User.id == UserSession.user_id)
                .outerjoin(UserRole, UserRole.user_id == User.id)
                .outerjoin(Role, Role.id == UserRole.role_id)
                .where(
                    UserSession.token_digest == token_digest,
                    UserSession.invalidated_at.is_(None),
                    UserSession.expires_at > now,
                    *user_predicates,
                )
            )
        ).all()
    except SQLAlchemyTimeoutError:
        return None
    if not rows:
        return None

    user_session = rows[0][0]
    user = rows[0][1]
    if user is None or user.status != "active":
        return None

    roles = frozenset(str(role_slug) for _, _, role_slug in rows if role_slug)
    auth_session = AuthenticatedSession(user=user, session=user_session, role_slugs=roles, now=now)
    if touch_session:
        await _touch_authenticated_session(auth_session)
    remember_authenticated_session(token_digest, auth_session)
    return auth_session


async def get_optional_authenticated_session(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> AuthenticatedSession | None:
    # Optional authentication is used by the high-volume read path.  The
    # session lookup is authoritative, but last_seen_at is telemetry rather
    # than an authorization decision.  Do not open a second transaction from
    # inside the request while the primary read session is checked out.  This
    # keeps a first authenticated GET at one DB connection instead of two.
    return await _resolve_optional_authenticated_session(
        request,
        db_session,
        touch_session=False,
    )
