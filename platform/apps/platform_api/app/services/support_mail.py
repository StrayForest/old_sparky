from __future__ import annotations

import asyncio
from email.message import EmailMessage
import secrets
import smtplib
import ssl

import httpx

from python_packages.platform_infra.config import PlatformSettings
from python_packages.platform_infra.auth_lifecycle import (
    email_delivery_configured,
    email_sender,
)


CATEGORY_LABELS = {
    "account": "Аккаунт",
    "tournament": "Турнир",
    "technical": "Техническая проблема",
    "rules": "Правила и модерация",
    "other": "Другое",
}


def support_mail_configured(settings: PlatformSettings) -> bool:
    return bool(
        settings.platform_support_recipient_email
        and email_delivery_configured(settings)
    )


async def send_email_message(settings: PlatformSettings, email: EmailMessage) -> None:
    if (settings.platform_resend_api_key or "").strip():
        await _send_resend_message(settings, email)
        return
    await asyncio.to_thread(_send_email_message_sync, settings, email)


async def send_support_message(
    settings: PlatformSettings,
    *,
    name: str,
    reply_email: str,
    category: str,
    message: str,
) -> None:
    email = _build_support_message(settings, name, reply_email, category, message)
    await send_email_message(settings, email)


def _build_support_message(
    settings: PlatformSettings,
    name: str,
    reply_email: str,
    category: str,
    message: str,
) -> EmailMessage:
    email = EmailMessage()
    email["From"] = email_sender(settings)
    email["To"] = settings.platform_support_recipient_email
    email["Reply-To"] = reply_email
    email["Subject"] = f"Old Sparky Arena: {CATEGORY_LABELS[category]}"
    email.set_content(
        f"Имя: {name}\nEmail: {reply_email}\nКатегория: {CATEGORY_LABELS[category]}\n\n{message}"
    )
    return email


def _send_email_message_sync(settings: PlatformSettings, email: EmailMessage) -> None:
    context = ssl.create_default_context()
    if settings.platform_support_smtp_ssl:
        client_context = smtplib.SMTP_SSL(
            settings.platform_support_smtp_host,
            settings.platform_support_smtp_port,
            timeout=10,
            context=context,
        )
    else:
        client_context = smtplib.SMTP(
            settings.platform_support_smtp_host,
            settings.platform_support_smtp_port,
            timeout=10,
        )
    with client_context as client:
        if settings.platform_support_smtp_starttls and not settings.platform_support_smtp_ssl:
            client.starttls(context=context)
        if settings.platform_support_smtp_username:
            client.login(
                settings.platform_support_smtp_username,
                settings.platform_support_smtp_password or "",
            )
        client.send_message(email)


async def _send_resend_message(
    settings: PlatformSettings,
    email: EmailMessage,
) -> None:
    recipients = [str(address) for address in email.get_all("To", [])]
    sender = str(email.get("From") or email_sender(settings))
    subject = str(email.get("Subject") or "")
    if not recipients or not sender or not subject:
        raise ValueError("Email sender, recipient and subject are required.")
    payload: dict[str, object] = {
        "from": sender,
        "to": recipients,
        "subject": subject,
        "text": email.get_content(),
    }
    reply_to = email.get("Reply-To")
    if reply_to:
        payload["reply_to"] = str(reply_to)
    headers = {
        "Authorization": f"Bearer {(settings.platform_resend_api_key or '').strip()}",
        "Content-Type": "application/json",
        "Idempotency-Key": f"oldsparky-{secrets.token_hex(16)}",
        "User-Agent": "OldSparky-Platform/1.0",
    }
    timeout = httpx.Timeout(settings.platform_resend_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers=headers,
            json=payload,
        )
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"Resend email delivery failed with HTTP {response.status_code}.")
