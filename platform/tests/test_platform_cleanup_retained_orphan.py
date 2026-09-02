from __future__ import annotations

from types import SimpleNamespace
import unittest

from tools import platform_cleanup_retained_orphan as cleanup


class RetainedOrphanCleanupTests(unittest.TestCase):
    def _run(self, **overrides: object) -> SimpleNamespace:
        marker = "preprod260829000001abcd"
        mode = "read-mix"
        report_path = (
            "/opt/oldsparky/platform/shared/production-retained-matrix/"
            "gha-12345/read-mix/read-mix.json"
        )
        report = {
            "marker": marker,
            "origin": cleanup.EXPECTED_ORIGIN,
            "request_origin": cleanup.EXPECTED_ORIGIN,
            "mode": mode,
            "report_path": report_path,
            "user_ids": ["00000000-0000-0000-0000-000000000001"],
            "tournament_ids": [],
        }
        values = {
            "marker": marker,
            "origin": cleanup.EXPECTED_ORIGIN,
            "report_path": report_path,
            "report": report,
            "status": "running",
            "cleanup_state": {},
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_builds_manifest_from_one_exact_durable_row(self) -> None:
        manifest = cleanup.build_durable_manifest(
            self._run(),
            load_run_id="12345",
            control_email="Control@example.com",
        )
        self.assertEqual(manifest["control_email"], "control@example.com")
        self.assertEqual(manifest["markers"], {"preprod260829000001abcd"})
        self.assertEqual(len(manifest["user_ids"]), 1)
        self.assertEqual(manifest["rows"][0]["report_path"], self._run().report_path)

    def test_refuses_row_with_noncanonical_report_path(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "report path"):
            cleanup.build_durable_manifest(
                self._run(report_path="/tmp/not-a-retained-run.json"),
                load_run_id="12345",
                control_email="control@example.com",
            )

    def test_builds_manifest_for_legacy_external_vote_report(self) -> None:
        marker = "preprod260829000001abcd"
        report_path = cleanup._legacy_external_vote_report_path(run_id="12345")
        run = self._run(
            mode="write-burst",
            report_path=report_path,
            report={
                "marker": marker,
                "origin": cleanup.EXPECTED_ORIGIN,
                "request_origin": cleanup.EXPECTED_ORIGIN,
                "mode": "write-burst",
                "report_path": report_path,
                "external_vote": {"tournament_count": 11},
                "user_ids": ["00000000-0000-0000-0000-000000000001"],
                "tournament_ids": [],
            },
        )

        manifest = cleanup.build_durable_manifest(
            run,
            load_run_id="12345",
            control_email="control@example.com",
        )

        self.assertEqual(manifest["mode"], "write-burst")
        self.assertEqual(manifest["rows"][0]["report_path"], report_path)

    def test_legacy_external_vote_path_requires_report_metadata(self) -> None:
        report_path = cleanup._legacy_external_vote_report_path(run_id="12345")
        with self.assertRaisesRegex(RuntimeError, "report path"):
            cleanup.build_durable_manifest(
                self._run(
                    mode="write-burst",
                    report_path=report_path,
                    report={
                        "marker": "preprod260829000001abcd",
                        "origin": cleanup.EXPECTED_ORIGIN,
                        "request_origin": cleanup.EXPECTED_ORIGIN,
                        "mode": "write-burst",
                        "report_path": report_path,
                        "user_ids": ["00000000-0000-0000-0000-000000000001"],
                        "tournament_ids": [],
                    },
                ),
                load_run_id="12345",
                control_email="control@example.com",
            )

    def test_compact_inventory_requires_explicit_resolved_ids(self) -> None:
        report_path = cleanup._legacy_external_vote_report_path(run_id="12345")
        compact = {
            "count": 2,
            "first": ["00000000-0000-0000-0000-000000000001"],
            "last": ["00000000-0000-0000-0000-000000000002"],
            "complete_inventory_in_final_report": True,
        }
        run = self._run(
            mode="write-burst",
            report_path=report_path,
            report={
                "marker": "preprod260829000001abcd",
                "origin": cleanup.EXPECTED_ORIGIN,
                "request_origin": cleanup.EXPECTED_ORIGIN,
                "mode": "write-burst",
                "report_path": report_path,
                "external_vote": {"tournament_count": 11},
                "user_ids": compact,
                "tournament_ids": [],
            },
        )

        with self.assertRaises(ValueError):
            cleanup.build_durable_manifest(
                run,
                load_run_id="12345",
                control_email="control@example.com",
            )

        manifest = cleanup.build_durable_manifest(
            run,
            load_run_id="12345",
            control_email="control@example.com",
            resolved_user_ids=list(compact["first"] + compact["last"]),
        )
        self.assertEqual(len(manifest["user_ids"]), 2)

    def test_refuses_already_cleaned_row(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "already records cleanup"):
            cleanup.build_durable_manifest(
                self._run(status="cleaned"),
                load_run_id="12345",
                control_email="control@example.com",
            )


if __name__ == "__main__":
    unittest.main()
