from __future__ import annotations

from pathlib import Path
import unittest

from tools.platform_render_service_envs import (
    WEB_FORBIDDEN_KEYS,
    WORKER_FORBIDDEN_KEYS,
    render_service_env,
)

PLATFORM_ROOT = Path(__file__).resolve().parents[1]


class RuntimeIsolationTests(unittest.TestCase):
    def test_web_env_never_receives_backend_credentials(self) -> None:
        values = {
            "PLATFORM_ENVIRONMENT": "PLATFORM_ENVIRONMENT=production",
            "PLATFORM_API_PORT": "PLATFORM_API_PORT=8010",
            "PLATFORM_WEB_BIND_HOST": "PLATFORM_WEB_BIND_HOST=127.0.0.1",
            "PLATFORM_DATABASE_URL": "PLATFORM_DATABASE_URL=postgresql://secret",
            "PLATFORM_SECRET_KEY": "PLATFORM_SECRET_KEY=secret",
            "PLATFORM_REDIS_URL": "PLATFORM_REDIS_URL=redis://secret",
            "PLATFORM_TURNSTILE_SECRET_KEY": "PLATFORM_TURNSTILE_SECRET_KEY=secret",
            "PLATFORM_RESEND_API_KEY": "PLATFORM_RESEND_API_KEY=secret",
            "PLATFORM_R2_SECRET_ACCESS_KEY": "PLATFORM_R2_SECRET_ACCESS_KEY=secret",
            "PLATFORM_OPENAI_API_KEY": "PLATFORM_OPENAI_API_KEY=secret",
        }
        rendered = render_service_env("web", values)
        for key in WEB_FORBIDDEN_KEYS:
            self.assertNotIn(f"{key}=", rendered)
        self.assertIn("PLATFORM_ENVIRONMENT=production", rendered)
        self.assertIn("PLATFORM_API_PORT=8010", rendered)

    def test_worker_env_excludes_auth_and_delivery_secrets(self) -> None:
        values = {
            "PLATFORM_ENVIRONMENT": "PLATFORM_ENVIRONMENT=production",
            "PLATFORM_DATABASE_URL": "PLATFORM_DATABASE_URL=postgresql://worker",
            "PLATFORM_REDIS_URL": "PLATFORM_REDIS_URL=redis://worker",
            "PLATFORM_CELERY_BROKER_URL": "PLATFORM_CELERY_BROKER_URL=redis://broker",
            "PLATFORM_R2_SECRET_ACCESS_KEY": "PLATFORM_R2_SECRET_ACCESS_KEY=r2-secret",
            "PLATFORM_OPENAI_API_KEY": "PLATFORM_OPENAI_API_KEY=openai-secret",
            "PLATFORM_SECRET_KEY": "PLATFORM_SECRET_KEY=session-secret",
            "PLATFORM_TURNSTILE_SECRET_KEY": "PLATFORM_TURNSTILE_SECRET_KEY=turnstile-secret",
            "PLATFORM_RESEND_API_KEY": "PLATFORM_RESEND_API_KEY=resend-secret",
        }
        rendered = render_service_env("worker", values)
        for key in WORKER_FORBIDDEN_KEYS:
            self.assertNotIn(f"{key}=", rendered)
        self.assertIn("PLATFORM_DATABASE_URL=", rendered)
        self.assertIn("PLATFORM_R2_SECRET_ACCESS_KEY=", rendered)
        self.assertIn("PLATFORM_OPENAI_API_KEY=", rendered)

    def test_systemd_units_use_distinct_identities_and_envs(self) -> None:
        expected = {
            "deadlock-web.service": ("oldsparky-web", "web"),
            "deadlock-api.service": ("oldsparky-api", "api"),
            "deadlock-worker.service": ("oldsparky-worker", "worker"),
        }
        for filename, (identity, service) in expected.items():
            unit = (PLATFORM_ROOT / "deploy" / "systemd" / filename).read_text()
            self.assertIn(f"User={identity}\n", unit)
            self.assertIn(f"Group={identity}\n", unit)
            self.assertIn(
                f"Environment=PLATFORM_ENV_FILE=/opt/oldsparky/platform/shared/env/{service}.env",
                unit,
            )
            self.assertIn(f"Environment=PLATFORM_RUNTIME_SERVICE={service}", unit)
            self.assertIn("ProtectProc=invisible", unit)
            self.assertNotIn(
                "Environment=PLATFORM_ENV_FILE=/opt/oldsparky/platform/shared/.env.platform",
                unit,
            )

    def test_only_api_and_worker_share_media_staging_group(self) -> None:
        api = (PLATFORM_ROOT / "deploy/systemd/deadlock-api.service").read_text()
        worker = (PLATFORM_ROOT / "deploy/systemd/deadlock-worker.service").read_text()
        web = (PLATFORM_ROOT / "deploy/systemd/deadlock-web.service").read_text()
        self.assertIn("SupplementaryGroups=oldsparky-media", api)
        self.assertIn("SupplementaryGroups=oldsparky-media", worker)
        self.assertNotIn("oldsparky-media", web)
        self.assertIn("UMask=0007", api)
        self.assertIn("UMask=0007", worker)


if __name__ == "__main__":
    unittest.main()
