from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from uuid import uuid4


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "platform_recover_retained_browser_report.py"
)
SPEC = importlib.util.spec_from_file_location(
    "platform_recover_retained_browser_report_tested", SCRIPT_PATH
)
assert SPEC is not None and SPEC.loader is not None
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class RetainedBrowserReportRecoveryTests(unittest.TestCase):
    def test_recovered_summary_is_an_exact_single_row_cleanup_manifest(self) -> None:
        user_ids = [str(uuid4()), str(uuid4())]
        tournament_ids = [str(uuid4())]
        report_path = Path(
            "/opt/oldsparky/platform/shared/production-retained-matrix/"
            "gha-32767006384/browser-polling/browser-polling.json"
        )
        report = {
            "user_ids": user_ids,
            "tournament_ids": tournament_ids,
            "polling": {"profile": "browser-polling-20x500", "deduped": 4},
            "performance": {"http_client": {"overall": {"p95_ms": 12.5, "p99_ms": 19.0}}},
        }

        summary = recovery.build_recovered_summary(
            report,
            marker="preprod260824120000abcd",
            report_path=report_path,
            load_run_id="32767006384",
            control_email="aleksei.lisitsin1@gmail.com",
        )

        self.assertEqual(summary["mode"], "browser-polling")
        self.assertFalse(summary["passed"])
        self.assertTrue(summary["recovered"])
        self.assertEqual(summary["completed_users"], 2)
        self.assertEqual(summary["completed_tournaments"], 1)
        self.assertEqual(len(summary["rows"]), 1)
        self.assertEqual(summary["rows"][0]["result"]["marker"], "preprod260824120000abcd")
        self.assertEqual(summary["polling"]["deduped"], 4)

    def test_uuid_validation_rejects_duplicates_and_noncanonical_values(self) -> None:
        duplicate = str(uuid4())
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            recovery._uuid_list([duplicate, duplicate], field="user_ids")
        with self.assertRaisesRegex(RuntimeError, "canonical"):
            recovery._uuid_list([duplicate.upper()], field="user_ids")


if __name__ == "__main__":
    unittest.main()
