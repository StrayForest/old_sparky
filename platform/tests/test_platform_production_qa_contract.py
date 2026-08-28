import unittest
from pathlib import Path
from uuid import uuid4

from tools.platform_production_qa import (
    ProductionQa,
    is_local_origin,
    normalize_path_for_slugs,
)


class ProductionQaContractTests(unittest.TestCase):
    def test_rostered_participant_requires_retained_scale_mode(self) -> None:
        kwargs = {
            "origin": "http://127.0.0.1",
            "report_path": Path("/tmp/platform-production-qa-test.json"),
            "http_timeout": 1.0,
            "rostered_participant_email": "owner@example.com",
        }

        with self.assertRaisesRegex(ValueError, "--mode scale and --keep-data"):
            ProductionQa(keep_data=False, mode="targeted", **kwargs)

        qa = ProductionQa(keep_data=True, mode="scale", **kwargs)

        self.assertEqual(qa.rostered_participant_email, "owner@example.com")

    def test_control_participant_requires_state_and_retained_scale_mode(self) -> None:
        kwargs = {
            "origin": "http://127.0.0.1",
            "report_path": Path("/tmp/platform-production-qa-control-test.json"),
            "http_timeout": 1.0,
            "control_participant_email": "owner@example.com",
        }

        with self.assertRaisesRegex(ValueError, "control-participant-state"):
            ProductionQa(keep_data=True, mode="scale", **kwargs)

        with self.assertRaisesRegex(ValueError, "mode scale and --keep-data"):
            ProductionQa(
                keep_data=False,
                mode="targeted",
                control_participant_state="assigned",
                **kwargs,
            )

        qa = ProductionQa(
            keep_data=True,
            mode="scale",
            control_participant_state="assigned",
            tournament_visibility="invite_only",
            profile_journey=True,
            **kwargs,
        )
        self.assertEqual(qa.tournament_visibility, "invite_only")
        self.assertTrue(qa.profile_journey)
        self.assertEqual(qa.control_participant_state, "assigned")

    def test_profile_payloads_have_changeable_general_deadlock_and_captain_data(self) -> None:
        qa = ProductionQa(
            origin="http://127.0.0.1",
            report_path=Path("/tmp/platform-production-qa-profile-test.json"),
            http_timeout=1.0,
            keep_data=True,
            mode="scale",
            scale_users=20,
            scale_teams=2,
        )
        user = {"id": "user", "label": "p", "profile_index": 0}
        initial = qa._profile_write_payloads(user, changed=False)
        changed = qa._profile_write_payloads(user, changed=True)

        self.assertNotEqual(initial[0], changed[0])
        self.assertNotEqual(initial[1], changed[1])
        self.assertNotEqual(initial[2], changed[2])
        self.assertEqual(len(initial[2]["slots"]), 6)

    def test_read_mix_models_manual_page_refreshes_without_background_refresh(self) -> None:
        qa = ProductionQa(
            origin="http://127.0.0.1",
            report_path=Path("/tmp/platform-production-qa-read-mix-test.json"),
            http_timeout=1.0,
            keep_data=True,
            mode="read-mix",
            scale_users=20,
            scale_site_mix_users=20,
            scale_bracket_view_users=20,
        )

        self.assertEqual(qa.scale_users, 20)
        self.assertEqual(qa.scale_site_mix_users, 20)
        self.assertEqual(qa.scale_bracket_view_users, 20)
        self.assertTrue(qa.marker.startswith("preprod"))

        source = Path(ProductionQa.run_scale_site_mix.__code__.co_filename).read_text()
        self.assertIn("manual_workspace_refresh", source)
        self.assertIn("manual_bracket_refresh", source)
        self.assertIn("If-None-Match", source)

    def test_preprod_progress_snapshot_bounds_large_identity_lists(self) -> None:
        qa = ProductionQa(
            origin="http://127.0.0.1",
            report_path=Path("/tmp/platform-production-qa-progress-test.json"),
            http_timeout=1.0,
            keep_data=True,
            mode="scale",
        )
        user_ids = [str(uuid4()) for _ in range(100)]
        qa.user_ids.extend(user_ids)
        qa.report["user_ids"] = qa.user_ids

        progress = qa._preprod_report_snapshot(progress=True)
        final = qa._preprod_report_snapshot(progress=False)

        self.assertEqual(progress["user_ids"]["count"], 100)
        self.assertEqual(progress["user_ids"]["first"], user_ids[:4])
        self.assertEqual(progress["user_ids"]["last"], user_ids[-4:])
        self.assertTrue(
            progress["fixture_progress"][
                "exact_identity_report_deferred_until_phase_completion"
            ]
        )
        self.assertEqual(final["user_ids"], user_ids)

    def test_origin_and_many_slug_normalization(self) -> None:
        self.assertTrue(is_local_origin("http://127.0.0.1"))
        self.assertTrue(is_local_origin("http://localhost:3000"))
        self.assertFalse(is_local_origin("https://example.com"))
        self.assertEqual(
            normalize_path_for_slugs(
                "/tournaments/first-slug/bracket?teams_view=summary",
                tournament_slug=None,
                tournament_slugs=["first-slug", "second-slug"],
            ),
            "/tournaments/{slug}/bracket",
        )


if __name__ == "__main__":
    unittest.main()
