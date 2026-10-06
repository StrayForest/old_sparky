from __future__ import annotations

import io
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from tools.platform_production_qa import ProductionQa


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
    def test_control_email_stdin_is_bounded_and_normalized_before_recovery(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "platform_recover_retained_report.py",
                "--run-root",
                "/opt/oldsparky/platform/shared/production-retained-matrix/gha-1",
                "--load-run-id",
                "1",
                "--control-email-stdin",
                "--mode",
                "external-vote",
            ],
        ):
            args = recovery.parse_args()
        self.assertTrue(args.control_email_stdin)
        self.assertIsNone(args.control_email)
        self.assertEqual(
            recovery.resolve_control_email(
                args, io.BytesIO(b"Control@Example.invalid\n")
            ),
            "control@example.invalid",
        )

        invalid = (
            b"control@example.invalid",
            b" control@example.invalid\n",
            b"control@example.invalid \n",
            b"control@example.invalid\n\n",
            b"control@example.invalid\r\n",
            b"control@example.invalid\x80\n",
            b"x" * (recovery.CONTROL_EMAIL_MAX_BYTES + 1) + b"\n",
        )
        for raw in invalid:
            with self.subTest(raw_length=len(raw)):
                with self.assertRaisesRegex(ValueError, "control email input is invalid"):
                    recovery.read_control_email_stdin(io.BytesIO(raw))

    def test_legacy_control_email_argument_remains_supported(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "platform_recover_retained_report.py",
                "--run-root",
                "/opt/oldsparky/platform/shared/production-retained-matrix/gha-1",
                "--load-run-id",
                "1",
                "--control-email",
                "Control@Example.invalid",
            ],
        ):
            args = recovery.parse_args()
        self.assertEqual(
            recovery.resolve_control_email(args), "control@example.invalid"
        )

    def test_main_delivers_stdin_identity_to_recovery_consumer(self) -> None:
        observed: list[str] = []

        async def consume(args: object) -> int:
            observed.append(args.control_email)
            return 0

        with (
            patch.object(
                sys,
                "argv",
                [
                    "platform_recover_retained_report.py",
                    "--run-root",
                    "/opt/oldsparky/platform/shared/production-retained-matrix/gha-1",
                    "--load-run-id",
                    "1",
                    "--control-email-stdin",
                ],
            ),
            patch.object(
                sys,
                "stdin",
                SimpleNamespace(buffer=io.BytesIO(b"control@example.invalid\n")),
            ),
            patch.object(recovery, "_async_main", consume),
        ):
            self.assertEqual(recovery.main(), 0)
        self.assertEqual(observed, ["control@example.invalid"])

    def test_external_vote_recovery_uses_transport_specific_paths(self) -> None:
        run_root = Path(
            "/opt/oldsparky/platform/shared/production-retained-matrix/gha-32767006384"
        )

        report_path, summary_path = recovery._recovery_paths(run_root, "external-vote")

        self.assertEqual(
            report_path,
            run_root / "external-vote" / "external-vote.json",
        )
        self.assertEqual(
            summary_path,
            run_root / "external-vote" / "matrix-summary.json",
        )

    def test_external_vote_recovery_accepts_write_burst_durable_mode(self) -> None:
        self.assertEqual("write-burst", recovery._expected_stored_mode("external-vote"))
        self.assertEqual("read-mix", recovery._expected_stored_mode("read-mix"))

    def test_durable_provenance_matches_producer_report_shape(self) -> None:
        producer = ProductionQa(
            origin=recovery.EXPECTED_ORIGIN,
            report_path=Path("/tmp/platform-recovery-provenance-test.json"),
            http_timeout=1.0,
            keep_data=True,
            mode="write-burst",
        )
        run = SimpleNamespace(origin=producer.origin)
        report = producer._preprod_report_snapshot(progress=False)

        self.assertEqual(report.get("origin_class"), "production_origin")
        self.assertNotIn("origin", report)
        recovery._validate_durable_provenance(run, report)

    def test_durable_provenance_rejects_wrong_row_origin_or_report_class(self) -> None:
        valid_report = {"origin_class": "production_origin"}
        invalid_cases = (
            (SimpleNamespace(origin="https://other.invalid"), valid_report),
            (SimpleNamespace(origin=recovery.EXPECTED_ORIGIN), {}),
            (
                SimpleNamespace(origin=recovery.EXPECTED_ORIGIN),
                {"origin_class": "local_origin"},
            ),
            (
                SimpleNamespace(origin=recovery.EXPECTED_ORIGIN),
                {"origin": recovery.EXPECTED_ORIGIN},
            ),
        )

        for run, report in invalid_cases:
            with self.subTest(report_keys=tuple(sorted(report))):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "durable QA provenance is not the canonical production origin",
                ):
                    recovery._validate_durable_provenance(run, report)

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
            control_email="qa@example.invalid",
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
            control_email="qa@example.invalid",
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
