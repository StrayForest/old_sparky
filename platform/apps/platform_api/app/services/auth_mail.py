from __future__ import annotations

from email.message import EmailMessage

from apps.platform_api.app.services.support_mail import send_email_message
from python_packages.platform_infra.auth_lifecycle import email_sender
from python_packages.platform_infra.config import PlatformSettings


async def send_password_reset_email(
    settings: PlatformSettings,
    *,
    recipient_email: str,
    code: str,
) -> None:
    message = EmailMessage()
    message["From"] = email_sender(settings)
    message["To"] = recipient_email
    message["Subject"] = "Код восстановления Old Sparky Arena"
    message.set_content(
        "Код для восстановления пароля Old Sparky Arena:\n\n"
        f"{code}\n\n"
        f"Код действует {settings.platform_password_reset_ttl_minutes} минут. "
        "Если вы не запрашивали сброс, проигнорируйте письмо."
    )
    await send_email_message(settings, message)


async def send_email_verification_email(
    settings: PlatformSettings,
    *,
    recipient_email: str,
    code: str,
) -> None:
    message = EmailMessage()
    message["From"] = email_sender(settings)
    message["To"] = recipient_email
    message["Subject"] = "Код подтверждения Old Sparky Arena"
    message.set_content(
        "Код подтверждения email для Old Sparky Arena:\n\n"
        f"{code}\n\n"
        f"Код действует {settings.platform_email_verification_ttl_minutes} минут. "
        "Если вы не создавали аккаунт, проигнорируйте письмо."
    )
    await send_email_message(settings, message)
