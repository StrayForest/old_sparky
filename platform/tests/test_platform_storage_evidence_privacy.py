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
