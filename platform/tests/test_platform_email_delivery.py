from __future__ import annotations

import unittest
from email.message import EmailMessage
from unittest.mock import patch

from apps.platform_api.app.services import support_mail
from python_packages.platform_infra.auth_lifecycle import email_delivery_configured
from python_packages.platform_infra.config import PlatformSettings
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class _Response:
    status_code = 200


class _ResendClient:
    def __init__(self) -> None:
        self.url = ""
        self.headers: dict[str, str] = {}
        self.payload: dict[str, object] = {}

    async def __aenter__(self) -> "_ResendClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
    ) -> _Response:
        self.url = url
        self.headers = headers
        self.payload = json
        return _Response()


class PlatformEmailDeliveryTests(PlatformIsolatedAsyncioTestCase):
    async def test_resend_delivery_uses_official_https_endpoint(self) -> None:
        settings = PlatformSettings(
            _env_file=None,
            platform_email_sender_email="support@old-sparky.com",
            platform_resend_api_key="replace-me-resend-key",
        )
        message = EmailMessage()
        message["From"] = "Old Sparky Arena <support@old-sparky.com>"
        message["To"] = "player@example.com"
        message["Reply-To"] = "reply@example.com"
        message["Subject"] = "Код подтверждения"
        message.set_content("Код: 123456")
        client = _ResendClient()

        with patch.object(support_mail.httpx, "AsyncClient", return_value=client):
            await support_mail.send_email_message(settings, message)

        self.assertTrue(email_delivery_configured(settings))
        self.assertEqual(client.url, "https://api.resend.com/emails")
        self.assertEqual(client.payload["to"], ["player@example.com"])
        self.assertEqual(client.payload["reply_to"], "reply@example.com")
        self.assertIn("123456", str(client.payload["text"]))
        self.assertEqual(client.headers["User-Agent"], "OldSparky-Platform/1.0")
        self.assertTrue(client.headers["Authorization"].startswith("Bearer "))
        self.assertTrue(client.headers["Idempotency-Key"].startswith("oldsparky-"))


if __name__ == "__main__":
    unittest.main()
