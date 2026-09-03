from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hmac
from hashlib import sha256

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.config import PlatformSettings
from python_packages.platform_infra.models import (
    EmailVerificationToken,
    GoogleAuthFlow,
    PasswordResetToken,
    SteamAuthFlow,
    SteamEmailLinkIntent,
    User,
    UserSession,
)


@dataclass(frozen=True, slots=True)
class IssuedOneTimeCode:
    code: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedEmailLinkCode(IssuedOneTimeCode):
    intent_id: str


@dataclass(frozen=True, slots=True)
class AuthCleanupResult:
    sessions_deleted: int
    password_reset_tokens_deleted: int
    email_verification_tokens_deleted: int
    steam_auth_flows_deleted: int
    google_auth_flows_deleted: int
    email_link_intents_deleted: int

    def as_dict(self) -> dict[str, int]:
        return {
            "sessions_deleted": self.sessions_deleted,
            "password_reset_tokens_deleted": self.password_reset_tokens_deleted,
            "email_verification_tokens_deleted": self.email_verification_tokens_deleted,
            "steam_auth_flows_deleted": self.steam_auth_flows_deleted,
            "google_auth_flows_deleted": self.google_auth_flows_deleted,
            "email_link_intents_deleted": self.email_link_intents_deleted,
        }


def email_verification_required(settings: PlatformSettings) -> bool:
    if settings.platform_environment.strip().lower() == "production":
        return True
    return bool(settings.platform_email_verification_required)


def email_sender(settings: PlatformSettings) -> str:
    return (
        settings.platform_email_sender_email
        or settings.platform_support_smtp_sender_email
        or ""
    ).strip()


def email_delivery_configured(settings: PlatformSettings) -> bool:
    if (settings.platform_resend_api_key or "").strip() and email_sender(settings):
        return True
    production = settings.platform_environment.strip().lower() == "production"
    encrypted_transport = bool(
        settings.platform_support_smtp_ssl
        or settings.platform_support_smtp_starttls
    )
    return bool(
        (settings.platform_support_smtp_host or "").strip()
        and email_sender(settings)
        and (not production or encrypted_transport)
        and (
            not (settings.platform_support_smtp_username or "").strip()
            or (settings.platform_support_smtp_password or "").strip()
        )
    )


def one_time_code_digest(user_id: str, code: str, *, secret_key: str) -> str:
    return hmac.new(
        secret_key.encode("utf-8"),
        f"{user_id}:{code}".encode("utf-8"),
        sha256,
    ).hexdigest()


def auth_grant_digest(grant: str, *, secret_key: str, purpose: str) -> str:
    return hmac.new(
        secret_key.encode("utf-8"),
        f"auth-grant:v1:{purpose}:{grant}".encode("utf-8"),
        sha256,
    ).hexdigest()


def _new_verification_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def _lock_user(db_session: AsyncSession, user_id: str) -> bool:
    return (
        await db_session.scalar(
            select(User.id).where(User.id == user_id).with_for_update()
        )
        is not None
    )


async def issue_password_reset_token(
    db_session: AsyncSession,
    *,
    user_id: str,
    secret_key: str,
    ttl_minutes: int,
    now: datetime | None = None,
) -> IssuedOneTimeCode:
    issued_at = now or datetime.now(UTC)
    if not await _lock_user(db_session, user_id):
        raise LookupError("Password reset code owner does not exist.")
    await db_session.execute(
        delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
    )
    code = _new_verification_code()
    expires_at = issued_at + timedelta(minutes=ttl_minutes)
    db_session.add(
        PasswordResetToken(
            user_id=user_id,
            token_digest=one_time_code_digest(user_id, code, secret_key=secret_key),
            expires_at=expires_at,
        )
    )
    await db_session.flush()
    return IssuedOneTimeCode(code=code, expires_at=expires_at)


async def validate_password_reset_code(
    db_session: AsyncSession,
    *,
    user_id: str,
    code: str,
    secret_key: str,
    now: datetime | None = None,
) -> bool:
    checked_at = now or datetime.now(UTC)
    digest = one_time_code_digest(user_id, code, secret_key=secret_key)
    return (
        await db_session.scalar(
            select(PasswordResetToken.id).where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.token_digest == digest,
                PasswordResetToken.consumed_at.is_(None),
                PasswordResetToken.expires_at > checked_at,
            )
        )
    ) is not None


async def consume_password_reset_token(
    db_session: AsyncSession,
    *,
    user_id: str,
    code: str,
    secret_key: str,
    now: datetime | None = None,
) -> PasswordResetToken | None:
    consumed_at = now or datetime.now(UTC)
    digest = one_time_code_digest(user_id, code, secret_key=secret_key)
    if not await _lock_user(db_session, user_id):
        return None
    row = await db_session.scalar(
        select(PasswordResetToken)
        .where(
            PasswordResetToken.token_digest == digest,
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.consumed_at.is_(None),
            PasswordResetToken.expires_at > consumed_at,
        )
        .with_for_update()
    )
    if row is not None:
        row.consumed_at = consumed_at
    return row


async def consume_user_password_reset_tokens(
    db_session: AsyncSession,
    *,
    user_id: str,
    now: datetime | None = None,
) -> int:
    consumed_at = now or datetime.now(UTC)
    if not await _lock_user(db_session, user_id):
        return 0
    result = await db_session.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.consumed_at.is_(None),
        )
        .values(consumed_at=consumed_at)
    )
    return int(result.rowcount or 0)


async def issue_email_verification_token(
    db_session: AsyncSession,
    *,
    user_id: str,
    secret_key: str,
    ttl_minutes: int,
    now: datetime | None = None,
) -> IssuedOneTimeCode:
    issued_at = now or datetime.now(UTC)
    if not await _lock_user(db_session, user_id):
        raise LookupError("Email verification code owner does not exist.")
    await db_session.execute(
        delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id)
    )
    code = _new_verification_code()
    expires_at = issued_at + timedelta(minutes=ttl_minutes)
    db_session.add(
        EmailVerificationToken(
            user_id=user_id,
            token_digest=one_time_code_digest(user_id, code, secret_key=secret_key),
            expires_at=expires_at,
        )
    )
    await db_session.flush()
    return IssuedOneTimeCode(code=code, expires_at=expires_at)


async def consume_email_verification_token(
    db_session: AsyncSession,
    *,
    user_id: str,
    code: str,
    secret_key: str,
    now: datetime | None = None,
) -> EmailVerificationToken | None:
    consumed_at = now or datetime.now(UTC)
    digest = one_time_code_digest(user_id, code, secret_key=secret_key)
    if not await _lock_user(db_session, user_id):
        return None
    row = await db_session.scalar(
        select(EmailVerificationToken)
        .where(
            EmailVerificationToken.token_digest == digest,
            EmailVerificationToken.user_id == user_id,
            EmailVerificationToken.consumed_at.is_(None),
            EmailVerificationToken.expires_at > consumed_at,
        )
        .with_for_update()
    )
    if row is not None:
        row.consumed_at = consumed_at
    return row


async def consume_user_email_verification_tokens(
    db_session: AsyncSession,
    *,
    user_id: str,
    now: datetime | None = None,
) -> int:
    consumed_at = now or datetime.now(UTC)
    if not await _lock_user(db_session, user_id):
        return 0
    result = await db_session.execute(
        update(EmailVerificationToken)
        .where(
            EmailVerificationToken.user_id == user_id,
            EmailVerificationToken.consumed_at.is_(None),
        )
        .values(consumed_at=consumed_at)
    )
    return int(result.rowcount or 0)


async def issue_email_link_intent(
    db_session: AsyncSession,
    *,
    user_id: str,
    candidate_email: str,
    browser_grant: str,
    secret_key: str,
    ttl_minutes: int,
    purpose: str = "email-link",
    now: datetime | None = None,
) -> IssuedEmailLinkCode:
    issued_at = now or datetime.now(UTC)
    if not await _lock_user(db_session, user_id):
        raise LookupError("Email-link owner does not exist.")
    await db_session.execute(
        delete(SteamEmailLinkIntent).where(
            SteamEmailLinkIntent.user_id == user_id,
            SteamEmailLinkIntent.consumed_at.is_(None),
        )
    )
    code = _new_verification_code()
    intent = SteamEmailLinkIntent(
        user_id=user_id,
        candidate_email=candidate_email.strip().lower(),
        code_digest=one_time_code_digest(user_id, code, secret_key=secret_key),
        browser_grant_digest=auth_grant_digest(
            browser_grant,
            secret_key=secret_key,
            purpose=purpose,
        ),
        expires_at=issued_at + timedelta(minutes=ttl_minutes),
        last_sent_at=issued_at,
    )
    db_session.add(intent)
    await db_session.flush()
    return IssuedEmailLinkCode(
        code=code,
        expires_at=intent.expires_at,
        intent_id=intent.id,
    )


async def consume_email_link_intent(
    db_session: AsyncSession,
    *,
    user_id: str,
    candidate_email: str,
    code: str,
    browser_grant: str,
    secret_key: str,
    purpose: str = "email-link",
    now: datetime | None = None,
) -> SteamEmailLinkIntent | None:
    consumed_at = now or datetime.now(UTC)
    if not await _lock_user(db_session, user_id):
        return None
    intent = await db_session.scalar(
        select(SteamEmailLinkIntent)
        .where(
            SteamEmailLinkIntent.user_id == user_id,
            SteamEmailLinkIntent.candidate_email == candidate_email.strip().lower(),
            SteamEmailLinkIntent.code_digest
            == one_time_code_digest(user_id, code, secret_key=secret_key),
            SteamEmailLinkIntent.browser_grant_digest
            == auth_grant_digest(
                browser_grant,
                secret_key=secret_key,
                purpose=purpose,
            ),
            SteamEmailLinkIntent.consumed_at.is_(None),
            SteamEmailLinkIntent.expires_at > consumed_at,
        )
        .with_for_update()
    )
    if intent is not None:
        intent.consumed_at = consumed_at
    return intent


async def cleanup_auth_lifecycle_records(
    db_session: AsyncSession,
    *,
    now: datetime | None = None,
) -> AuthCleanupResult:
    cleanup_at = now or datetime.now(UTC)
    consumed_cutoff = cleanup_at - timedelta(days=1)
    invalidated_cutoff = cleanup_at - timedelta(days=1)
    session_result = await db_session.execute(
        delete(UserSession).where(
            or_(
                UserSession.expires_at <= cleanup_at,
                UserSession.invalidated_at <= invalidated_cutoff,
            )
        )
    )
    reset_result = await db_session.execute(
        delete(PasswordResetToken).where(
            or_(
                PasswordResetToken.expires_at <= cleanup_at,
                PasswordResetToken.consumed_at <= consumed_cutoff,
            )
        )
    )
    verification_result = await db_session.execute(
        delete(EmailVerificationToken).where(
            or_(
                EmailVerificationToken.expires_at <= cleanup_at,
                EmailVerificationToken.consumed_at <= consumed_cutoff,
            )
        )
    )
    steam_flow_result = await db_session.execute(
        delete(SteamAuthFlow).where(
            or_(
                SteamAuthFlow.expires_at <= cleanup_at,
                SteamAuthFlow.consumed_at <= consumed_cutoff,
            )
        )
    )
    google_flow_result = await db_session.execute(
        delete(GoogleAuthFlow).where(
            or_(
                GoogleAuthFlow.expires_at <= cleanup_at,
                GoogleAuthFlow.consumed_at <= consumed_cutoff,
            )
        )
    )
    email_link_result = await db_session.execute(
        delete(SteamEmailLinkIntent).where(
            or_(
                SteamEmailLinkIntent.expires_at <= cleanup_at,
                SteamEmailLinkIntent.consumed_at <= consumed_cutoff,
            )
        )
    )
    await db_session.commit()
    return AuthCleanupResult(
        sessions_deleted=int(session_result.rowcount or 0),
        password_reset_tokens_deleted=int(reset_result.rowcount or 0),
        email_verification_tokens_deleted=int(verification_result.rowcount or 0),
        steam_auth_flows_deleted=int(steam_flow_result.rowcount or 0),
        google_auth_flows_deleted=int(google_flow_result.rowcount or 0),
        email_link_intents_deleted=int(email_link_result.rowcount or 0),
    )
