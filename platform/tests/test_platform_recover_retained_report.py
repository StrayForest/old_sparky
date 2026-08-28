from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from uuid import uuid4


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "platform_recover_retained_report.py"
)
SPEC = importlib.util.spec_from_file_location(
    "platform_recover_retained_report_tested", SCRIPT_PATH
)
assert SPEC is not None and SPEC.loader is not None
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class RetainedWriteBurstReportRecoveryTests(unittest.TestCase):
    @unittest.skipUnless(os.geteuid() == 0, "root-owned file contract")
    def test_existing_root_report_permissions_are_tightened(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "write-burst.json"
            report_path.write_text("{}", encoding="utf-8")
            report_path.chmod(0o644)

            self.assertTrue(recovery._regular_file(report_path, required=True))
            self.assertEqual(stat.S_IMODE(report_path.stat().st_mode), 0o600)

    def test_recovered_summary_is_an_exact_single_row_cleanup_manifest(self) -> None:
        user_ids = [str(uuid4()), str(uuid4())]
        tournament_ids = [str(uuid4())]
        report_path = Path(
            "/opt/oldsparky/platform/shared/production-retained-matrix/"
            "gha-32767006384/write-burst/write-burst.json"
        )
        report = {
            "user_ids": user_ids,
            "tournament_ids": tournament_ids,
            "mode": "write-burst",
            "write_burst": {"selection": "all"},
            "performance": {"http_client": {"overall": {"p95_ms": 12.5, "p99_ms": 19.0}}},
        }

        summary = recovery.build_recovered_summary(
            report,
            marker="preprod260824120000abcd",
            report_path=report_path,
            load_run_id="32767006384",
            control_email="aleksei.lisitsin1@gmail.com",
        )

        self.assertEqual(summary["mode"], "write-burst")
        self.assertFalse(summary["passed"])
        self.assertTrue(summary["recovered"])
        self.assertEqual(summary["completed_users"], 2)
        self.assertEqual(summary["completed_tournaments"], 1)
        self.assertEqual(len(summary["rows"]), 1)
        self.assertEqual(summary["rows"][0]["result"]["marker"], "preprod260824120000abcd")
        self.assertEqual(summary["write_burst"]["selection"], "all")

    def test_read_mix_recovery_has_one_tournament(self) -> None:
        user_ids = [str(uuid4())]
        tournament_ids = [str(uuid4())]
        report = {
            "user_ids": user_ids,
            "tournament_ids": tournament_ids,
            "mode": "read-mix",
            "read_mix": {"manual_workspace_refresh": True},
            "performance": {},
        }

        summary = recovery.build_recovered_summary(
            report,
            marker="preprod260824120000abcd",
            report_path=Path(
                "/opt/oldsparky/platform/shared/production-retained-matrix/"
                "gha-32767006384/read-mix/read-mix.json"
            ),
            load_run_id="32767006384",
            control_email="aleksei.lisitsin1@gmail.com",
        )

        self.assertEqual(summary["mode"], "read-mix")
        self.assertEqual(summary["planned_tournaments"], 1)

    def test_uuid_validation_rejects_duplicates_and_noncanonical_values(self) -> None:
        duplicate = str(uuid4())
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            recovery._uuid_list([duplicate, duplicate], field="user_ids")
        with self.assertRaisesRegex(RuntimeError, "canonical"):
            recovery._uuid_list([duplicate.upper()], field="user_ids")


if __name__ == "__main__":
    unittest.main()
