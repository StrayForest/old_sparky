"""Adversarial privacy checks for storage and deployment evidence summaries."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.platform_storage_evidence_summary import (
    summarize_backup,
    summarize_df,
    summarize_du,
    summarize_inode,
    summarize_journal,
    summarize_lock,
    summarize_retention,
    summarize_service,
)
from tools import platform_storage_diagnostics_sanitizer as diagnostics_sanitizer
from tools import platform_storage_diagnostics_contract as diagnostics_contract
from tools.platform_storage_diagnostics_sanitizer import (
    MAX_INPUT_BYTES,
    MAX_REPORTED_BYTES,
    project_public_artifact,
)


FORBIDDEN_VALUES = (
    "operator@example.test",
    "Authorization: Bearer secret-token",
    "Cookie: session=secret-session; csrf=secret-csrf",
    "198.51.100.42",
    "2001:db8::42",
    "https://old-sparky.example/invite/INVITE-CODE?token=secret-token",
    "INVITE-CODE",
    "SELECT email FROM users WHERE password='secret-password'",
    "/home/operator/private-report.json",
    "/root/.ssh/id_ed25519",
    "ssh -i /root/.ssh/id_ed25519 operator@example.test",
    "".join(("-----BEGIN ", "OPENSSH ", "PRIVATE ", "KEY-----")),
)


def serialized(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def valid_storage_report() -> bytes:
    lines = [
        '{"schema":1,"kind":"platform_storage_diagnostics",'
        '"active_release_id":"release-1",'
        '"active_source_sha":"' + "a" * 40 + '"}',
        "=== retained_load_lock ===",
        "state=unlocked",
        "=== filesystem_usage ===",
    ]
    for category in ("root", "tmp", "var_tmp", "platform_runtime", "logs"):
        lines.extend(
            (
                f"--- df {category}",
                "Filesystem 1024-blocks Used Available Capacity Mounted on",
                "1000 400 600 40% /private/path",
                f"--- inode {category}",
                "Inodes IUsed IFree IUse% Mounted on",
                "1000 400 40%",
            )
        )
    lines.extend(("=== journal_usage ===", "Archived and active journals take up 32.0M in the file system."))
    lines.append("=== service_sandbox ===")
    for service in ("deadlock-api", "deadlock-worker", "deadlock-web"):
        lines.extend(
            (
                f"--- service {service}",
                "ActiveState=active",
                "SubState=running",
                "Result=success",
                "ExecMainCode=exited",
                "ExecMainStatus=0",
                "NRestarts=0",
                "MemoryCurrent=1",
                "MemoryPeak=1",
                "MemoryMax=infinity",
                "TasksCurrent=1",
                "TasksMax=infinity",
                "CPUUsageNSec=1",
            )
        )
    lines.append("=== known_category_usage ===")
    for category in (
        "source_release_artifacts",
        "browser_test_artifacts",
        "preprod_screenshots",
        "live_qa_runtime",
        "backups",
    ):
        lines.extend((f"--- category {category}", "42 /private/path"))
    lines.extend(
        (
            "=== storage_retention_dry_run ===",
            json.dumps(
                {
                    "ok": True,
                    "mode": "dry-run",
                    "production_releases": {
                        "protected": [],
                        "retained": [],
                        "deleted": [],
                        "reclaimable_bytes": 0,
                    },
                    "source_release_artifacts": {
                        "protected": [],
                        "retained": [],
                        "deleted": [],
                        "reclaimable_bytes": 0,
                    },
                    "live_qa_runtime_caches": {
                        "protected": [],
                        "retained": [],
                        "deleted": [],
                        "reclaimable_bytes": 0,
                    },
                    "transient": {
                        "failed_builds": {"count": 0, "reclaimable_bytes": 0},
                        "browser_test_artifacts": {"count": 0, "reclaimable_bytes": 0},
                        "preprod_screenshots": {"count": 0, "reclaimable_bytes": 0},
                        "reclaimable_bytes": {
                            "failed_builds": 0,
                            "browser_test_artifacts": 0,
                            "preprod_screenshots": 0,
                        },
                    },
                    "duration_seconds": 0.1,
                    "limits": {
                        "minimum_free_bytes": 1,
                        "maximum_used_percent": 90,
                    },
                    "disk_before": {"free_bytes": 2, "used_percent": 10},
                    "disk_after": {"free_bytes": 2, "used_percent": 10},
                    "backup": {"status": "skipped"},
                },
                separators=(",", ":"),
            ),
        )
    )
    return ("\n".join(lines) + "\n").encode()


class PlatformStorageEvidencePrivacyTests(unittest.TestCase):
    def assert_no_forbidden_values(self, value: object) -> None:
        output = serialized(value).lower()
        for forbidden in FORBIDDEN_VALUES:
            self.assertNotIn(forbidden.lower(), output)

    def test_fixed_schema_storage_producers_drop_raw_values(self) -> None:
        disk = summarize_df(
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "10737418240 4294967296 6442450944 40%\n"
            "operator@example.test 198.51.100.42 2001:db8::42\n",
            category="root",
        )
        inode = summarize_inode(
            "Inodes IUsed IFree IUse% Mounted on\n"
            "400 600 40%\n"
            "ssh -i /root/.ssh/id_ed25519 operator@example.test\n",
            category="root",
        )
        category = summarize_du(
            "42 /root/.ssh/id_ed25519 SELECT email FROM users WHERE "
            "password='secret-password'\n",
            category="backups",
        )
        journal = summarize_journal(
            "Archived and active journals take up 32.0M in the file system. "
            "Authorization: Bearer secret-token https://old-sparky.example/invite/"
            "INVITE-CODE?token=secret-token 198.51.100.42 2001:db8::42\n"
        )
        lock = summarize_lock(
            "state=held\ncommand=ssh -i /root/.ssh/id_ed25519 "
            "operator@example.test path=/home/operator/private-report.json "
            + "".join(("-----BEGIN ", "OPENSSH ", "PRIVATE ", "KEY-----"))
            + "\n"
        )
        service = summarize_service(
            "ActiveState=failed\nSubState=failed\nResult=exit-code\n"
            "ExecMainStatus=1\nUser=operator@example.test\n"
            "ExecStart=/bin/sh -c 'SELECT email FROM users WHERE password=secret-password'\n",
            service="deadlock-api",
        )
        backup = summarize_backup(
            json.dumps(
                {
                    "ok": True,
                    "dump_file": "/root/backups/platformdb-private.dump",
                    "metadata_file": "/home/operator/backup.json",
                    "sha256": "a" * 64,
                    "size_bytes": 1024,
                    "restore_verified": True,
                    "alembic_revision_verified": True,
                    "removed": ["operator@example.test"],
                    "error": "ssh -i /root/.ssh/id_ed25519 operator@example.test",
                }
            ),
            phase="create",
        )

        for summary in (disk, inode, category, journal, lock, service, backup):
            self.assert_no_forbidden_values(summary)
        self.assertEqual(disk["free_bytes"], 6442450944)
        self.assertEqual(disk["used_percent"], 40.0)
        self.assertEqual(category["size_bytes"], 42)
        self.assertTrue(lock["lock_held"])
        self.assertEqual(lock["holder_count"], 1)
        self.assertEqual(service["error_class"], "failure")
        self.assertEqual(backup["removed_count"], 1)
        self.assertTrue(backup["restore_verified"])

        hostile_stderr = (
            b"active release SHA differs from expected_sha. "
            b"Authorization: Bearer secret-token "
            b"Cookie: session=secret-session; csrf=secret-csrf "
            b"https://old-sparky.example/invite/INVITE-CODE?token=secret-token "
            b"/home/operator/private-report.json "
            b"ssh -i /root/.ssh/id_ed25519 operator@example.test"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "report"
            stderr_path = root / "stderr"
            output_path = root / "artifact"
            report_path.write_bytes(
                b"raw secret-token /home/operator/private-report.json "
                b"https://old-sparky.example/invite/INVITE-CODE?token=secret-token\n"
                + valid_storage_report()
            )
            stderr_path.write_bytes(b"")
            success = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=0,
                remote_stderr_bytes=0,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=True,
            )
            self.assertEqual(success["status"], "passed")
            self.assertEqual(success["kind"], "platform_storage_diagnostics")
            self.assertNotIn("failure", success)
            self.assertFalse(success["raw_output_included"])
            self.assert_no_forbidden_values(success)
            self.assertNotIn("private/path", serialized(success))

            success_output = root / "success-artifact"
            success_status = diagnostics_sanitizer.main(
                [
                    "--report-path",
                    str(report_path),
                    "--stderr-path",
                    str(stderr_path),
                    "--output-path",
                    str(success_output),
                    "--expected-sha",
                    "a" * 40,
                    "--remote-exit-code",
                    "0",
                    "--remote-stderr-bytes",
                    "0",
                    "--report-present",
                    "true",
                ]
            )
            self.assertEqual(success_status, 0)
            self.assertEqual(success_output.stat().st_mode & 0o777, 0o600)
            self.assertFalse(success_output.is_symlink())
            self.assertNotIn("failure", json.loads(success_output.read_text()))

            invalid_report_path = root / "invalid-report"
            invalid_report_path.write_bytes(
                valid_storage_report().replace(
                    b"42 /private/path", b"not-a-size /private/path", 1
                )
            )
            invalid_report = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=1,
                remote_stderr_bytes=0,
                report_path=invalid_report_path,
                stderr_path=stderr_path,
                report_present=True,
            )
            self.assertEqual(invalid_report["reason"], "report_schema_incomplete")

            stderr_path.write_bytes(hostile_stderr + b"\x00\x1b[31m\xff\xfe")
            report_path.unlink()
            failure = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=1,
                remote_stderr_bytes=len(hostile_stderr),
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=False,
            )
            self.assert_no_forbidden_values(failure)
            self.assertEqual(failure["status"], "failed")
            self.assertFalse(failure["raw_output_included"])
            self.assertEqual(failure["phase"], "precondition")
            self.assertEqual(failure["reason"], "active_sha_mismatch")
            self.assertEqual(failure["action"], "recalculate_expected_sha_from_release")
            self.assertFalse(failure["report_present"])

            stderr_path.write_bytes(b"")
            before_report = project_public_artifact(
                expected_sha="not-a-sha",
                remote_exit_code=0,
                remote_stderr_bytes=0,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=False,
            )
            self.assertEqual(before_report["reason"], "remote_report_missing")
            self.assertEqual(before_report["expected_sha"], "unavailable")

            report_path.write_bytes(b"x" * (MAX_INPUT_BYTES + 1))
            stderr_path.write_bytes(b"y" * (MAX_INPUT_BYTES + 1))
            oversized = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=1,
                remote_stderr_bytes=MAX_REPORTED_BYTES + 1,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=True,
            )
            self.assertEqual(oversized["reason"], "report_truncated")
            self.assertTrue(oversized["stderr_truncated"])
            self.assertTrue(oversized["report_truncated"])
            self.assertIsNone(oversized["remote_stderr_bytes"])

            report_path.unlink()
            stderr_path.write_bytes(b"private transport path /root/.ssh/id_ed25519")
            transport = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=255,
                remote_stderr_bytes=12,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=False,
            )
            self.assertEqual(transport["reason"], "ssh_transport")
            self.assert_no_forbidden_values(transport)

            timeout = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=124,
                remote_stderr_bytes=0,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=False,
            )
            self.assertEqual(timeout["reason"], "remote_timeout")

            fifo = root / "endless"
            os.mkfifo(fifo)
            endless = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=1,
                remote_stderr_bytes=0,
                report_path=fifo,
                stderr_path=stderr_path,
                report_present=True,
            )
            self.assertEqual(endless["reason"], "remote_report_missing")

            exception_output = root / "exception-artifact"
            report_path.write_bytes(valid_storage_report())
            with mock.patch.object(
                diagnostics_sanitizer,
                "_project_report",
                side_effect=RuntimeError("secret path must not escape"),
            ):
                status = diagnostics_sanitizer.main(
                    [
                        "--report-path",
                        str(report_path),
                        "--stderr-path",
                        str(stderr_path),
                        "--output-path",
                        str(exception_output),
                        "--expected-sha",
                        "a" * 40,
                        "--remote-exit-code",
                        "1",
                        "--remote-stderr-bytes",
                        "0",
                        "--report-present",
                        "true",
                    ]
                )
            self.assertEqual(status, 1)
            exception_report = json.loads(exception_output.read_text())
            self.assertEqual(exception_report["reason"], "sanitizer_exception")
            self.assert_no_forbidden_values(exception_report)

            output_path.symlink_to(stderr_path)
            with mock.patch.object(
                diagnostics_sanitizer,
                "_project_report",
                side_effect=RuntimeError("secret path must not escape"),
            ):
                status = diagnostics_sanitizer.main(
                    [
                        "--report-path",
                        str(report_path),
                        "--stderr-path",
                        str(stderr_path),
                        "--output-path",
                        str(output_path),
                        "--expected-sha",
                        "a" * 40,
                        "--remote-exit-code",
                        "1",
                        "--remote-stderr-bytes",
                        "0",
                        "--report-present",
                        "true",
                    ]
                )
            self.assertEqual(status, 1)
            self.assertTrue(output_path.is_symlink())
            self.assertEqual(output_path.read_bytes(), stderr_path.read_bytes())

    def test_retention_summary_keeps_numeric_recovery_audit_only(self) -> None:
        report = summarize_retention(
            json.dumps(
                {
                    "ok": True,
                    "mode": "apply",
                    "production_releases": {
                        "protected": ["release-20260912"],
                        "retained": ["release-20260911"],
                        "deleted": [
                            "operator@example.test",
                            "/home/operator/private-report.json",
                        ],
                        "reclaimable_bytes": 8192,
                    },
                    "source_release_artifacts": {
                        "protected": ["release-20260912"],
                        "retained": [],
                        "deleted": [],
                        "reclaimable_bytes": 0,
                    },
                    "live_qa_runtime_caches": {
                        "protected": ["runtime-" + "a" * 40],
                        "retained": [],
                        "deleted": [],
                        "reclaimable_bytes": 0,
                    },
                    "transient": {
                        "reclaimable_bytes": {
                            "failed_builds": 1024,
                            "browser_test_artifacts": 2048,
                            "preprod_screenshots": 4096,
                        },
                        "raw": (
                            "Authorization: Bearer secret-token Cookie=session=secret-session "
                            "https://old-sparky.example/invite/INVITE-CODE?token=secret-token "
                            "SELECT email FROM users WHERE password='secret-password' "
                            "/root/.ssh/id_ed25519"
                        ),
                    },
                    "duration_seconds": 12.5,
                    "limits": {
                        "minimum_free_bytes": 5 * 1024**3,
                        "maximum_used_percent": 85,
                    },
                    "disk_before": {"free_bytes": 6 * 1024**3, "used_percent": 76.5},
                    "disk_after": {"free_bytes": 7 * 1024**3, "used_percent": 71.5},
                    "backup": {
                        "status": "completed",
                        "restore_verified": True,
                        "alembic_revision_verified": True,
                        "checksum_present": True,
                        "size_bytes": 1234,
                        "dump_file": "/root/backups/private.sql",
                        "metadata_file": "/home/operator/metadata.json",
                    },
                    "error": "ssh -i /root/.ssh/id_ed25519 operator@example.test",
                }
            )
        )

        self.assert_no_forbidden_values(report)
        self.assertEqual(report["disk_after"], {
            "free_bytes": 7 * 1024**3,
            "used_percent": 71.5,
        })
        self.assertEqual(report["disk_before"]["free_bytes"], 6 * 1024**3)
        self.assertEqual(report["limits"], {
            "minimum_free_bytes": 5 * 1024**3,
            "maximum_used_percent": 85.0,
        })
        self.assertEqual(report["duration_seconds"], 12.5)
        self.assertEqual(report["backup"]["status"], "completed")
        self.assertTrue(report["backup"]["restore_verified"])
        self.assertTrue(report["backup"]["checksum_present"])
        self.assertEqual(report["categories"]["production_releases"]["reclaimable_bytes"], 8192)
        self.assertEqual(report["transient_reclaimable_bytes"]["failed_builds"], 1024)

    def test_failure_schema_is_closed_and_success_validation_is_strict(self) -> None:
        failure_keys = {
            "schema",
            "kind",
            "status",
            "raw_output_included",
            "expected_sha",
            "remote_exit_code",
            "remote_stderr_bytes",
            "stderr_truncated",
            "report_present",
            "report_truncated",
            "phase",
            "reason",
            "action",
            "active_release_id",
            "active_source_sha",
            "sections",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "report"
            stderr_path = root / "stderr"
            stderr_path.write_bytes(b"")

            before_report = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=255,
                remote_stderr_bytes=0,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=False,
            )
            self.assertEqual(set(before_report), failure_keys)
            self.assertEqual(before_report["reason"], "ssh_transport")
            self.assertIn(before_report["phase"], diagnostics_sanitizer.SAFE_FAILURE_PHASES)
            self.assertIn(before_report["reason"], diagnostics_sanitizer.SAFE_FAILURE_REASONS)
            self.assertIn(before_report["action"], diagnostics_sanitizer.SAFE_FAILURE_ACTIONS)
            self.assertEqual(before_report["active_source_sha"], "unavailable")
            self.assertFalse(before_report["raw_output_included"])

            malformed = valid_storage_report().replace(
                b'"active_source_sha":"' + b"a" * 40 + b'"}',
                b'"active_source_sha":"' + b"a" * 40 + b'","unexpected":"drop"}',
            )
            report_path.write_bytes(malformed)
            invalid = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=0,
                remote_stderr_bytes=0,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=True,
            )
            self.assertEqual(invalid["reason"], "report_schema_incomplete")
            self.assertEqual(set(invalid), failure_keys | {"active_release_id", "active_source_sha", "sections"})

            unknown_record = valid_storage_report().replace(
                b"--- df root\n", b"--- unexpected root\n--- df root\n", 1
            )
            report_path.write_bytes(unknown_record)
            invalid = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=0,
                remote_stderr_bytes=0,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=True,
            )
            self.assertEqual(invalid["reason"], "report_schema_incomplete")

            malformed_report_path = root / "malformed-report"
            malformed_report_path.write_bytes(
                valid_storage_report().replace(
                    b"state=unlocked", b"state=unlocked\x00\x1b[31m", 1
                )
            )
            malformed_report = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=0,
                remote_stderr_bytes=0,
                report_path=malformed_report_path,
                stderr_path=stderr_path,
                report_present=True,
            )
            self.assertEqual(malformed_report["status"], "failed")
            self.assertEqual(malformed_report["reason"], "report_schema_incomplete")
            self.assert_no_forbidden_values(malformed_report)

            report_path.write_bytes(valid_storage_report())
            stderr_path.write_bytes(b"warning=\xff\x00\x1b[31m")
            noisy_success = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=0,
                remote_stderr_bytes=11,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=True,
            )
            self.assertEqual(noisy_success["status"], "failed")
            self.assertFalse(noisy_success["raw_output_included"])
            self.assert_no_forbidden_values(noisy_success)

            sanitized = diagnostics_sanitizer._failure_summary(
                expected_sha="a" * 40,
                remote_exit_code=1,
                remote_stderr_bytes=0,
                report_present=False,
                phase="private-secret",
                reason="private-secret",
                action="private-secret",
            )
            self.assertEqual(sanitized["phase"], "capture")
            self.assertEqual(sanitized["reason"], "remote_collection_failed")
            self.assertEqual(sanitized["action"], "inspect_remote_collection")
            self.assert_no_forbidden_values(sanitized)

    def test_failure_contract_is_shared_and_atomic_writer_removes_stale_output(self) -> None:
        self.assertEqual(
            diagnostics_sanitizer.SAFE_FAILURE_PHASES,
            diagnostics_contract.SAFE_FAILURE_PHASES,
        )
        self.assertEqual(
            diagnostics_sanitizer.SAFE_FAILURE_REASONS,
            diagnostics_contract.SAFE_FAILURE_REASONS,
        )
        self.assertEqual(
            diagnostics_sanitizer.SAFE_FAILURE_ACTIONS,
            diagnostics_contract.SAFE_FAILURE_ACTIONS,
        )
        workflow = (
            Path(__file__).resolve().parents[2]
            / ".github/workflows/platform-production-storage-diagnostics.yml"
        ).read_text(encoding="utf-8")
        for value in (
            *diagnostics_contract.SAFE_FAILURE_PHASES,
            *diagnostics_contract.SAFE_FAILURE_REASONS,
            *diagnostics_contract.SAFE_FAILURE_ACTIONS,
        ):
            self.assertIn(f'"{value}"', workflow)
        for key in diagnostics_contract.FAILURE_KEYS:
            self.assertIn(f'"{key}"', workflow)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "report"
            stderr_path = root / "stderr"
            output_path = root / "artifact"
            report_path.write_bytes(valid_storage_report())
            stderr_path.write_bytes(b"")
            passed = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=0,
                remote_stderr_bytes=0,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=True,
            )
            output_path.write_text(json.dumps(passed), encoding="utf-8")
            with mock.patch.object(
                diagnostics_sanitizer,
                "_project_report",
                side_effect=RuntimeError("projector failed"),
            ):
                status = diagnostics_sanitizer.main(
                    [
                        "--report-path",
                        str(report_path),
                        "--stderr-path",
                        str(stderr_path),
                        "--output-path",
                        str(output_path),
                        "--expected-sha",
                        "a" * 40,
                        "--remote-exit-code",
                        "1",
                        "--remote-stderr-bytes",
                        "0",
                        "--report-present",
                        "true",
                    ]
                )
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(output_path.read_text())["status"], "failed")

            with mock.patch.object(
                diagnostics_sanitizer,
                "project_public_artifact",
                return_value=passed,
            ):
                status = diagnostics_sanitizer.main(
                    [
                        "--report-path",
                        str(report_path),
                        "--stderr-path",
                        str(stderr_path),
                        "--output-path",
                        str(output_path),
                        "--expected-sha",
                        "a" * 40,
                        "--remote-exit-code",
                        "1",
                        "--remote-stderr-bytes",
                        "0",
                        "--report-present",
                        "true",
                    ]
                )
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(output_path.read_text())["status"], "failed")

            output_path.write_text(json.dumps(passed), encoding="utf-8")
            with mock.patch.object(
                diagnostics_sanitizer.os,
                "replace",
                side_effect=OSError("atomic commit failed"),
            ):
                with self.assertRaises(OSError):
                    diagnostics_sanitizer._write_nofollow(output_path, passed)
            self.assertFalse(output_path.exists())

            with mock.patch.object(
                diagnostics_sanitizer.os,
                "fsync",
                side_effect=OSError("partial fsync failed"),
            ):
                status = diagnostics_sanitizer.main(
                    [
                        "--report-path",
                        str(report_path),
                        "--stderr-path",
                        str(stderr_path),
                        "--output-path",
                        str(output_path),
                        "--expected-sha",
                        "a" * 40,
                        "--remote-exit-code",
                        "1",
                        "--remote-stderr-bytes",
                        "0",
                        "--report-present",
                        "false",
                    ]
                )
            self.assertEqual(status, 1)
            self.assertFalse(output_path.exists())

            stderr_path.unlink()
            missing_stderr = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=0,
                remote_stderr_bytes=0,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=True,
            )
            self.assertEqual(missing_stderr["reason"], "stderr_capture_missing")

    def test_incomplete_numeric_and_retention_records_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "report"
            stderr_path = root / "stderr"
            stderr_path.write_bytes(b"")
            missing_service_numeric = valid_storage_report().replace(
                b"MemoryCurrent=1", b"MemoryCurrent=", 1
            )
            report_path.write_bytes(missing_service_numeric)
            result = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=0,
                remote_stderr_bytes=0,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=True,
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "report_schema_incomplete")

            incomplete_retention = valid_storage_report().replace(
                b'"duration_seconds":0.1', b'"duration_seconds":null', 1
            )
            report_path.write_bytes(incomplete_retention)
            result = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=0,
                remote_stderr_bytes=0,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=True,
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "report_schema_incomplete")

            nonfinite = valid_storage_report().replace(
                b"MemoryMax=infinity", b"MemoryMax=NaN", 1
            )
            report_path.write_bytes(nonfinite)
            result = project_public_artifact(
                expected_sha="a" * 40,
                remote_exit_code=0,
                remote_stderr_bytes=0,
                report_path=report_path,
                stderr_path=stderr_path,
                report_present=True,
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "report_schema_incomplete")

if __name__ == "__main__":
    unittest.main()
