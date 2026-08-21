from __future__ import annotations

import unittest

from tools import platform_update_shared_env as update_env


class PlatformUpdateSharedEnvTests(unittest.TestCase):
    def test_only_whitelisted_values_are_merged_without_output_values(self) -> None:
        updates = update_env.read_updates(
            '{"PLATFORM_ENVIRONMENT":"production",'
            '"PLATFORM_TURNSTILE_MODE":"always",'
            '"PLATFORM_STEAM_LOGIN_ENABLED":"true",'
            '"PLATFORM_RESEND_API_KEY":"replace-me-private"}'
        )
        content, changed = update_env.merge_updates(
            [
                "PLATFORM_DATABASE_URL=example",
                "PLATFORM_ENVIRONMENT=development",
            ],
            updates,
        )

        self.assertIn("PLATFORM_DATABASE_URL=example", content)
        self.assertIn("PLATFORM_ENVIRONMENT=production", content)
        self.assertIn("PLATFORM_RESEND_API_KEY=replace-me-private", content)
        self.assertEqual(
            changed,
            [
                "PLATFORM_ENVIRONMENT",
                "PLATFORM_RESEND_API_KEY",
                "PLATFORM_STEAM_LOGIN_ENABLED",
                "PLATFORM_TURNSTILE_MODE",
            ],
        )

    def test_rejects_unknown_multiline_and_unreviewed_fixed_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "not allowed"):
            update_env.read_updates('{"PLATFORM_DATABASE_URL":"unsafe"}')
        with self.assertRaisesRegex(ValueError, "invalid"):
            update_env.read_updates('{"PLATFORM_RESEND_API_KEY":"line\\nbreak"}')
        with self.assertRaisesRegex(ValueError, "reviewed value"):
            update_env.read_updates('{"PLATFORM_ENVIRONMENT":"development"}')
        with self.assertRaisesRegex(ValueError, "invalid reviewed value"):
            update_env.read_updates('{"PLATFORM_STEAM_LOGIN_ENABLED":"yes"}')


if __name__ == "__main__":
    unittest.main()
