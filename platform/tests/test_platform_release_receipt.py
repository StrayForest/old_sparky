from __future__ import annotations

from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from platform_release_receipt import (  # noqa: E402
    RECEIPT_MEMBER,
    ReceiptError,
    canonical_bytes,
    inspect_single_member_archive,
    validate_receipt,
    write_receipt,
)


class ReleaseReceiptTests(unittest.TestCase):
    SHA = "a" * 40

    def _payload(self) -> dict[str, object]:
        ref = "StrayForest/old_sparky/.github/workflows/platform-security.yml@refs/heads/dev"
        return {
            "schema": 1,
            "kind": "platform-production-release",
            "target_sha": self.SHA,
            "mode": "deploy",
            "runtime_profile": "ready-vote-static-8",
            "web_compression": "enabled",
            "security": {
                "event": "push",
                "workflow_name": "Platform security and build",
                "workflow_path": ".github/workflows/platform-security.yml",
                "workflow_ref": ref,
                "workflow_sha": self.SHA,
                "repository": "StrayForest/old_sparky",
                "run_id": "100",
                "run_attempt": "2",
            },
            "classifier": {
                "event": "push",
                "workflow_name": "Platform security and build",
                "workflow_path": ".github/workflows/platform-security.yml",
                "workflow_ref": ref,
                "workflow_sha": self.SHA,
                "repository": "StrayForest/old_sparky",
                "run_id": "100",
                "run_attempt": "2",
            },
            "caller": {
                "event": "workflow_run",
                "workflow_name": "Platform production auto-deploy",
                "workflow_path": ".github/workflows/platform-production-autodeploy.yml",
                "workflow_ref": "StrayForest/old_sparky/.github/workflows/platform-production-autodeploy.yml@refs/heads/dev",
                "workflow_sha": self.SHA,
                "repository": "StrayForest/old_sparky",
                "run_id": "200",
                "run_attempt": "1",
            },
            "called": {
                "event": "workflow_run",
                "workflow_name": "Platform production deploy",
                "workflow_path": ".github/workflows/platform-production-deploy.yml",
                "workflow_ref": "StrayForest/old_sparky/.github/workflows/platform-production-deploy.yml@refs/heads/dev",
                "workflow_sha": self.SHA,
                "repository": "StrayForest/old_sparky",
                "run_id": "200",
                "run_attempt": "1",
            },
            "jobs": {
                "deploy": {"name": "Deploy production", "status": "completed", "conclusion": "success"},
                "final": {"name": "Release finalizer", "status": "completed", "conclusion": "success"},
            },
            "status": {
                "context": "platform-production-deploy",
                "state": "success",
                "description": "Production deployment and live smoke passed",
                "target_url": "https://github.com/StrayForest/old_sparky/actions/runs/200/attempts/1",
            },
            "status_url": "https://github.com/StrayForest/old_sparky/actions/runs/200/attempts/1",
            "artifact": {
                "id": 300,
                "name": "platform-production-release-receipt-content-200-1",
                "size_bytes": 512,
                "digest": "sha256:" + "b" * 64,
                "member": RECEIPT_MEMBER,
                "content_sha256": "c" * 64,
                "workflow_run_id": "200",
                "workflow_run_attempt": "1",
            },
        }

    def test_closed_schema_and_expected_bindings(self) -> None:
        payload = self._payload()
        self.assertIs(validate_receipt(payload, expected_target_sha=self.SHA), payload)
        payload["unexpected"] = True
        with self.assertRaises(ReceiptError):
            validate_receipt(payload)

    def test_write_is_canonical_mode600_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            write_receipt(path, self._payload())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_bytes(), canonical_bytes(self._payload()))
            self.assertNotIn("unexpected", path.read_text(encoding="ascii"))

    def test_archive_requires_one_canonical_receipt_member(self) -> None:
        payload = self._payload()
        data = canonical_bytes(payload)
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "receipt.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
                info = zipfile.ZipInfo(RECEIPT_MEMBER)
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, data)
            receipt, read_data = inspect_single_member_archive(archive_path)
            self.assertEqual(receipt["target_sha"], self.SHA)
            self.assertEqual(read_data, data)

    def test_archive_rejects_extra_member(self) -> None:
        payload = canonical_bytes(self._payload())
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "receipt.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(RECEIPT_MEMBER, payload)
                archive.writestr("extra", b"x")
            with self.assertRaises(ReceiptError):
                inspect_single_member_archive(archive_path)

    def test_manual_receipt_binds_dispatch_caller_and_called_workflow(self) -> None:
        payload = self._payload()
        for identity in ("caller", "called"):
            payload[identity]["event"] = "workflow_dispatch"
            payload[identity]["workflow_name"] = "Platform production deploy"
            payload[identity]["workflow_path"] = ".github/workflows/platform-production-deploy.yml"
        self.assertIs(validate_receipt(payload), payload)
        payload["caller"]["workflow_name"] = "Platform production auto-deploy"
        with self.assertRaises(ReceiptError):
            validate_receipt(payload)


if __name__ == "__main__":
    unittest.main()
