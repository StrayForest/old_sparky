from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from tools import platform_host_tools_bundle as bundle


SOURCE_SHA = "a" * 40
SOURCE_HEAD_SHA = "b" * 40
PACKAGING_COMMIT = "c" * 40
ARTIFACT_ID = "123456"


@unittest.skipUnless(os.getuid() == 0, "installer filesystem tests require root")
class HostToolsInstallerTests(unittest.TestCase):
    def _artifact(self, root: Path) -> tuple[Path, dict[str, str]]:
        inner = root / "inner.zip"
        outer = root / "outer.zip"
        summary = bundle.build_bundle(Path(__file__).resolve().parents[2], SOURCE_SHA, inner)
        with zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_DEFLATED) as opened:
            opened.writestr(bundle.OUTER_MEMBER_NAME, inner.read_bytes())
        digests = {
            "outer": hashlib.sha256(outer.read_bytes()).hexdigest(),
            "inner": str(summary["bundle_sha256"]),
            "manifest": str(summary["manifest_sha256"]),
            "capabilities": str(summary["capabilities_sha256"]),
        }
        return outer, digests

    def _attestation(self, path: Path, digests: dict[str, str]) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "status": "satisfied",
                    "verifier": "pinned-attestation-verifier@1",
                    "host_tools_sha": SOURCE_SHA,
                    "source_head_sha": SOURCE_HEAD_SHA,
                    "packaging_commit": PACKAGING_COMMIT,
                    "artifact_id": ARTIFACT_ID,
                    "outer_sha256": digests["outer"],
                    "inner_sha256": digests["inner"],
                },
                sort_keys=True,
            ),
            encoding="ascii",
        )

    def _install(self, root: Path, work: Path, *, evidence: Path | None = None) -> dict[str, object]:
        outer, digests = self._artifact(work)
        attestation = work / "attestation.json"
        self._attestation(attestation, digests)
        with patch.object(
            bundle,
            "_run_post_install_self_tests",
            return_value={"host-capabilities": "ok", "host-contract": "ok"},
        ):
            return bundle.install_bundle(
                outer,
                host_tools_root=root,
                expected_source_sha=SOURCE_SHA,
                expected_outer_sha256=digests["outer"],
                expected_inner_sha256=digests["inner"],
                expected_manifest_sha256=digests["manifest"],
                expected_capabilities_sha256=digests["capabilities"],
                attestation_evidence=attestation,
                source_head_sha=SOURCE_HEAD_SHA,
                packaging_commit=PACKAGING_COMMIT,
                artifact_id=ARTIFACT_ID,
                evidence_output=evidence,
            )

    def test_install_is_closed_root_owned_and_preserves_pointers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            host_root = work / "host-tools"
            host_root.mkdir()
            for pointer in ("current", "previous"):
                (host_root / pointer).write_text("preserve\n", encoding="ascii")
            evidence = work / "install-evidence.json"
            result = self._install(host_root, work, evidence=evidence)
            generation = host_root / SOURCE_SHA
            self.assertEqual(result["status"], "installed")
            self.assertEqual((host_root / "current").read_text(encoding="ascii"), "preserve\n")
            self.assertEqual((host_root / "previous").read_text(encoding="ascii"), "preserve\n")
            self.assertEqual({path.name for path in generation.iterdir()}, bundle.HOST_TOOLS_INVENTORY)
            self.assertEqual(stat.S_IMODE(generation.stat().st_mode), 0o555)
            for path in generation.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444 if path.name in {"manifest.json", "capabilities.txt"} else 0o555)
                self.assertEqual(path.stat().st_uid, 0)
                self.assertEqual(path.stat().st_gid, 0)
                self.assertEqual(path.stat().st_nlink, 1)
            payload = json.loads(evidence.read_text(encoding="ascii"))
            self.assertEqual(payload["provenance"]["host_tools_sha"], SOURCE_SHA)
            self.assertEqual(payload["provenance"]["source_head_sha"], SOURCE_HEAD_SHA)
            self.assertEqual(payload["provenance"]["packaging_commit"], PACKAGING_COMMIT)

    def test_existing_generation_is_no_overwrite_and_stage_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            host_root = work / "host-tools"
            host_root.mkdir()
            target = host_root / SOURCE_SHA
            target.mkdir()
            (target / "sentinel").write_text("keep\n", encoding="ascii")
            outer, digests = self._artifact(work)
            attestation = work / "attestation.json"
            self._attestation(attestation, digests)
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.install_bundle(
                    outer,
                    host_tools_root=host_root,
                    expected_source_sha=SOURCE_SHA,
                    expected_outer_sha256=digests["outer"],
                    expected_inner_sha256=digests["inner"],
                    expected_manifest_sha256=digests["manifest"],
                    expected_capabilities_sha256=digests["capabilities"],
                    attestation_evidence=attestation,
                    source_head_sha=SOURCE_HEAD_SHA,
                    packaging_commit=PACKAGING_COMMIT,
                    artifact_id=ARTIFACT_ID,
                )
            self.assertTrue((target / "sentinel").exists())
            self.assertEqual([entry.name for entry in host_root.iterdir()], [SOURCE_SHA])

    def test_publish_cross_device_and_race_fail_closed_without_replacing_target(self) -> None:
        for error in (bundle.HostToolsBundleError("host-tools generation already exists"), bundle.HostToolsBundleError("host-tools no-overwrite publish cannot be proven")):
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                host_root = work / "host-tools"
                host_root.mkdir()
                original = bundle._rename_noreplace
                with patch.object(bundle, "_rename_noreplace", side_effect=error):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        self._install(host_root, work)
                self.assertFalse((host_root / SOURCE_SHA).exists())
                self.assertEqual(list(host_root.iterdir()), [])
                self.assertIs(bundle._rename_noreplace, original)

    def test_partial_kernel_writes_are_completed_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            host_root = work / "host-tools"
            host_root.mkdir()
            original_write = os.write

            def short_write(descriptor: int, data: bytes) -> int:
                return original_write(descriptor, data[:7])

            with patch.object(bundle.os, "write", side_effect=short_write):
                result = self._install(host_root, work)
            self.assertEqual(result["status"], "installed")
            self.assertEqual(len(list((host_root / SOURCE_SHA).iterdir())), 15)

    def test_symlink_parent_non_root_and_provenance_confusion_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            real = work / "real"
            real.mkdir()
            host_root = work / "host-tools"
            host_root.symlink_to(real, target_is_directory=True)
            with self.assertRaises(bundle.HostToolsBundleError):
                self._install(host_root, work)
            with patch.object(bundle.os, "geteuid", return_value=1000), patch.object(bundle.os, "getuid", return_value=1000):
                with self.assertRaises(bundle.HostToolsBundleError):
                    self._install(real, work)
            outer, digests = self._artifact(work)
            attestation = work / "bad-attestation.json"
            self._attestation(attestation, digests)
            payload = json.loads(attestation.read_text(encoding="ascii"))
            payload["packaging_commit"] = SOURCE_HEAD_SHA
            attestation.write_text(json.dumps(payload), encoding="ascii")
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.install_bundle(
                    outer,
                    host_tools_root=real,
                    expected_source_sha=SOURCE_SHA,
                    expected_outer_sha256=digests["outer"],
                    expected_inner_sha256=digests["inner"],
                    expected_manifest_sha256=digests["manifest"],
                    expected_capabilities_sha256=digests["capabilities"],
                    attestation_evidence=attestation,
                    source_head_sha=SOURCE_HEAD_SHA,
                    packaging_commit=PACKAGING_COMMIT,
                    artifact_id=ARTIFACT_ID,
                )


class HostToolsOuterEnvelopeTests(unittest.TestCase):
    def test_outer_rejects_traversal_links_duplicates_and_bomb_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for label, members in {
                "traversal": [("../platform-host-tools-bundle.zip", b"x")],
                "duplicate": [(bundle.OUTER_MEMBER_NAME, b"x"), (bundle.OUTER_MEMBER_NAME, b"x")],
                "extra": [(bundle.OUTER_MEMBER_NAME, b"x"), ("extra", b"x")],
            }.items():
                with self.subTest(label=label):
                    archive = root / f"{label}.zip"
                    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as opened:
                        for name, data in members:
                            opened.writestr(name, data)
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle.verify_outer_bundle(archive)
            bomb = root / "bomb.zip"
            with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as opened:
                opened.writestr(bundle.OUTER_MEMBER_NAME, b"A" * (bundle.MAX_OUTER_MEMBER_BYTES // 2))
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_outer_bundle(bomb)

    def test_outer_extractor_is_no_overwrite_and_rejects_special_member(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "outer.zip"
            info = zipfile.ZipInfo(bundle.OUTER_MEMBER_NAME)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as opened:
                opened.writestr(info, b"inner")
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_outer_bundle(archive)


if __name__ == "__main__":
    unittest.main()
