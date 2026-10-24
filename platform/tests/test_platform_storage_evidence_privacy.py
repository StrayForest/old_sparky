"""Adversarial privacy checks for storage and deployment evidence summaries."""

from __future__ import annotations

import json
import unittest

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
from tools.platform_backup_failure_receipt import (
    ReceiptError,
    project_receipt,
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
                    "protected_retention_proof": {
                        "manifest_path": "/opt/oldsparky/platform/shared/backup-retention/protected-dump-files.json",
                        "protected_dump_files": ["platformdb-20261012T120000Z.dump"],
                        "inventory": [{"dump_identity": [1, 2, 3], "claimed_sha256": "b" * 64}],
                        "digest": "c" * 64,
                    },
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
        self.assertNotIn("protected_retention_proof", backup)
        self.assertNotIn("platformdb-20261012T120000Z.dump", serialized(backup))
        self.assertNotIn("/opt/oldsparky/platform/shared/backup-retention", serialized(backup))
        self.assertNotIn("b" * 64, serialized(backup))
        self.assertNotIn("c" * 64, serialized(backup))

    def test_backup_failure_receipt_projects_only_exact_bound_closed_fields(self) -> None:
        run_id = "39248154270"
        attempt = "1"
        source_sha = "a" * 40
        receipt = {
            "schema": 1,
            "event": "platform_backup_failure",
            "run_id": run_id,
            "attempt": attempt,
            "operation": "create",
            "source_sha": source_sha,
            "bundle_sha256": "b" * 64,
            "stage": "backup_create",
            "public_summary_error": "none",
            "public_summary_normalizer_diagnostic": {
                "schema": 1,
                "status": "not_run",
                "normalizer_exit_code": None,
                "stdout_bytes": None,
                "stdout_sha256": None,
                "stdout_class": "not_run",
                "stderr_bytes": None,
                "stderr_sha256": None,
                "stderr_class": "not_run",
                "exception_class": "none",
                "collector_error_class": "none",
            },
            "exit_code": 1,
            "report_state": "typed_failure",
            "report_error_class": "storage",
            "restore_diagnostic": None,
            "scratch_unit_diagnostic": None,
            "scratch_admission_diagnostic": {
                "schema": 1,
                "phase": "pre_create_admission",
                "guard_reason": "memory_reserve",
                "available_bytes": 1024,
                "required_bytes": 1073741824,
                "helper_rss_bytes": 400,
                "direct_parent_rss_bytes": 600,
                "observed_pair_rss_bytes": 1000,
                "observed_process_count": 2,
            },
            "backup_disk_diagnostic": None,
            "scratch_host_guard_diagnostic": None,
            "scratch_first_fault": None,
            "scratch_resource_attribution": None,
            "scratch_phase_attribution": None,
            "scratch_cleanup_status": None,
            "report_bytes": 31,
            "report_sha256": "c" * 64,
            "stderr_state": "read",
            "stderr_exception_class": "none",
            "stderr_bytes": 17,
            "stderr_sha256": "d" * 64,
        }
        result = project_receipt(json.dumps(receipt).encode("ascii"), run_id, attempt, source_sha)
        self.assertEqual(result["stage"], "backup_create")
        self.assertEqual(result["report_error_class"], "storage")
        self.assertIsNone(result["backup_disk_diagnostic"])
        self.assertEqual(
            result["scratch_admission_diagnostic"],
            {
                "phase": "pre_create_admission",
                "guard_reason": "memory_reserve",
                "available_bytes": 1024,
                "required_bytes": 1073741824,
                "observed_process_count": 2,
            },
        )
        self.assertNotIn("helper_rss_bytes", result["scratch_admission_diagnostic"])
        self.assert_no_forbidden_values(result)
        self.assertNotIn("bundle_sha256", result)

        receipt["backup_disk_diagnostic"] = {
            "schema": 1,
            "phase": "scratch_image_admission",
            "guard_reason": "disk_floor",
            "free_bytes": 1,
            "required_free_bytes": 2,
        }
        disk_result = project_receipt(json.dumps(receipt).encode("ascii"), run_id, attempt, source_sha)
        self.assertEqual(
            disk_result["backup_disk_diagnostic"],
            {
                "phase": "scratch_image_admission",
                "guard_reason": "disk_floor",
                "free_bytes": 1,
                "required_free_bytes": 2,
            },
        )
        for phase in ("scratch_image_postallocation",):
            receipt["backup_disk_diagnostic"]["phase"] = phase
            phase_result = project_receipt(
                json.dumps(receipt).encode("ascii"), run_id, attempt, source_sha
            )
            self.assertEqual(phase_result["backup_disk_diagnostic"]["phase"], phase)
        receipt["backup_disk_diagnostic"]["phase"] = "unrecognized_image_phase"
        with self.assertRaises(ReceiptError):
            project_receipt(json.dumps(receipt).encode("ascii"), run_id, attempt, source_sha)
        receipt["backup_disk_diagnostic"] = {
            "schema": 1,
            "phase": "scratch_image_admission",
            "guard_reason": "disk_floor",
            "free_bytes": 1,
            "required_free_bytes": 2,
            "unexpected": "field",
        }
        with self.assertRaises(ReceiptError):
            project_receipt(json.dumps(receipt).encode("ascii"), run_id, attempt, source_sha)

    def test_backup_failure_receipt_rejects_wrong_binding_and_open_schema(self) -> None:
        receipt = {
            "schema": 1,
            "event": "platform_backup_failure",
            "run_id": "12",
            "attempt": "2",
            "operation": "create",
            "source_sha": "a" * 40,
            "bundle_sha256": "b" * 64,
            "stage": "backup_create",
            "public_summary_error": "none",
            "public_summary_normalizer_diagnostic": {
                "schema": 1,
                "status": "not_run",
                "normalizer_exit_code": None,
                "stdout_bytes": None,
                "stdout_sha256": None,
                "stdout_class": "not_run",
                "stderr_bytes": None,
                "stderr_sha256": None,
                "stderr_class": "not_run",
                "exception_class": "none",
                "collector_error_class": "none",
            },
            "exit_code": 1,
            "report_state": "typed_failure",
            "report_error_class": "storage",
            "restore_diagnostic": None,
            "scratch_unit_diagnostic": None,
            "scratch_admission_diagnostic": None,
            "backup_disk_diagnostic": None,
            "scratch_host_guard_diagnostic": None,
            "scratch_first_fault": None,
            "scratch_resource_attribution": None,
            "scratch_phase_attribution": None,
            "scratch_cleanup_status": None,
            "report_bytes": 0,
            "report_sha256": None,
            "stderr_state": "absent",
            "stderr_exception_class": "none",
            "stderr_bytes": 0,
            "stderr_sha256": None,
        }
        binding = ("12", "2", "a" * 40)
        for field, wrong_binding in zip(
            ("run_id", "attempt", "source_sha"),
            (("13", "2", "a" * 40), ("12", "1", "a" * 40), ("12", "2", "e" * 40)),
            strict=True,
        ):
            with self.subTest(field=field):
                with self.assertRaises(ReceiptError):
                    project_receipt(json.dumps(receipt).encode("ascii"), *wrong_binding)
        receipt["private_message"] = "Authorization: Bearer secret-token /root/private-report"
        with self.assertRaises(ReceiptError):
            project_receipt(json.dumps(receipt).encode("ascii"), *binding)
        duplicate_key_json = json.dumps(receipt).replace('"schema": 1,', '"schema": 1, "schema": 1,', 1)
        with self.assertRaises(ReceiptError):
            project_receipt(duplicate_key_json.encode("ascii"), *binding)

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

if __name__ == "__main__":
    unittest.main()
