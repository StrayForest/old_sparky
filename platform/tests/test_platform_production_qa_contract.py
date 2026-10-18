import asyncio
from contextlib import nullcontext
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from tools.platform_prepare_external_vote_fixture import (
    FIXTURE_TIMING_STAGES,
    FixtureStageTimings,
    prepare,
    project_fixture_timing_summary,
    write_fixture_timing_receipt,
)
from tools.platform_production_qa import (
    ProductionQa,
    is_local_origin,
    normalize_path_for_slugs,
    write_burst_tournament_name,
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

    def test_write_burst_tournament_names_are_unique_and_within_public_limit(self) -> None:
        marker = "preprod2609012009294e7a"
        names = {
            write_burst_tournament_name(marker, index, "external_ready")
            for index in range(1, 41)
        }

        self.assertEqual(len(names), 40)
        self.assertTrue(all(len(name) <= 25 for name in names))
        self.assertTrue(all(marker[-10:] in name for name in names))

    def test_external_fixture_stage_timings_use_monotonic_elapsed_fields(self) -> None:
        self.assertEqual(
            FIXTURE_TIMING_STAGES,
            (
                "bulk_user_creation",
                "permission_grants",
                "tournament_api_setup",
                "ready_check_starts",
                "fixture_finalization",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt_path = root / "fixture-timing.json"
            report_path = root / "external-vote.json"
            manifest_path = root / "manifest.json"
            events: list[str] = []

            class FakeClient:
                async def aclose(self) -> None:
                    events.append("client_closed")

            class FakeQa:
                def __init__(self, **_kwargs: object) -> None:
                    self.report: dict[str, object] = {"marker": "private-marker"}
                    self.marker = "private-marker"
                    self.session_cookie_name = "session"
                    self.csrf_cookie_name = "csrf"
                    self.session_tokens_by_user_id = {
                        str(index): f"session-{index}" for index in range(14)
                    }
                    self.csrf_tokens_by_user_id = {
                        str(index): f"csrf-{index}" for index in range(14)
                    }
                    self.tournament_ids: list[str] = []

                async def new_client(self, _origin: str) -> FakeClient:
                    events.append("new_client")
                    return FakeClient()

                async def record_preprod_run(self, *, status: str, **_kwargs: object) -> None:
                    events.append(f"record_{status}")

                async def bulk_register_scale_users(self) -> list[dict[str, str]]:
                    events.append("bulk_user_creation")
                    return [{"id": str(index)} for index in range(14)]

                def preseed_csrf_tokens(self, _users: list[dict[str, str]]) -> None:
                    events.append("csrf_preseed")

                def scenario(self, name: str, _passed: bool, _details: object) -> None:
                    events.append(name)

                async def grant_tournament_permissions(
                    self, _organizers: list[dict[str, str]], _users: list[dict[str, str]]
                ) -> None:
                    events.append("permission_grants")

                def phase(self, _name: str):
                    return nullcontext()

                async def prepare_ready_burst_tournament(
                    self,
                    _client: FakeClient,
                    *,
                    organizer: dict[str, str],
                    **_kwargs: object,
                ) -> dict[str, object]:
                    events.append("tournament_api_setup")
                    self.tournament_ids = ["tournament-1"]
                    return {
                        "id": "tournament-1",
                        "slug": "fixture-slug",
                        "organizer": organizer,
                    }

                async def request_as(self, *_args: object, **_kwargs: object) -> None:
                    events.append("ready_check_starts")

            args = SimpleNamespace(
                env_file=root / "env",
                origin="https://old-sparky.com",
                local_origin="http://127.0.0.1:8010",
                report_path=report_path,
                manifest_path=manifest_path,
                tournament_count=1,
                users_per_tournament=14,
                concurrency=1,
                http_timeout=1.0,
                runner_sha=None,
                source_binding_base64=None,
            )
            with (
                patch("tools.platform_prepare_external_vote_fixture.os.geteuid", return_value=0),
                patch("tools.platform_prepare_external_vote_fixture.load_env_file"),
                patch("tools.platform_prepare_external_vote_fixture.ProductionQa", FakeQa),
                patch(
                    "tools.platform_prepare_external_vote_fixture.time.perf_counter",
                    side_effect=(10.0, 11.25, 20.0, 22.0, 30.0, 33.0, 40.0, 44.0, 50.0, 56.0),
                ),
            ):
                report = asyncio.run(prepare(args))

            expected_events = [
                "new_client",
                "record_running",
                "bulk_user_creation",
                "csrf_preseed",
                "external_vote_fixture_users_created",
                "permission_grants",
                "tournament_api_setup",
                "ready_check_starts",
                "external_vote_fixture_ready_rounds_created",
                "record_passed",
                "client_closed",
            ]
            self.assertEqual(events, expected_events)
            self.assertTrue(report["passed"])
            self.assertEqual(os.stat(receipt_path).st_mode & 0o777, 0o600)
            self.assertEqual(
                json.loads(receipt_path.read_text(encoding="utf-8")),
                {
                    "schema": 1,
                    "clock": "time.perf_counter",
                    "stages": {
                        "bulk_user_creation": {"duration_ms": 1250.0, "completed": True},
                        "permission_grants": {"duration_ms": 2000.0, "completed": True},
                        "tournament_api_setup": {"duration_ms": 3000.0, "completed": True},
                        "ready_check_starts": {"duration_ms": 4000.0, "completed": True},
                        "fixture_finalization": {"duration_ms": 6000.0, "completed": True},
                    },
                },
            )

            source_summary = {
                "mode": "write-burst",
                "control_account_preserved": None,
                "planned_tournaments": 1,
                "completed_tournaments": 1,
                "planned_users": 14,
                "completed_users": 14,
                "passed": True,
                "external_vote_fixture": {
                    "report_path": "/private/path/external-vote.json",
                    "marker": "private-marker",
                },
                "rows": [
                    {
                        "synthetic_users": 14,
                        "report_path": "/private/path/external-vote.json",
                        "result": {
                            "passed": True,
                            "marker": "private-marker",
                            "report_path": "/private/path/external-vote.json",
                        },
                    }
                ],
            }
            source_summary_path = root / "source-matrix-summary.json"
            source_summary_path.write_text(json.dumps(source_summary), encoding="utf-8")
            source_summary_path.chmod(0o600)
            summary_path = root / "matrix-summary.json"
            project_fixture_timing_summary(
                receipt_path,
                source_summary_path,
                summary_path,
                qa_succeeded=True,
            )
            projected = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertIsNone(projected["control_account_preserved"])
            timing_projection = projected["external_vote_fixture"]["fixture_setup_timings"]
            self.assertEqual(
                set(projected),
                {
                    "schema",
                    "mode",
                    "control_account_preserved",
                    "planned_tournaments",
                    "completed_tournaments",
                    "planned_users",
                    "completed_users",
                    "passed",
                    "rows",
                    "external_vote_fixture",
                },
            )
            self.assertEqual(
                timing_projection,
                json.loads(receipt_path.read_text(encoding="utf-8")),
            )
            self.assertNotIn("private-marker", summary_path.read_text(encoding="utf-8"))
            self.assertNotIn("/private/path", summary_path.read_text(encoding="utf-8"))

            malformed = json.loads(receipt_path.read_text(encoding="utf-8"))
            invalid_cases = [
                {**malformed, "unexpected": "private"},
                {
                    **malformed,
                    "stages": {
                        **malformed["stages"],
                        "bulk_user_creation": {"duration_ms": float("nan"), "completed": True},
                    },
                },
                {
                    **malformed,
                    "stages": {
                        **malformed["stages"],
                        "bulk_user_creation": {"duration_ms": float("inf"), "completed": True},
                    },
                },
                {
                    **malformed,
                    "stages": {
                        **malformed["stages"],
                        "bulk_user_creation": {"duration_ms": -1, "completed": True},
                    },
                },
                {
                    **malformed,
                    "stages": {
                        **malformed["stages"],
                        "bulk_user_creation": {"duration_ms": True, "completed": True},
                    },
                },
                {
                    **malformed,
                    "stages": {
                        **malformed["stages"],
                        "bulk_user_creation": {
                            "duration_ms": 1,
                            "completed": True,
                            "token": "private",
                        },
                    },
                },
            ]
            for invalid in invalid_cases:
                with self.subTest(invalid=invalid):
                    receipt_path.write_text(json.dumps(invalid, allow_nan=True), encoding="utf-8")
                    receipt_path.chmod(0o600)
                    with self.assertRaises(ValueError):
                        project_fixture_timing_summary(
                            receipt_path,
                            source_summary_path,
                            summary_path,
                            qa_succeeded=True,
                        )

            receipt_path.write_text(json.dumps(malformed), encoding="utf-8")
            receipt_path.chmod(0o600)
            for invalid_preservation in ("unknown", 0, []):
                source_summary_path.write_text(
                    json.dumps({**source_summary, "control_account_preserved": invalid_preservation}),
                    encoding="utf-8",
                )
                source_summary_path.chmod(0o600)
                with self.subTest(control_account_preserved=invalid_preservation), self.assertRaises(ValueError):
                    project_fixture_timing_summary(
                        receipt_path,
                        source_summary_path,
                        summary_path,
                        qa_succeeded=True,
                    )

            source_summary_path.write_text(
                json.dumps({**source_summary, "control_account_preserved": False}),
                encoding="utf-8",
            )
            source_summary_path.chmod(0o600)
            project_fixture_timing_summary(
                receipt_path,
                source_summary_path,
                summary_path,
                qa_succeeded=True,
            )
            self.assertIs(
                json.loads(summary_path.read_text(encoding="utf-8"))["control_account_preserved"],
                False,
            )

            source_summary_path.write_text(
                json.dumps({key: value for key, value in source_summary.items() if key != "control_account_preserved"}),
                encoding="utf-8",
            )
            source_summary_path.chmod(0o600)
            project_fixture_timing_summary(
                receipt_path,
                source_summary_path,
                summary_path,
                qa_succeeded=True,
            )
            self.assertIsNone(
                json.loads(summary_path.read_text(encoding="utf-8"))["control_account_preserved"]
            )

            partial_timings = FixtureStageTimings()
            with patch(
                "tools.platform_prepare_external_vote_fixture.time.perf_counter",
                side_effect=(100.0, 100.5),
            ):
                with self.assertRaisesRegex(RuntimeError, "stage failed"):
                    with partial_timings.measure("tournament_api_setup"):
                        raise RuntimeError("stage failed")
            write_fixture_timing_receipt(receipt_path, partial_timings)
            source_summary_path.write_text(
                json.dumps({**source_summary, "passed": False}),
                encoding="utf-8",
            )
            source_summary_path.chmod(0o600)
            project_fixture_timing_summary(
                receipt_path,
                source_summary_path,
                summary_path,
                qa_succeeded=False,
            )
            partial = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(
                partial["external_vote_fixture"]["fixture_setup_timings"]["stages"],
                {"tournament_api_setup": {"duration_ms": 500.0, "completed": False}},
            )

    def test_tournament_lifecycle_uses_the_current_workspace_and_profile_flow(self) -> None:
        qa = ProductionQa(
            origin="http://127.0.0.1",
            request_origin="https://old-sparky.com",
            report_path=Path("/tmp/platform-production-qa-lifecycle-test.json"),
            http_timeout=1.0,
            keep_data=True,
            mode="tournament-lifecycle",
            lifecycle_tournament_count=2,
            lifecycle_users_per_tournament=28,
            lifecycle_teams_count=4,
        )

        self.assertEqual(qa.scale_users, 56)
        self.assertEqual(qa.lifecycle_teams_count, 4)
        self.assertTrue(qa.collect_performance)
        self.assertEqual(qa.request_origin, "https://old-sparky.com")

        source = Path(ProductionQa.run_tournament_lifecycle.__code__.co_filename).read_text()
        self.assertIn('workspace?participants_limit=0"', source)
        self.assertIn('&participants_offset=0&workspace_view=detail&include_current_user=false', source)
        self.assertIn('&participants_offset=0&workspace_view=bracket&include_current_user=false', source)
        self.assertIn("/profiles/{target_user_id}", source)
        self.assertIn("/bracket?teams_view=summary", source)
        self.assertIn("/matches/seed-opening-round", source)
        self.assertIn("/matches/seed-next-round", source)
        self.assertIn("/matches/{match['id']}/report", source)
        self.assertIn('"If-None-Match": etag', source)
        lifecycle_source = source[source.index("async def run_tournament_lifecycle") :]
        self.assertNotIn('f"/tournaments/{tournament[\'slug\']}/participants', lifecycle_source)


if __name__ == "__main__":
    unittest.main()
