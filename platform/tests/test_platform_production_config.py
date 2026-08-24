from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from python_packages.platform_infra.config import (
    DEVELOPMENT_SECRET_KEY,
    PlatformSettings,
    parse_load_test_source_ips,
    validate_platform_settings,
)


class PlatformProductionConfigTests(unittest.TestCase):
    def production_settings(self, **overrides: object) -> PlatformSettings:
        values: dict[str, object] = {
            "platform_environment": "production",
            "platform_database_url": (
                "postgresql+asyncpg://platform_user:test@127.0.0.1:5432/platformdb"
            ),
            "platform_redis_url": "redis://127.0.0.1:6379/0",
            "platform_secret_key": "x" * 48,
            "platform_object_storage_backend": "r2",
            "platform_r2_endpoint_url": "https://account.r2.cloudflarestorage.com",
            "platform_r2_access_key_id": "test-access-key",
            "platform_r2_secret_access_key": "test-secret-key",
            "platform_r2_bucket_name": "test-bucket",
            "platform_media_public_base_url": "https://cdn.example.test",
            "platform_web_origin": "https://old-sparky.com",
            "platform_steam_callback_url": (
                "https://old-sparky.com/api/v1/auth/steam/callback"
            ),
            "platform_steam_login_enabled": True,
        }
        values.update(overrides)
        return PlatformSettings(_env_file=None, **values)

    def test_production_requires_explicit_secret(self) -> None:
        settings = self.production_settings(platform_secret_key=DEVELOPMENT_SECRET_KEY)

        with self.assertRaisesRegex(RuntimeError, "PLATFORM_SECRET_KEY"):
            validate_platform_settings(settings)

    def test_production_requires_complete_r2_configuration(self) -> None:
        settings = self.production_settings(platform_r2_secret_access_key=None)

        with self.assertRaisesRegex(RuntimeError, "PLATFORM_R2_SECRET_ACCESS_KEY"):
            validate_platform_settings(settings)

    def test_r2_endpoint_cannot_embed_bucket_path(self) -> None:
        settings = self.production_settings(
            platform_r2_endpoint_url="https://account.r2.cloudflarestorage.com/test-bucket"
        )

        with self.assertRaisesRegex(RuntimeError, "without a bucket path"):
            validate_platform_settings(settings)

    def test_complete_production_configuration_passes(self) -> None:
        validate_platform_settings(self.production_settings())

    def test_load_test_source_allowlist_accepts_exact_ipv4_and_ipv6_only(self) -> None:
        self.assertEqual(
            parse_load_test_source_ips("192.0.2.10, 2001:db8::10"),
            frozenset({"192.0.2.10", "2001:db8::10"}),
        )
        for value in ("192.0.2.0/24", "0.0.0.0", "127.0.0.1", "not-an-ip"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_load_test_source_ips(value)

    def test_production_rejects_invalid_load_test_source_allowlist(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "PLATFORM_LOAD_TEST_SOURCE_IPS"):
            validate_platform_settings(
                self.production_settings(platform_load_test_source_ips="0.0.0.0")
            )

    def test_worker_runtime_does_not_require_api_secret(self) -> None:
        settings = self.production_settings(
            platform_secret_key=DEVELOPMENT_SECRET_KEY,
        )
        with patch.dict(os.environ, {"PLATFORM_RUNTIME_SERVICE": "worker"}):
            validate_platform_settings(settings)
        with self.assertRaisesRegex(RuntimeError, "PLATFORM_SECRET_KEY"):
            validate_platform_settings(settings, require_api_secret=True)

    def test_production_bind_and_forwarded_trust_are_loopback_only(self) -> None:
        for overrides, expected in (
            ({"platform_api_host": "0.0.0.0"}, "API must bind"),
            ({"platform_web_bind_host": "0.0.0.0"}, "web must bind"),
            (
                {"platform_api_forwarded_allow_ips": "0.0.0.0"},
                "forwarded headers",
            ),
        ):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(
                RuntimeError, expected
            ):
                validate_platform_settings(self.production_settings(**overrides))

    def test_steam_callback_must_use_exact_public_origin_and_api_path(self) -> None:
        for callback_url in (
            None,
            "http://old-sparky.com/api/v1/auth/steam/callback",
            "https://evil.example/api/v1/auth/steam/callback",
            "https://old-sparky.com:8443/api/v1/auth/steam/callback",
            "https://old-sparky.com/alternate/callback",
            "https://old-sparky.com/api/v1/auth/steam/callback?next=evil",
        ):
            with self.subTest(callback_url=callback_url), self.assertRaisesRegex(
                RuntimeError,
                "PLATFORM_STEAM_CALLBACK_URL",
            ):
                validate_platform_settings(
                    self.production_settings(
                        platform_steam_callback_url=callback_url,
                    )
                )

    def test_disabled_steam_login_does_not_require_callback_configuration(self) -> None:
        validate_platform_settings(
            self.production_settings(
                platform_steam_login_enabled=False,
                platform_steam_callback_url=None,
            )
        )

    def test_staging_quota_cannot_be_smaller_than_one_upload(self) -> None:
        settings = self.production_settings(
            platform_media_max_input_bytes=6 * 1024 * 1024,
            platform_media_max_staged_bytes=5 * 1024 * 1024,
        )

        with self.assertRaisesRegex(RuntimeError, "MAX_STAGED_BYTES"):
            validate_platform_settings(settings)

    def test_test_runtime_requires_isolated_database_redis_and_storage(self) -> None:
        valid = PlatformSettings(
            _env_file=None,
            platform_environment="test",
            platform_database_url=(
                "postgresql+asyncpg://platform_test_user:test@127.0.0.1:5432/"
                "platformdb_test"
            ),
            platform_redis_url="redis://127.0.0.1:6379/15",
            platform_object_storage_backend="local",
        )
        validate_platform_settings(valid)

        for overrides, expected in (
            ({"platform_database_url": "postgresql+asyncpg://u:p@127.0.0.1/platformdb"}, "platformdb_test"),
            ({"platform_redis_url": "redis://127.0.0.1:6379/0"}, "Redis database 15"),
            ({"platform_object_storage_backend": "r2"}, "must not use production object storage"),
        ):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(RuntimeError, expected):
                validate_platform_settings(valid.model_copy(update=overrides))

    def test_non_test_runtime_cannot_target_test_or_legacy_bot_database(self) -> None:
        for database in ("platformdb_test", "sparkydb"):
            settings = PlatformSettings(
                _env_file=None,
                platform_environment="development",
                platform_database_url=f"postgresql+asyncpg://u:p@127.0.0.1/{database}",
            )
            with self.subTest(database=database), self.assertRaisesRegex(RuntimeError, "platformdb"):
                validate_platform_settings(settings)

    def test_alembic_validates_platform_target_before_using_database_url(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "alembic" / "env.py"
        ).read_text(encoding="utf-8")
        validation = source.index("validate_platform_settings(settings)")
        configure_url = source.index(
            'config.set_main_option("sqlalchemy.url", settings.platform_database_url)'
        )
        self.assertLess(validation, configure_url)


if __name__ == "__main__":
    unittest.main()
