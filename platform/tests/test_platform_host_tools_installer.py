from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from tools import platform_host_tools_bundle as bundle


SOURCE_SHA = "a" * 40
SOURCE_HEAD_SHA = "b" * 40
PACKAGING_COMMIT = "c" * 40
ARTIFACT_ID = "123456"
ARTIFACT_NAME = "platform-host-tools-bundle-10-1"
TRUSTED_SOURCE_SHA = "d" * 40
TESTED_MERGE_SHA = "e" * 40
SECURITY_RUN_ID = "20"
SECURITY_RUN_ATTEMPT = "1"


@unittest.skipUnless(os.getuid() == 0, "installer filesystem tests require root")
class HostToolsInstallerTests(unittest.TestCase):
    def _artifact(self, root: Path) -> tuple[Path, dict[str, str]]:
        suffix = len(tuple(root.glob("inner-*.zip")))
        inner = root / f"inner-{suffix}.zip"
        outer = root / f"outer-{suffix}.zip"
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

    def _attestation(self, path: Path, digests: dict[str, str]) -> str:
        payload = {
            "schema": bundle.PROVENANCE_SCHEMA,
            "status": "satisfied",
            "verifier": bundle.ATTESTATION_VERIFIER_ID,
            "issuer": bundle.ATTESTATION_ISSUER,
            "repository": bundle.ATTESTATION_REPOSITORY,
            "workflow_name": bundle.ATTESTATION_WORKFLOW_NAME,
            "workflow_path": bundle.ATTESTATION_WORKFLOW_PATH,
            "ref": bundle.ATTESTATION_REF,
            "event": bundle.ATTESTATION_EVENT,
            "run_id": "10",
            "run_attempt": "1",
            "artifact_id": ARTIFACT_ID,
            "artifact_name": ARTIFACT_NAME,
            "outer_sha256": digests["outer"],
            "subject_name": bundle.OUTER_MEMBER_NAME,
            "inner_sha256": digests["inner"],
            "security_run_id": SECURITY_RUN_ID,
            "security_run_attempt": SECURITY_RUN_ATTEMPT,
            "host_tools_sha": SOURCE_SHA,
            "source_head_sha": SOURCE_HEAD_SHA,
            "trusted_source_sha": TRUSTED_SOURCE_SHA,
            "tested_merge_sha": TESTED_MERGE_SHA,
            "packaging_commit": PACKAGING_COMMIT,
        }
        raw = bundle._canonical_json(payload)
        path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def _install(self, root: Path, work: Path, *, evidence: Path | None = None) -> dict[str, object]:
        outer, digests = self._artifact(work)
        attestation = work / "attestation.json"
        receipt_sha256 = self._attestation(attestation, digests)
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
                artifact_name=ARTIFACT_NAME,
                trusted_source_sha=TRUSTED_SOURCE_SHA,
                tested_merge_sha=TESTED_MERGE_SHA,
                security_run_id=SECURITY_RUN_ID,
                security_run_attempt=SECURITY_RUN_ATTEMPT,
                expected_receipt_sha256=receipt_sha256,
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
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle._verify_generation_dir(
                    generation,
                    expected_source_sha=SOURCE_SHA,
                    expected_manifest_sha256=result["manifest_sha256"],
                    expected_capabilities_sha256=result["capabilities_sha256"],
                    expected_device=generation.stat().st_dev + 1,
                )
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
            receipt_sha256 = self._attestation(attestation, digests)
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
                    artifact_name=ARTIFACT_NAME,
                    trusted_source_sha=TRUSTED_SOURCE_SHA,
                    tested_merge_sha=TESTED_MERGE_SHA,
                    security_run_id=SECURITY_RUN_ID,
                    security_run_attempt=SECURITY_RUN_ATTEMPT,
                    expected_receipt_sha256=receipt_sha256,
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

    def test_signal_abort_preserves_original_and_cleans_partial_outer_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            outer, digests = self._artifact(work)
            for signal in (KeyboardInterrupt(), SystemExit(17)):
                output = work / f"partial-{type(signal).__name__}.zip"
                with patch.object(bundle, "_write_all", side_effect=signal):
                    with self.assertRaises(type(signal)):
                        bundle.extract_outer_bundle(
                            outer,
                            output,
                            expected_outer_sha256=digests["outer"],
                            expected_inner_sha256=digests["inner"],
                        )
                self.assertFalse(output.exists())

    def test_signal_abort_preserves_original_and_cleans_partial_stage_member(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            stage.mkdir(mode=0o700)
            descriptor = os.open(stage, os.O_RDONLY | bundle._require_os_flag("O_DIRECTORY"))
            try:
                for signal in (KeyboardInterrupt(), SystemExit(19)):
                    with self.subTest(signal=type(signal).__name__):
                        with patch.object(bundle, "_write_all", side_effect=signal):
                            with self.assertRaises(type(signal)):
                                bundle._write_stage_member(
                                    descriptor,
                                    "manifest.json",
                                    b"{}",
                                    bundle.DATA_MODE,
                                    expected_device=os.fstat(descriptor).st_dev,
                                )
                        self.assertFalse((stage / "manifest.json").exists())
            finally:
                os.close(descriptor)

    def test_stage_and_evidence_paths_preserve_preexisting_links_and_specials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            stage.mkdir(mode=0o700)
            descriptor = os.open(stage, os.O_RDONLY | bundle._require_os_flag("O_DIRECTORY"))
            try:
                peer = root / "peer"
                peer.write_bytes(b"keep")
                hardlink = stage / "manifest.json"
                os.link(peer, hardlink)
                with self.assertRaises(bundle.HostToolsBundleError):
                    bundle._write_stage_member(
                        descriptor,
                        "manifest.json",
                        b"new",
                        bundle.DATA_MODE,
                        expected_device=os.fstat(descriptor).st_dev,
                    )
                self.assertEqual(hardlink.read_bytes(), b"keep")
                hardlink.unlink()
                hardlink.symlink_to(peer)
                with self.assertRaises(bundle.HostToolsBundleError):
                    bundle._write_stage_member(
                        descriptor,
                        "manifest.json",
                        b"new",
                        bundle.DATA_MODE,
                        expected_device=os.fstat(descriptor).st_dev,
                    )
                self.assertTrue(hardlink.is_symlink())
            finally:
                os.close(descriptor)

            handoff = root / "handoff"
            handoff.mkdir(mode=0o700)
            evidence = handoff / "evidence.json"
            peer = root / "evidence-peer"
            peer.write_bytes(b"keep")
            os.link(peer, evidence)
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle._write_evidence(
                    evidence,
                    {"schema": bundle.PROVENANCE_SCHEMA},
                    host_tools_root=root / "host-tools",
                )
            self.assertEqual(evidence.read_bytes(), b"keep")

    def test_evidence_write_preserves_signal_and_cleans_only_partial_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host_root = root / "host-tools"
            host_root.mkdir(mode=0o755)
            handoff = root / "secure-handoff"
            handoff.mkdir(mode=0o700)
            for signal in (KeyboardInterrupt(), SystemExit(23)):
                evidence = handoff / f"signal-{type(signal).__name__}.json"
                with self.subTest(signal=type(signal).__name__):
                    with patch.object(bundle, "_write_all", side_effect=signal):
                        with self.assertRaises(type(signal)):
                            bundle._write_evidence(
                                evidence,
                                {"schema": bundle.PROVENANCE_SCHEMA},
                                host_tools_root=host_root,
                            )
                    self.assertFalse(evidence.exists())

    def test_evidence_output_under_generation_is_rejected_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host_root = root / "host-tools"
            host_root.mkdir(mode=0o755)
            generation = host_root / SOURCE_SHA
            generation.mkdir(mode=0o555)
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle._validate_evidence_output(
                    generation / "host-tools-install-evidence.json",
                    host_root,
                )

    def test_provenance_receipt_v2_is_closed_digest_bound_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _outer, digests = self._artifact(root)
            receipt = root / "receipt.json"
            expected = self._attestation(receipt, digests)
            valid = json.loads(receipt.read_text(encoding="ascii"))

            def validate(path: Path, receipt_digest: str) -> None:
                bundle._validate_provenance(
                    path,
                    host_tools_sha=SOURCE_SHA,
                    source_head_sha=SOURCE_HEAD_SHA,
                    packaging_commit=PACKAGING_COMMIT,
                    outer_sha256=digests["outer"],
                    inner_sha256=digests["inner"],
                    artifact_id=ARTIFACT_ID,
                    artifact_name=ARTIFACT_NAME,
                    trusted_source_sha=TRUSTED_SOURCE_SHA,
                    tested_merge_sha=TESTED_MERGE_SHA,
                    security_run_id=SECURITY_RUN_ID,
                    security_run_attempt=SECURITY_RUN_ATTEMPT,
                    expected_receipt_sha256=receipt_digest,
                )

            validate(receipt, expected)
            for label, mutation in (
                ("unknown", lambda payload: {**payload, "unknown": "reject"}),
                ("missing", lambda payload: {key: value for key, value in payload.items() if key != "inner_sha256"}),
                ("issuer", lambda payload: {**payload, "issuer": "https://evil.invalid"}),
                ("verifier", lambda payload: {**payload, "verifier": "unapproved"}),
                ("run", lambda payload: {**payload, "run_id": "99"}),
                ("attempt", lambda payload: {**payload, "run_attempt": "2"}),
                ("artifact", lambda payload: {**payload, "artifact_id": "999"}),
                ("outer", lambda payload: {**payload, "outer_sha256": "0" * 64}),
                ("inner", lambda payload: {**payload, "inner_sha256": "0" * 64}),
                ("C", lambda payload: {**payload, "host_tools_sha": "0" * 40}),
                ("E", lambda payload: {**payload, "source_head_sha": "0" * 40}),
                ("T", lambda payload: {**payload, "trusted_source_sha": "0" * 40}),
                ("M", lambda payload: {**payload, "tested_merge_sha": "0" * 40}),
                ("security", lambda payload: {**payload, "security_run_attempt": "2"}),
            ):
                with self.subTest(receipt_field=label):
                    mutated = root / f"receipt-{label}.json"
                    mutated.write_bytes(bundle._canonical_json(mutation(valid)))
                    with self.assertRaises(bundle.HostToolsBundleError):
                        validate(mutated, hashlib.sha256(mutated.read_bytes()).hexdigest())

            duplicate = root / "receipt-duplicate.json"
            fields = list(valid.items()) + [("schema", bundle.PROVENANCE_SCHEMA)]
            duplicate.write_bytes(
                (
                    "{"
                    + ",".join(
                        json.dumps(key, ensure_ascii=True)
                        + ":"
                        + json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                        for key, value in fields
                    )
                    + "}\n"
                ).encode("ascii")
            )
            with self.assertRaises(bundle.HostToolsBundleError):
                validate(duplicate, hashlib.sha256(duplicate.read_bytes()).hexdigest())

    def test_actual_installer_self_tests_run_inside_private_opt_namespace(self) -> None:
        """Exercise the fixed /opt generation without touching the host mount."""

        required = ("/usr/bin/unshare", "/usr/bin/mount", "/usr/bin/python3.12")
        missing = [path for path in required if not Path(path).is_file()]
        if missing:
            self.fail(
                "LOCAL GATE BLOCKED: missing privileged namespace prerequisite "
                + ", ".join(missing)
            )
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            outer, digests = self._artifact(work)
            attestation = work / "attestation.json"
            receipt_sha256 = self._attestation(attestation, digests)
            handoff = work / "secure-handoff"
            handoff.mkdir(mode=0o700)
            evidence = handoff / "host-tools-install-evidence.json"
            helper = Path(bundle.__file__).resolve()
            args = [
                "/usr/bin/python3.12",
                "-I",
                "-B",
                str(helper),
                "install",
                "--outer-bundle",
                str(outer),
                "--host-tools-root",
                "/opt/oldsparky/platform/shared/host-tools",
                "--expected-source-sha",
                SOURCE_SHA,
                "--expected-outer-sha256",
                digests["outer"],
                "--expected-inner-sha256",
                digests["inner"],
                "--expected-manifest-sha256",
                digests["manifest"],
                "--expected-capabilities-sha256",
                digests["capabilities"],
                "--artifact-id",
                ARTIFACT_ID,
                "--attestation-evidence",
                str(attestation),
                "--source-head-sha",
                SOURCE_HEAD_SHA,
                "--packaging-commit",
                PACKAGING_COMMIT,
                "--artifact-name",
                ARTIFACT_NAME,
                "--trusted-source-sha",
                TRUSTED_SOURCE_SHA,
                "--tested-merge-sha",
                TESTED_MERGE_SHA,
                "--security-run-id",
                SECURITY_RUN_ID,
                "--security-run-attempt",
                SECURITY_RUN_ATTEMPT,
                "--expected-receipt-sha256",
                receipt_sha256,
                "--evidence-output",
                str(evidence),
            ]
            command = "\n".join(
                (
                    "set -eu",
                    "test -d /opt && test ! -L /opt",
                    "/usr/bin/mount -t tmpfs -o mode=0755 tmpfs /opt",
                    "/usr/bin/mkdir -p -m 0755 /opt/oldsparky/platform/shared/host-tools",
                    "exec " + " ".join(shlex.quote(value) for value in args),
                )
            )
            completed = subprocess.run(
                [
                    "/usr/bin/unshare",
                    "--mount",
                    "--fork",
                    "--propagation",
                    "private",
                    "/bin/sh",
                    "-eu",
                    "-c",
                    command,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=45,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(evidence.read_text(encoding="ascii"))
            self.assertEqual(payload["schema"], bundle.PROVENANCE_SCHEMA)
            self.assertEqual(
                payload["self_tests"],
                {"host-capabilities": "passed", "host-contract": "passed"},
            )

    def test_evidence_failure_rolls_back_only_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            host_root = work / "host-tools"
            host_root.mkdir()
            (host_root / "current").write_text("keep\n", encoding="ascii")
            (host_root / "previous").write_text("keep\n", encoding="ascii")
            evidence = work / "handoff" / "evidence.json"
            evidence.parent.mkdir(mode=0o700)
            outer, digests = self._artifact(work)
            attestation = work / "attestation.json"
            receipt_sha256 = self._attestation(attestation, digests)
            with patch.object(bundle, "_write_evidence", side_effect=KeyboardInterrupt()):
                with patch.object(
                    bundle,
                    "_run_post_install_self_tests",
                    return_value={"host-capabilities": "ok", "host-contract": "ok"},
                ):
                    with self.assertRaises(KeyboardInterrupt):
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
                            artifact_name=ARTIFACT_NAME,
                            trusted_source_sha=TRUSTED_SOURCE_SHA,
                            tested_merge_sha=TESTED_MERGE_SHA,
                            security_run_id=SECURITY_RUN_ID,
                            security_run_attempt=SECURITY_RUN_ATTEMPT,
                            expected_receipt_sha256=receipt_sha256,
                            evidence_output=evidence,
                        )
            self.assertFalse((host_root / SOURCE_SHA).exists())
            self.assertEqual((host_root / "current").read_text(encoding="ascii"), "keep\n")
            self.assertEqual((host_root / "previous").read_text(encoding="ascii"), "keep\n")

    def test_missing_primitives_and_device_mismatch_fail_closed(self) -> None:
        for name in ("O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW", "O_EXCL"):
            with self.subTest(primitive=name), patch.object(bundle.os, name, None):
                with self.assertRaises(bundle.HostToolsBundleError):
                    bundle._require_install_primitives()
        for name in ("pread", "fchmod", "fchown", "fsync"):
            with self.subTest(primitive=name), patch.object(bundle.os, name, None):
                with self.assertRaises(bundle.HostToolsBundleError):
                    bundle._require_install_primitives()
        with patch.object(bundle.os, "supports_dir_fd", ()):
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle._require_install_primitives()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            stage.mkdir(mode=0o700)
            descriptor = os.open(stage, os.O_RDONLY | bundle._require_os_flag("O_DIRECTORY"))
            try:
                with self.assertRaises(bundle.HostToolsBundleError):
                    bundle._write_stage_member(
                        descriptor,
                        "manifest.json",
                        b"{}",
                        bundle.DATA_MODE,
                        expected_device=os.fstat(descriptor).st_dev + 1,
                    )
            finally:
                os.close(descriptor)
            self.assertFalse((stage / "manifest.json").exists())

    def test_real_rename_noreplace_eexist_and_injected_exdev_enosys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "stage"
            target = root / SOURCE_SHA
            source.mkdir(mode=0o700)
            target.mkdir(mode=0o555)
            parent_fd = os.open(root, os.O_RDONLY | bundle._require_os_flag("O_DIRECTORY"))
            try:
                with self.assertRaises(bundle.HostToolsBundleError):
                    bundle._rename_noreplace(parent_fd, source.name, target.name)
                self.assertTrue(source.exists())
                fake = type("FakeLibc", (), {})()
                fake.renameat2 = lambda *args: -1
                with patch.object(bundle.ctypes, "CDLL", return_value=fake), patch.object(
                    bundle.ctypes, "get_errno", return_value=errno.EXDEV
                ):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle._rename_noreplace(parent_fd, source.name, "other")
                fake_missing = type("MissingLibc", (), {})()
                with patch.object(bundle.ctypes, "CDLL", return_value=fake_missing):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle._rename_noreplace(parent_fd, source.name, "other")
            finally:
                os.close(parent_fd)

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
            receipt_sha256 = self._attestation(attestation, digests)
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
                    artifact_name=ARTIFACT_NAME,
                    trusted_source_sha=TRUSTED_SOURCE_SHA,
                    tested_merge_sha=TESTED_MERGE_SHA,
                    security_run_id=SECURITY_RUN_ID,
                    security_run_attempt=SECURITY_RUN_ATTEMPT,
                    expected_receipt_sha256=receipt_sha256,
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
            valid_outer, digests = HostToolsInstallerTests()._artifact(root)
            output = root / "existing-inner.zip"
            output.write_bytes(b"preserve")
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.extract_outer_bundle(
                    valid_outer,
                    output,
                    expected_outer_sha256=digests["outer"],
                    expected_inner_sha256=digests["inner"],
                )
            self.assertEqual(output.read_bytes(), b"preserve")
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
