from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
import unittest
from uuid import uuid4

from tools import platform_cleanup_retained_orphan as cleanup
from tools.platform_evidence_sanitizer import sanitized_log_summary
from tools.platform_cleanup_retained_matrix import validate_cleanup_completion_result
from tools.platform_production_qa import ProductionQa


class RetainedOrphanCleanupTests(unittest.TestCase):
    @staticmethod
    def _cleanup_result(*, valid: bool = True) -> dict[str, object]:
        return {
            "ok": True,
            "markers": 1,
            "users_deleted": 1 if valid else 0,
            "tournaments_deleted": 0,
            "control_account_preserved": True,
            "remaining_users": 0,
            "remaining_tournaments": 0,
            "remaining_sessions": 0,
            "remaining_audit_logs": 0,
            "read_models": {
                "keys_expected": 0,
                "keys_deleted": 0,
                "keys_remaining": 0,
            },
        }

    def test_invalid_control_stdin_stops_before_database_setup(self) -> None:
        argv = [
            "cleanup",
            "--load-run-id",
            "12345",
            "--control-email-stdin",
            "--confirm",
            cleanup.CONFIRMATION,
            "--result-path",
            "/tmp/result.json",
        ]
        stdin = io.TextIOWrapper(io.BytesIO(b"bad identity\n"), encoding="ascii")
        with (
            patch.object(cleanup.sys, "argv", argv),
            patch.object(cleanup.sys, "stdin", stdin),
            patch.object(cleanup, "get_settings") as get_settings,
            patch.object(cleanup, "dispose_engine", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "control email input is invalid"):
                asyncio.run(cleanup._main())
        get_settings.assert_not_called()

    def _run(self, **overrides: object) -> SimpleNamespace:
        marker = "preprod260829000001abcd"
        mode = "read-mix"
        report_path = (
            "/opt/oldsparky/platform/shared/production-retained-matrix/"
            "gha-12345/read-mix/read-mix.json"
        )
        report = {
            "marker": marker,
            "origin_class": "production_origin",
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

    @staticmethod
    def _never_started_proof(load_run_id: str) -> dict[str, object]:
        return {
            "schema": 1,
            "load_run_id": load_run_id,
            "load_run_attempt": "2",
            "repository": "StrayForest/old_sparky",
            "workflow_path": ".github/workflows/platform-production-external-load.yml",
            "head_branch": "dev",
            "head_sha": "d" * 40,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "failure",
            "jobs": {
                "validate-external-inputs": {"status": "completed", "conclusion": "failure"},
                "fixture-setup": {"status": "completed", "conclusion": "skipped"},
                "load-client": {"status": "completed", "conclusion": "skipped"},
                "cpu-diagnostic-pair": {"status": "completed", "conclusion": "skipped"},
            },
            "finalizer_steps": {
                "Configure finalizer SSH": "skipped",
                "Signal fixture completion and collect origin evidence": "skipped",
                "Exact cleanup of external fixture": "skipped",
            },
        }

    def _invoke_no_start_proof(
        self,
        *,
        proof: dict[str, object] | None,
        rows: list[object],
        run_id: str = "9876543210",
    ) -> tuple[dict[str, object], dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="retained-no-start-") as temporary:
            root = Path(temporary)
            run_root_base = root / "run-roots"
            result_path = root / "cleanup-result.json"
            export_base = root / "external-load-export-"
            session = SimpleNamespace(
                scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: rows))
            )

            class SessionContext:
                async def __aenter__(self):
                    return session

                async def __aexit__(self, *_args):
                    return None

            arguments = SimpleNamespace(
                load_run_id=run_id,
                confirm=cleanup.CONFIRMATION,
                control_email="qa.control@example.test",
                result_path=result_path,
                never_started_proof_base64=(
                    base64.b64encode(
                        json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("ascii")
                    ).decode("ascii")
                    if proof is not None
                    else None
                ),
            )
            settings = SimpleNamespace(
                platform_environment="production",
                platform_web_origin=cleanup.EXPECTED_ORIGIN,
            )
            with (
                patch.object(cleanup.os, "geteuid", return_value=0),
                patch.object(cleanup, "RUN_ROOT_BASE", run_root_base),
                patch.object(cleanup, "EXTERNAL_LOAD_EXPORT_BASE", export_base),
                patch.object(cleanup, "get_settings", return_value=settings),
                patch.object(cleanup, "validate_platform_settings"),
                patch.object(cleanup, "session_factory", return_value=lambda: SessionContext()),
            ):
                result = asyncio.run(cleanup.clean_orphan(arguments))
            return result, json.loads(result_path.read_text(encoding="utf-8"))

    def test_never_started_zero_row_cleanup_emits_explicit_zero_credit_receipt(self) -> None:
        proof = self._never_started_proof("9876543210")
        result, published = self._invoke_no_start_proof(proof=proof, rows=[])
        validate_cleanup_completion_result(result)
        self.assertEqual(result["never_started"], True)
        self.assertEqual((result["markers"], result["users_deleted"], result["tournaments_deleted"]), (0, 0, 0))
        self.assertEqual(published, result)
        self.assertEqual(
            (published["load_run_id"], published["load_run_attempt"], published["load_source_sha"]),
            ("9876543210", "2", "d" * 40),
        )
        for mutation in (
            {**result, "load_run_attempt": "3" * 40},
            {**result, "load_source_sha": "not-a-source-sha"},
            {**result, "unexpected": "field"},
            {**result, "users_deleted": 1},
        ):
            with self.subTest(receipt=mutation):
                with self.assertRaises(RuntimeError):
                    validate_cleanup_completion_result(mutation)

    def test_never_started_noop_requires_zero_rows_and_exact_absent_paths(self) -> None:
        proof = self._never_started_proof("9876543210")
        row = self._run()
        with self.assertRaisesRegex(RuntimeError, "conflicts with a durable QA row"):
            self._invoke_no_start_proof(proof=proof, rows=[row])
        with self.assertRaisesRegex(RuntimeError, "exactly one durable QA row"):
            self._invoke_no_start_proof(proof=None, rows=[])

        proof = self._never_started_proof("9876543211")
        with tempfile.TemporaryDirectory(prefix="retained-no-start-export-") as temporary:
            export_base = Path(temporary) / "export-"
            export_path = Path(f"{export_base}9876543211")
            export_path.mkdir(mode=0o700)
            arguments = SimpleNamespace(
                load_run_id="9876543211",
                confirm=cleanup.CONFIRMATION,
                control_email="qa.control@example.test",
                result_path=Path(temporary) / "result.json",
                never_started_proof_base64=base64.b64encode(
                    json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("ascii")
                ).decode("ascii"),
            )
            session = SimpleNamespace(
                scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: []))
            )

            class SessionContext:
                async def __aenter__(self):
                    return session

                async def __aexit__(self, *_args):
                    return None

            with (
                patch.object(cleanup.os, "geteuid", return_value=0),
                patch.object(cleanup, "RUN_ROOT_BASE", Path(temporary) / "run-roots"),
                patch.object(cleanup, "EXTERNAL_LOAD_EXPORT_BASE", export_base),
                patch.object(
                    cleanup,
                    "get_settings",
                    return_value=SimpleNamespace(
                        platform_environment="production",
                        platform_web_origin=cleanup.EXPECTED_ORIGIN,
                    ),
                ),
                patch.object(cleanup, "validate_platform_settings"),
                patch.object(cleanup, "session_factory", return_value=lambda: SessionContext()),
            ):
                with self.assertRaisesRegex(RuntimeError, "conflicts with a retained-load export"):
                    asyncio.run(cleanup.clean_orphan(arguments))
            self.assertFalse(arguments.result_path.exists())

    def test_builds_manifest_from_one_exact_durable_row(self) -> None:
        manifest = cleanup.build_durable_manifest(
            self._run(),
            load_run_id="12345",
            control_email="Control@example.com",
        )
        self.assertEqual(manifest["_control_email"], "control@example.com")
        self.assertNotIn("control_email", manifest)
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
        producer = ProductionQa(
            origin=cleanup.EXPECTED_ORIGIN,
            report_path=Path(report_path),
            http_timeout=1.0,
            keep_data=True,
            mode="write-burst",
        )
        producer.marker = marker
        producer.user_ids.append(str(uuid4()))
        producer.report["marker"] = marker
        producer.report["external_vote"] = {"tournament_count": 11}
        report = producer._preprod_report_snapshot(progress=False)
        self.assertEqual(report["origin_class"], "production_origin")
        self.assertNotIn("origin", report)
        self.assertNotIn("request_origin", report)
        run = SimpleNamespace(
            marker=marker,
            origin=producer.origin,
            report_path=report_path,
            report=report,
            status="running",
            cleanup_state={},
        )

        manifest = cleanup.build_durable_manifest(
            run,
            load_run_id="12345",
            control_email="control@example.com",
        )

        self.assertEqual(manifest["mode"], "write-burst")
        self.assertEqual(manifest["rows"][0]["report_path"], report_path)
        self.assertEqual(
            manifest["rows"][0]["request_origin"], cleanup.EXPECTED_ORIGIN
        )
        self.assertNotIn("origin", report)
        self.assertNotIn("request_origin", report)

        invalid_provenance = (
            (
                SimpleNamespace(**{**vars(run), "origin": "https://other.invalid"}),
                report,
                "durable QA row is not from the canonical production origin",
            ),
            (
                run,
                {**report, "origin_class": "local_origin"},
                "durable QA report is not from the canonical production origin",
            ),
            (
                run,
                {
                    key: value
                    for key, value in report.items()
                    if key != "origin_class"
                } | {"origin": cleanup.EXPECTED_ORIGIN},
                "durable QA report is not from the canonical production origin",
            ),
        )
        for invalid_run, invalid_report, error in invalid_provenance:
            with self.subTest(error=error):
                invalid_run.report = invalid_report
                with self.assertRaisesRegex(RuntimeError, error):
                    cleanup.build_durable_manifest(
                        invalid_run,
                        load_run_id="12345",
                        control_email="control@example.com",
                    )

    def test_legacy_external_vote_path_requires_report_metadata(self) -> None:
        report_path = cleanup._legacy_external_vote_report_path(run_id="12345")
        with self.assertRaisesRegex(RuntimeError, "report path"):
            cleanup.build_durable_manifest(
                self._run(
                    mode="write-burst",
                    report_path=report_path,
                    report={
                        "marker": "preprod260829000001abcd",
                        "origin_class": "production_origin",
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
                "origin_class": "production_origin",
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

    def test_allows_complete_inventory_for_already_cleaned_artifact_cleanup(self) -> None:
        manifest = cleanup.build_durable_manifest(
            self._run(status="cleaned"),
            load_run_id="12345",
            control_email="control@example.com",
            allow_already_cleaned=True,
        )
        self.assertEqual(manifest["user_ids"], {
            "00000000-0000-0000-0000-000000000001"
        })

    def test_orphan_cli_emits_completed_only_for_validated_cleanup_result(self) -> None:
        for result, should_complete in (
            (self._cleanup_result(), True),
            (self._cleanup_result(valid=False), False),
        ):
            with self.subTest(should_complete=should_complete):
                with tempfile.TemporaryDirectory() as temporary:
                    result_path = Path(temporary) / "cleanup-result.json"
                    output = io.StringIO()
                    argv = [
                        "cleanup",
                        "--load-run-id",
                        "12345",
                        "--control-email",
                        "control@example.com",
                        "--confirm",
                        cleanup.CONFIRMATION,
                        "--result-path",
                        str(result_path),
                    ]

                    async def clean_orphan_result(_args: object) -> dict[str, object]:
                        cleanup.validate_cleanup_completion_result(result)
                        result_path.write_text(json.dumps(result), encoding="utf-8")
                        return result

                    with (
                        patch.object(cleanup.sys, "argv", argv),
                        patch.object(cleanup.sys, "stdout", output),
                        patch.object(
                            cleanup,
                            "clean_orphan",
                            new=AsyncMock(side_effect=clean_orphan_result),
                        ),
                        patch.object(cleanup, "dispose_engine", new=AsyncMock()),
                    ):
                        if should_complete:
                            self.assertEqual(asyncio.run(cleanup._main()), 0)
                            lines = output.getvalue().splitlines()
                            self.assertEqual(lines[-1], '{"status":"completed"}')
                            self.assertEqual(
                                sanitized_log_summary(lines)["status"], "passed"
                            )
                            self.assertNotIn("status", json.loads(result_path.read_text()))
                        else:
                            with self.assertRaisesRegex(
                                RuntimeError, "result is incomplete"
                            ):
                                asyncio.run(cleanup._main())
                            self.assertEqual(output.getvalue(), "")
                            self.assertFalse(result_path.exists())


if __name__ == "__main__":
    unittest.main()
