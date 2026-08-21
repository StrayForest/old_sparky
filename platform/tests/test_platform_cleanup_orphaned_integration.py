from __future__ import annotations

from types import SimpleNamespace
import unittest

from tools import platform_cleanup_orphaned_integration as cleanup


class PlatformCleanupOrphanedIntegrationTests(unittest.TestCase):
    def test_inventory_contract_accepts_only_bounded_test_identities(self) -> None:
        marker = "it-deadlock-2077c391"
        users = [
            SimpleNamespace(
                id="test-user-id",
                email=f"{marker}-player01@example.com",
                display_name="test-player01",
            )
        ]
        tournaments = [
            SimpleNamespace(
                slug=f"{marker}-a",
                organizer_user_id="test-user-id",
            ),
            SimpleNamespace(
                slug="it-deadlock-2077-wait",
                organizer_user_id="test-user-id",
            ),
        ]
        cleanup.validate_inventory(marker, users, tournaments)

        users[0].email = "real-user@example.com"
        with self.assertRaises(RuntimeError):
            cleanup.validate_inventory(marker, users, tournaments)

    def test_inventory_contract_refuses_bounds_and_non_test_display_name(self) -> None:
        marker = "it-deadlock-2077c391"
        too_many = [
            SimpleNamespace(
                id=f"test-user-{index}",
                email=f"{marker}-player{index:02d}@example.com",
                display_name=f"test-player{index:02d}",
            )
            for index in range(cleanup.MAX_USERS + 1)
        ]
        with self.assertRaisesRegex(RuntimeError, "hard safety bound"):
            cleanup.validate_inventory(marker, too_many, [])

        with self.assertRaisesRegex(RuntimeError, "identity contract"):
            cleanup.validate_inventory(
                marker,
                [
                    SimpleNamespace(
                        id="test-user-id",
                        email=f"{marker}-player01@example.com",
                        display_name="Real Player",
                    )
                ],
                [],
            )


if __name__ == "__main__":
    unittest.main()
