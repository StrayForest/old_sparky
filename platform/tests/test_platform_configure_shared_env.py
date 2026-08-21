from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools import platform_configure_shared_env as configure


class PlatformConfigureSharedEnvTests(unittest.TestCase):
    def test_merge_preserves_credentials_and_environment(self) -> None:
        lines = [
            "PLATFORM_ENVIRONMENT=development",
            "PLATFORM_DATABASE_URL=postgresql://user:secret@127.0.0.1/platformdb",
            "PLATFORM_SECRET_KEY=existing-secret",
            "PLATFORM_R2_ACCESS_KEY_ID=existing-access",
            "PLATFORM_R2_SECRET_ACCESS_KEY=existing-r2-secret",
            "PLATFORM_WEB_ORIGIN=http://127.0.0.1:3000",
            "PLATFORM_WEB_ORIGIN=http://duplicate.invalid",
            "PLATFORM_TURNSTILE_MODE=off",
            "PLATFORM_STEAM_LOGIN_ENABLED=true",
        ]
        content, changed = configure.merge_baseline(lines)
        self.assertIn("PLATFORM_ENVIRONMENT=development", content)
        self.assertIn("PLATFORM_SECRET_KEY=existing-secret", content)
        self.assertIn("PLATFORM_R2_SECRET_ACCESS_KEY=existing-r2-secret", content)
        self.assertIn("PLATFORM_TURNSTILE_MODE=off", content)
        self.assertIn("PLATFORM_WEB_ORIGIN=https://old-sparky.com", content)
        self.assertIn(
            "PLATFORM_EMAIL_SENDER_EMAIL='Old Sparky Arena <noreply@auth.old-sparky.com>'",
            content,
        )
        self.assertEqual(content.count("PLATFORM_WEB_ORIGIN="), 1)
        self.assertIn("PLATFORM_MEDIA_PUBLIC_BASE_URL", changed)
        self.assertIn("PLATFORM_STEAM_LOGIN_ENABLED=true", content)
        self.assertIn(
            "PLATFORM_STEAM_CALLBACK_URL=https://old-sparky.com/api/v1/auth/steam/callback",
            content,
        )
        self.assertIn("PLATFORM_AUTH_DELIVERY_COOLDOWN_SECONDS=60", content)

    def test_merge_defaults_new_and_invalid_steam_rollout_flags_to_disabled(self) -> None:
        missing_content, _changed = configure.merge_baseline([])
        invalid_content, changed = configure.merge_baseline(
            ["PLATFORM_STEAM_LOGIN_ENABLED=unexpected"]
        )

        self.assertIn("PLATFORM_STEAM_LOGIN_ENABLED=false", missing_content)
        self.assertIn("PLATFORM_STEAM_LOGIN_ENABLED=false", invalid_content)
        self.assertIn("PLATFORM_STEAM_LOGIN_ENABLED", changed)

    def test_atomic_write_preserves_private_owner_group_and_mode(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env.platform"
            path.write_text("OLD=value\n", encoding="utf-8")
            path.chmod(0o640)
            metadata = path.stat()
            configure.atomic_write(path, "NEW=value\n", metadata)
            updated = path.stat()
            self.assertEqual(path.read_text(encoding="utf-8"), "NEW=value\n")
            self.assertEqual(updated.st_uid, os.geteuid())
            self.assertEqual(updated.st_gid, metadata.st_gid)
            self.assertEqual(updated.st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
