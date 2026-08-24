import unittest
from pathlib import Path

from tools.platform_production_qa import (
    PollingMetricsRecorder,
    ProductionQa,
    fixed_polling_expectation,
    is_local_origin,
    normalize_path_for_slugs,
    polling_delay_seconds,
)


class ProductionQaPollingProfileTests(unittest.TestCase):
    def test_rostered_participant_requires_retained_scale_mode(self) -> None:
        kwargs = {
            "origin": "http://127.0.0.1",
            "report_path": Path("/tmp/platform-production-qa-test.json"),
            "browser_gate_dir": None,
            "browser_gate_timeout": 1.0,
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
            "browser_gate_dir": None,
            "browser_gate_timeout": 1.0,
            "http_timeout": 1.0,
            "control_participant_email": "owner@example.com",
        }

        with self.assertRaisesRegex(ValueError, "control-participant-state"):
            ProductionQa(keep_data=True, mode="scale", **kwargs)

        with self.assertRaisesRegex(ValueError, "mode scale and --keep-data"):
            ProductionQa(
                keep_data=False,
                mode="targeted",
                control_participant_email="owner@example.com",
                control_participant_state="assigned",
                **{key: value for key, value in kwargs.items() if key != "control_participant_email"},
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
            browser_gate_dir=None,
            browser_gate_timeout=1.0,
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

    def test_polling_metrics_group_events(self) -> None:
        recorder = PollingMetricsRecorder()
        recorder.mark(
            "scheduled",
            route="GET /tournaments/{slug}/workspace",
            role="participant",
            tournament_status="ready_check_active",
        )
        recorder.mark(
            "executed",
            route="GET /tournaments/{slug}/workspace",
            role="participant",
            tournament_status="ready_check_active",
        )
        recorder.mark(
            "skipped_hidden",
            route="GET /tournaments/{slug}/workspace",
            role="viewer",
            tournament_status="registration_open",
            hidden=True,
        )
        recorder.mark(
            "executed",
            route="GET /tournaments/{slug}",
            role="viewer",
            tournament_status="terminal",
            terminal_known=True,
        )

        summary = recorder.summary()

        self.assertEqual(summary["total_scheduled"], 1)
        self.assertEqual(summary["executed"], 2)
        self.assertEqual(summary["skipped_hidden"], 1)
        self.assertEqual(
            summary["by_route"]["GET /tournaments/{slug}/workspace"]["executed"],
            1,
        )
        self.assertEqual(
            summary["by_role"]["participant"]["total_scheduled"],
            1,
        )
        self.assertEqual(
            summary["by_tournament_status"]["terminal"]["executed"],
            1,
        )
        self.assertEqual(
            summary["executed_after_terminal"],
            {"GET /tournaments/{slug}": 1},
        )

    def test_fixed_polling_expectation_uses_route_labels(self) -> None:
        tabs = [
            {
                "route": "/tournaments/slug-a/workspace?participants_limit=0",
                "route_label": "GET /tournaments/{slug}/workspace",
                "role": "viewer",
                "tournament_status": "registration_open",
            },
            {
                "route": "/tournaments/slug-b/bracket?teams_view=summary",
                "route_label": "GET /tournaments/{slug}/bracket",
                "role": "participant",
                "tournament_status": "bracket_active",
            },
        ]

        expectation = fixed_polling_expectation(duration_seconds=30, tabs=tabs)

        self.assertEqual(expectation["ticks_per_tab"], 3)
        self.assertEqual(expectation["total_expected_gets"], 6)
        self.assertEqual(
            expectation["by_route"],
            {
                "GET /tournaments/{slug}/bracket": 3,
                "GET /tournaments/{slug}/workspace": 3,
            },
        )

    def test_polling_delay_zero_disables_polling(self) -> None:
        self.assertIsNone(polling_delay_seconds(0, tab_index=1, tick=1))
        self.assertGreater(
            polling_delay_seconds(None, tab_index=1, tick=1) or 0,
            0,
        )

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
