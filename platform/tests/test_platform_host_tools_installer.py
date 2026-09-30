from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import struct
import subprocess
import sys
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

    def test_outer_link_interrupt_reconciles_exact_published_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            outer, digests = self._artifact(work)
            output = work / "linked-inner.zip"
            original_hook = bundle.INJECTION_HOOK

            def interrupt(point: str) -> None:
                if point in {
                    "linkat_after_success",
                    "outer_extract_after_link",
                    "outer_extract_before_parent_fsync",
                    "outer_extract_after_parent_fsync",
                }:
                    raise KeyboardInterrupt(point)

            bundle.INJECTION_HOOK = interrupt
            try:
                for point in (
                    "linkat_after_success",
                    "outer_extract_after_link",
                    "outer_extract_before_parent_fsync",
                    "outer_extract_after_parent_fsync",
                ):
                    with self.subTest(point=point):
                        with self.assertRaises(KeyboardInterrupt):
                            bundle.extract_outer_bundle(
                                outer,
                                output,
                                expected_outer_sha256=digests["outer"],
                                expected_inner_sha256=digests["inner"],
                            )
                        self.assertTrue(output.is_file())
                        self.assertEqual(output.stat().st_nlink, 1)
                        output.unlink()
            finally:
                bundle.INJECTION_HOOK = original_hook

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

    def test_evidence_is_final_commit_marker_and_retry_adopts_exact_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host_root = root / "host-tools"
            host_root.mkdir(mode=0o755)
            handoff = root / "secure-handoff"
            handoff.mkdir(mode=0o700)
            evidence = handoff / "evidence.json"
            payload = {"schema": bundle.PROVENANCE_SCHEMA, "status": "installed"}
            original_hook = bundle.INJECTION_HOOK

            def interrupt(point: str) -> None:
                if point == "evidence_after_link":
                    raise KeyboardInterrupt(point)

            bundle.INJECTION_HOOK = interrupt
            try:
                with self.assertRaises(KeyboardInterrupt):
                    bundle._write_evidence(evidence, payload, host_tools_root=host_root)
            finally:
                bundle.INJECTION_HOOK = original_hook
            self.assertTrue(evidence.is_file())
            first_inode = evidence.stat().st_ino
            adopted = bundle._write_evidence(evidence, payload, host_tools_root=host_root)
            self.assertEqual(adopted.metadata.st_ino, first_inode)
            self.assertEqual(evidence.read_bytes(), bundle._canonical_json(payload))
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle._write_evidence(
                    evidence,
                    {"schema": bundle.PROVENANCE_SCHEMA, "status": "different"},
                    host_tools_root=host_root,
                )

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
        opt_before = subprocess.check_output(
            ["/usr/bin/stat", "-c", "%d:%i:%u:%g:%a", "/opt"],
            text=True,
        ).strip()
        mountinfo_before = tuple(
            line
            for line in Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines()
            if len(line.split()) > 4 and line.split()[4] == "/opt"
        )
        host_generation = Path("/opt/oldsparky/platform/shared/host-tools") / SOURCE_SHA
        host_generation_before = (
            (host_generation.stat().st_dev, host_generation.stat().st_ino, host_generation.stat().st_mode)
            if host_generation.exists()
            else None
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
            self.assertEqual(list(payload["self_tests"]), ["host-capabilities", "host-contract"])
        opt_after = subprocess.check_output(
            ["/usr/bin/stat", "-c", "%d:%i:%u:%g:%a", "/opt"],
            text=True,
        ).strip()
        mountinfo_after = tuple(
            line
            for line in Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines()
            if len(line.split()) > 4 and line.split()[4] == "/opt"
        )
        self.assertEqual(opt_after, opt_before)
        self.assertEqual(mountinfo_after, mountinfo_before)
        if host_generation_before is None:
            self.assertFalse(host_generation.exists())
        else:
            self.assertEqual(
                (host_generation.stat().st_dev, host_generation.stat().st_ino, host_generation.stat().st_mode),
                host_generation_before,
            )

    def test_self_tests_keep_capabilities_before_contract_and_fail_closed(self) -> None:
        generation = Path("/tmp") / ("host-tools-self-test-" + SOURCE_SHA)
        calls: list[str] = []
        expected_capabilities = (
            "HOST_TOOLS schema=1 "
            f"source_sha={SOURCE_SHA} generation={SOURCE_SHA} "
            "dispatcher=2 artifact_prepare=2 supervisor=2 input_guard=1 "
            "python_isolated=1 python_bytecode_disabled=1\n"
        )
        expected_contract = (
            "HOST_TOOLS_CONTRACT "
            f"source_sha={SOURCE_SHA} generation={SOURCE_SHA} "
            f"manifest_sha256={'f' * 64} capabilities_sha256={'e' * 64}\n"
        )

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            action = "host-capabilities" if "host-capabilities" in command else "host-contract"
            calls.append(action)
            output = (
                expected_capabilities
                if action == "host-capabilities"
                else expected_contract
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=output.encode("ascii"),
                stderr=b"",
            )

        with patch.object(bundle.subprocess, "run", side_effect=fake_run):
            result = bundle._run_post_install_self_tests(
                generation,
                source_sha=SOURCE_SHA,
                manifest_sha256="f" * 64,
                capabilities_sha256="e" * 64,
            )
        self.assertEqual(calls, ["host-capabilities", "host-contract"])
        self.assertEqual(
            result,
            {
                "host-capabilities": expected_capabilities,
                "host-contract": expected_contract,
            },
        )

    def test_self_tests_reject_nonzero_malformed_stderr_and_oversize_output(self) -> None:
        cases = (
            (1, b"", b""),
            (0, b"malformed\n", b""),
            (0, b"", b"diagnostic\n"),
            (0, b"x" * 4097, b""),
            (0, b"", b"x" * 4097),
        )
        generation = Path("/tmp") / ("host-tools-self-test-negative-" + SOURCE_SHA)
        for returncode, stdout, stderr in cases:
            with self.subTest(returncode=returncode, stdout=len(stdout), stderr=len(stderr)):
                completed = subprocess.CompletedProcess(
                    ["/usr/bin/python3.12"],
                    returncode,
                    stdout=stdout,
                    stderr=stderr,
                )
                with patch.object(bundle.subprocess, "run", return_value=completed):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle._run_post_install_self_tests(
                            generation,
                            source_sha=SOURCE_SHA,
                            manifest_sha256="f" * 64,
                            capabilities_sha256="e" * 64,
                        )

    def test_evidence_failure_retains_generation_without_success_receipt(self) -> None:
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
            # Generation publication is durable/uncertain before evidence is
            # the final commit marker.  A failed evidence write therefore
            # retains the exact generation for reconciliation/retry, while
            # never leaving an ``installed`` receipt behind.
            self.assertTrue((host_root / SOURCE_SHA).exists())
            self.assertFalse(evidence.exists())
            self.assertEqual((host_root / "current").read_text(encoding="ascii"), "keep\n")
            self.assertEqual((host_root / "previous").read_text(encoding="ascii"), "keep\n")

    def test_install_retry_matrix_adopts_exact_generation_and_receipt_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            outer, digests = self._artifact(work)
            attestation = work / "attestation.json"
            receipt_sha256 = self._attestation(attestation, digests)
            handoff = work / "handoff"
            handoff.mkdir(mode=0o700)

            def install(root: Path, evidence: Path) -> dict[str, object]:
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

            host_root = work / "host-tools"
            host_root.mkdir()
            evidence = handoff / "install-evidence.json"
            old_hook = bundle.INJECTION_HOOK

            def uncertain_after_link(point: str) -> None:
                if point == "rename_noreplace_after_success":
                    raise KeyboardInterrupt(point)

            bundle.INJECTION_HOOK = uncertain_after_link
            try:
                with self.assertRaises(KeyboardInterrupt):
                    install(host_root, evidence)
            finally:
                bundle.INJECTION_HOOK = old_hook
            generation = host_root / SOURCE_SHA
            generation_inode = generation.stat().st_ino
            self.assertTrue(generation.is_dir())
            self.assertFalse(evidence.exists())

            # exact generation / missing evidence: full reverify and both
            # self-tests run, then one receipt is created.
            install(host_root, evidence)
            self.assertEqual(generation.stat().st_ino, generation_inode)
            evidence_inode = evidence.stat().st_ino
            install(host_root, evidence)
            self.assertEqual(generation.stat().st_ino, generation_inode)
            self.assertEqual(evidence.stat().st_ino, evidence_inode)

            conflicting = evidence.read_bytes()
            evidence.write_bytes(b"conflicting receipt\n")
            with self.assertRaises(bundle.HostToolsBundleError):
                install(host_root, evidence)
            self.assertEqual(evidence.read_bytes(), b"conflicting receipt\n")
            self.assertNotEqual(evidence.read_bytes(), conflicting)

            orphan_root = work / "orphan-host-tools"
            orphan_root.mkdir()
            orphan_evidence = handoff / "orphan-evidence.json"
            orphan_evidence.write_bytes(b"orphan\n")
            with self.assertRaises(bundle.HostToolsBundleError):
                install(orphan_root, orphan_evidence)
            self.assertFalse((orphan_root / SOURCE_SHA).exists())
            self.assertEqual(orphan_evidence.read_bytes(), b"orphan\n")

    def test_rename_interrupt_reconciles_exact_generation_without_stage_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            host_root = work / "host-tools"
            host_root.mkdir()
            original_hook = bundle.INJECTION_HOOK

            def interrupt(point: str) -> None:
                if point == "rename_noreplace_after_success":
                    raise KeyboardInterrupt(point)

            bundle.INJECTION_HOOK = interrupt
            try:
                with self.assertRaises(KeyboardInterrupt):
                    self._install(host_root, work)
            finally:
                bundle.INJECTION_HOOK = original_hook
            generation = host_root / SOURCE_SHA
            self.assertTrue(generation.is_dir())
            self.assertFalse(any(path.name.startswith(".host-tools-stage-") for path in host_root.iterdir()))

    def test_install_parent_fsync_interrupt_retains_published_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            host_root = work / "host-tools"
            host_root.mkdir()
            original_hook = bundle.INJECTION_HOOK

            def interrupt(point: str) -> None:
                if point in {"install_before_parent_fsync", "install_after_parent_fsync"}:
                    raise KeyboardInterrupt(point)

            bundle.INJECTION_HOOK = interrupt
            try:
                for point in ("install_before_parent_fsync", "install_after_parent_fsync"):
                    with self.subTest(point=point):
                        with self.assertRaises(KeyboardInterrupt):
                            self._install(host_root, work)
                        generation = host_root / SOURCE_SHA
                        self.assertTrue(generation.is_dir())
                        self.assertFalse(
                            any(path.name.startswith(".host-tools-stage-") for path in host_root.iterdir())
                        )
                        # Each subcase starts with the same exact generation;
                        # remove it before the next fresh publication.
                        shutil.rmtree(generation)
            finally:
                bundle.INJECTION_HOOK = original_hook

    def test_stage_name_replacement_is_quarantined_without_foreign_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            host_root = work / "host-tools"
            host_root.mkdir()
            token = "1" * 16
            old_hook = bundle.INJECTION_HOOK

            def replace_stage(point: str) -> None:
                if point == "new_stage_after_mkdir":
                    original = host_root / f".host-tools-stage-{SOURCE_SHA}-{token}"
                    foreign = host_root / "foreign-stage"
                    original.rename(foreign)
                    original.mkdir(mode=bundle.STAGE_MODE)

            bundle.INJECTION_HOOK = replace_stage
            try:
                with patch.object(bundle.secrets, "token_hex", return_value=token):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        self._install(host_root, work)
            finally:
                bundle.INJECTION_HOOK = old_hook
            foreign = host_root / "foreign-stage"
            replacement = host_root / f".host-tools-stage-{SOURCE_SHA}-{token}"
            self.assertTrue(foreign.is_dir())
            self.assertTrue(replacement.is_dir())
            self.assertEqual(list(foreign.iterdir()), [])
            self.assertEqual(list(replacement.iterdir()), [])

    def test_install_lock_serializes_cooperating_installers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "host-tools"
            root.mkdir()
            parent_fd, lock_fd = bundle._open_install_lock(root)
            try:
                with self.assertRaises(bundle.HostToolsBundleError):
                    bundle._open_install_lock(root)
                lock_path = root.parent / bundle.INSTALL_LOCK_NAME
                metadata = lock_path.stat()
                self.assertEqual(stat.S_IMODE(metadata.st_mode), bundle.INSTALL_LOCK_MODE)
                self.assertEqual(metadata.st_uid, 0)
                self.assertEqual(metadata.st_gid, 0)
                self.assertEqual(metadata.st_nlink, 1)
            finally:
                bundle._close_quietly(lock_fd)
                bundle._close_quietly(parent_fd)
            reopened_parent, reopened_lock = bundle._open_install_lock(root)
            bundle._close_quietly(reopened_lock)
            bundle._close_quietly(reopened_parent)

    def test_install_lock_contention_and_release_are_process_level(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "host-tools"
            root.mkdir()
            ready_read, ready_write = os.pipe()
            release_read, release_write = os.pipe()
            code = """
import os
import sys
from pathlib import Path
from tools import platform_host_tools_bundle as bundle

root = Path(sys.argv[1])
ready = int(sys.argv[2])
release = int(sys.argv[3])
parent, lock = bundle._open_install_lock(root)
os.write(ready, b"ready")
os.read(release, 1)
bundle._close_quietly(lock)
bundle._close_quietly(parent)
"""
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(root), str(ready_write), str(release_read)],
                cwd=Path(__file__).resolve().parents[2],
                env={
                    **os.environ,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "platform"),
                },
                pass_fds=(ready_write, release_read),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.close(ready_write)
            os.close(release_read)
            try:
                self.assertEqual(os.read(ready_read, len(b"ready")), b"ready")
                with self.assertRaises(bundle.HostToolsBundleError):
                    bundle._open_install_lock(root)
                os.write(release_write, b"x")
                stdout, stderr = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, stderr or stdout)
                parent, lock = bundle._open_install_lock(root)
                bundle._close_quietly(lock)
                bundle._close_quietly(parent)
            finally:
                os.close(ready_read)
                os.close(release_write)
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    def test_real_rename_noreplace_race_has_one_winner_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stages = []
            for label in ("stage-a", "stage-b"):
                stage = root / label
                stage.mkdir(mode=0o700)
                (stage / "winner").write_text(label, encoding="ascii")
                stages.append(stage)
            target = root / "target"
            start_read, start_write = os.pipe()
            ready = []
            results = []
            code = """
import os
import sys
from pathlib import Path
from tools import platform_host_tools_bundle as bundle

root, source, target = map(Path, sys.argv[1:4])
start = int(sys.argv[4])
ready_fd = int(sys.argv[5])
result = int(sys.argv[6])
parent = os.open(root, os.O_RDONLY | bundle._require_os_flag("O_DIRECTORY"))
os.write(ready_fd, b"ready")
os.read(start, 1)
try:
    bundle._rename_noreplace(parent, source.name, target.name)
except BaseException as exc:
    os.write(result, ("error:" + type(exc).__name__ + "\\n").encode("ascii"))
else:
    os.write(result, b"ok\\n")
finally:
    os.close(parent)
"""
            children = []
            try:
                for stage in stages:
                    ready_read, ready_write = os.pipe()
                    result_read, result_write = os.pipe()
                    ready.append(ready_read)
                    results.append(result_read)
                    child = subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            code,
                            str(root),
                            str(stage),
                            str(target),
                            str(start_read),
                            str(ready_write),
                            str(result_write),
                        ],
                        cwd=Path(__file__).resolve().parents[2],
                        env={
                            **os.environ,
                            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "platform"),
                        },
                        pass_fds=(start_read, ready_write, result_write),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    children.append(child)
                    os.close(ready_write)
                    os.close(result_write)
                for descriptor in ready:
                    self.assertEqual(os.read(descriptor, len(b"ready")), b"ready")
                os.write(start_write, b"xx")
                reports = [
                    os.read(descriptor, 128).decode("ascii") for descriptor in results
                ]
                for child in children:
                    stdout, stderr = child.communicate(timeout=10)
                    self.assertEqual(child.returncode, 0, stderr or stdout)
                self.assertEqual(sum(report == "ok\n" for report in reports), 1, reports)
                self.assertEqual(sum(report.startswith("error:") for report in reports), 1, reports)
                self.assertTrue(target.is_dir())
                self.assertEqual(
                    len(tuple(stage for stage in stages if stage.exists())), 1
                )
                self.assertEqual(
                    (target / "winner").read_text(encoding="ascii"),
                    next(stage.name for stage in stages if not stage.exists()),
                )
            finally:
                os.close(start_read)
                os.close(start_write)
                for descriptor in ready + results:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)

    def test_missing_primitives_and_device_mismatch_fail_closed(self) -> None:
        for name in ("O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW", "O_EXCL", "O_TMPFILE"):
            with self.subTest(primitive=name), patch.object(bundle.os, name, None):
                with self.assertRaises(bundle.HostToolsBundleError):
                    bundle._require_install_primitives()
        with patch.object(bundle.ctypes, "CDLL", return_value=object()):
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


class HostToolsTrustedOutputTests(unittest.TestCase):
    def test_outer_extraction_uses_current_unprivileged_owner(self) -> None:
        """The secret-free output policy is distinct from root installation."""

        if os.getuid() != 0:
            self.skipTest("the focused runner is already unprivileged")
        runuser = shutil.which("runuser")
        if runuser is None:
            self.fail("LOCAL GATE BLOCKED: runuser is required for the unprivileged output test")
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            outer, digests = HostToolsInstallerTests()._artifact(work)
            output_dir = work / "unprivileged-output"
            output_dir.mkdir(mode=0o700)
            # The repository checkout lives below /home/runner in CI and is
            # intentionally not traversable by nobody.  Copy only the
            # stdlib-only trusted helper into an accessible, mode-755 test
            # directory and exercise it with the system interpreter.
            accessible = work / "accessible"
            tools_dir = accessible / "tools"
            tools_dir.mkdir(parents=True, mode=0o755)
            shutil.copy2(bundle.__file__, tools_dir / "platform_host_tools_bundle.py")
            os.chmod(accessible, 0o755)
            os.chmod(tools_dir, 0o755)
            os.chmod(tools_dir / "platform_host_tools_bundle.py", 0o644)
            os.chown(work, 65534, 65534)
            os.chown(outer, 65534, 65534)
            os.chown(output_dir, 65534, 65534)
            os.chmod(work, 0o755)
            os.chmod(outer, 0o644)
            script = (
                "from pathlib import Path; "
                "from tools import platform_host_tools_bundle as b; "
                "import os, sys; "
                "p=Path(sys.argv[1]); "
                "b.extract_outer_bundle(p/'outer-0.zip', p/'unprivileged-output'/'inner.zip', "
                "expected_outer_sha256=sys.argv[2], expected_inner_sha256=sys.argv[3]); "
                "q=p/'unprivileged-output'/'inner.zip'; "
                "assert q.stat().st_uid == os.getuid() and q.stat().st_gid == os.getgid(); "
                "assert (q.stat().st_mode & 0o777) == 0o600"
            )
            completed = subprocess.run(
                [
                    runuser,
                    "-u",
                    "nobody",
                    "--",
                    "/usr/bin/python3",
                    "-c",
                    script,
                    str(work),
                    digests["outer"],
                    digests["inner"],
                ],
                env={**os.environ, "PYTHONPATH": str(accessible)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


class ReleaseArtifactRawZipTests(unittest.TestCase):
    def test_open_input_archive_closes_parent_and_input_on_all_open_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "input.zip"
            archive.write_bytes(b"not a zip")
            real_open = os.open
            real_close = os.close

            def exercise(failure: BaseException, *, stat_failure: bool) -> list[int]:
                parent_fd = real_open(root, os.O_RDONLY | bundle._require_os_flag("O_DIRECTORY"))
                source_fd = real_open(archive, os.O_RDONLY)
                parent_metadata = os.fstat(parent_fd)
                close_calls: list[int] = []

                def fake_open(name: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
                    if name == archive.name and dir_fd == parent_fd:
                        return source_fd
                    return real_open(name, flags, mode, dir_fd=dir_fd)

                def counted_close(descriptor: int) -> None:
                    close_calls.append(descriptor)
                    real_close(descriptor)

                patches = [
                    patch.object(
                        bundle,
                        "_open_no_symlink_directory",
                        return_value=(parent_fd, parent_metadata),
                    ),
                    patch.object(bundle.os, "open", side_effect=fake_open),
                    patch.object(bundle.os, "close", side_effect=counted_close),
                ]
                if stat_failure:
                    patches.append(patch.object(bundle.os, "stat", side_effect=failure))
                else:
                    patches.append(patch.object(bundle.os, "fstat", side_effect=failure))
                try:
                    with patches[0], patches[1], patches[2], patches[3]:
                        expected_error = (
                            bundle.HostToolsBundleError
                            if isinstance(failure, OSError)
                            else type(failure)
                        )
                        with self.assertRaises(expected_error) as raised:
                            bundle._open_input_archive(archive)
                        if isinstance(failure, OSError):
                            self.assertIs(raised.exception.__cause__, failure)
                        else:
                            self.assertIs(raised.exception, failure)
                finally:
                    # The helper owns both descriptors on every unsuccessful
                    # path; do not close them a second time in the test.
                    pass
                self.assertEqual(sorted(close_calls), sorted([parent_fd, source_fd]))
                return close_calls

            exercise(OSError("fstat failure"), stat_failure=False)
            exercise(OSError("stat failure"), stat_failure=True)
            exercise(KeyboardInterrupt("interrupt"), stat_failure=False)

    def _zip_fixture(
        self,
        root: Path,
        slug: str,
        members: list[tuple[str, bytes, int | None]],
    ) -> tuple[Path, str]:
        archive = root / "release-api.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as opened:
            for name, data, external_attr in members:
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                if external_attr is not None:
                    info.create_system = 3
                    info.external_attr = external_attr
                opened.writestr(info, data)
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def _mark_member_encrypted(self, archive: Path, member_name: str) -> None:
        """Set the ZIP encryption flag in both local and central headers."""

        payload = bytearray(archive.read_bytes())
        for signature, flag_offset, name_offset in (
            (b"PK\x03\x04", 6, 26),
            (b"PK\x01\x02", 8, 28),
        ):
            cursor = 0
            found = False
            while True:
                cursor = payload.find(signature, cursor)
                if cursor < 0:
                    break
                name_size = struct.unpack_from("<H", payload, cursor + name_offset)[0]
                name_start = cursor + (30 if signature == b"PK\x03\x04" else 46)
                name = bytes(payload[name_start : name_start + name_size]).decode("utf-8")
                if name == member_name:
                    flags = struct.unpack_from("<H", payload, cursor + flag_offset)[0]
                    struct.pack_into("<H", payload, cursor + flag_offset, flags | 0x1)
                    found = True
                    break
                cursor += 4
            self.assertTrue(found, (signature, member_name))
        archive.write_bytes(payload)

    def _zip64_fixture(self, root: Path, slug: str) -> tuple[Path, str]:
        archive = root / "release-api-zip64.zip"
        names = (
            f"{slug}.tar.gz",
            f"{slug}.tar.gz.sha256",
            "RELEASE.provenance.json",
        )
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=False) as opened:
            for name in names:
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                if name == names[0]:
                    # A ZIP64 extra field is explicit even though this small
                    # fixture does not need ZIP64 sizes.
                    info.extra = struct.pack("<HHQQ", 0x0001, 16, 0, 0)
                opened.writestr(info, b"zip64 fixture")
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def test_raw_api_release_zip_round_trip_is_exact_three_member_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            slug = "gha-123-4-abcdefabcdef"
            names = (
                f"{slug}.tar.gz",
                f"{slug}.tar.gz.sha256",
                "RELEASE.provenance.json",
            )
            payloads = {
                names[0]: b"release archive bytes\n",
                names[1]: f"{'a' * 64}  {names[0]}\n".encode("ascii"),
                names[2]: b'{"schema":1}\n',
            }
            archive, digest = self._zip_fixture(
                root,
                slug,
                [(name, payloads[name], None) for name in names],
            )
            summary = bundle.extract_release_artifact(
                archive,
                root / "release",
                release_slug=slug,
                expected_archive_sha256=digest,
            )
            output = root / "release"
            self.assertEqual(set(path.name for path in output.iterdir()), set(names))
            self.assertEqual(
                {path.name: path.read_bytes() for path in output.iterdir()}, payloads
            )
            self.assertEqual(summary["archive_sha256"], digest)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            for path in output.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.stat().st_uid, os.getuid())
                self.assertEqual(path.stat().st_gid, os.getgid())

    def test_raw_api_release_zip_rejects_explicit_zip64_and_encrypted_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            slug = "gha-777-1-abcdefabcdef"
            zip64_archive, zip64_digest = self._zip64_fixture(root, slug)
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.extract_release_artifact(
                    zip64_archive,
                    root / "release-zip64",
                    release_slug=slug,
                    expected_archive_sha256=zip64_digest,
                )

            names = (
                f"{slug}.tar.gz",
                f"{slug}.tar.gz.sha256",
                "RELEASE.provenance.json",
            )
            encrypted_archive, _ = self._zip_fixture(
                root,
                slug,
                [(name, b"encrypted fixture", None) for name in names],
            )
            self._mark_member_encrypted(encrypted_archive, names[0])
            encrypted_digest = hashlib.sha256(encrypted_archive.read_bytes()).hexdigest()
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.extract_release_artifact(
                    encrypted_archive,
                    root / "release-encrypted",
                    release_slug=slug,
                    expected_archive_sha256=encrypted_digest,
                )

    def test_raw_api_snapshot_survives_same_inode_same_size_source_rewrite(self) -> None:
        """Parsing must consume the fsynced snapshot, never the mutable source fd."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            slug = "gha-321-2-abcdefabcdef"
            names = (
                f"{slug}.tar.gz",
                f"{slug}.tar.gz.sha256",
                "RELEASE.provenance.json",
            )
            payloads = {
                names[0]: b"verified release bytes\n",
                names[1]: f"{'b' * 64}  {names[0]}\n".encode("ascii"),
                names[2]: b'{"schema":1}\n',
            }
            archive, digest = self._zip_fixture(
                root,
                slug,
                [(name, payloads[name], None) for name in names],
            )
            original = archive.read_bytes()
            before = archive.stat()
            old_hook = bundle.INJECTION_HOOK

            def rewrite_source(point: str) -> None:
                if point == "release_extract_after_snapshot":
                    with archive.open("r+b") as source:
                        source.seek(0)
                        source.write(b"X")  # same inode/size, now an invalid ZIP

            bundle.INJECTION_HOOK = rewrite_source
            try:
                summary = bundle.extract_release_artifact(
                    archive,
                    root / "release",
                    release_slug=slug,
                    expected_archive_sha256=digest,
                )
            finally:
                bundle.INJECTION_HOOK = old_hook
            after = archive.stat()
            self.assertEqual(
                (after.st_dev, after.st_ino, after.st_size),
                (before.st_dev, before.st_ino, before.st_size),
            )
            self.assertNotEqual(archive.read_bytes(), original)
            self.assertEqual(
                (root / "release" / names[0]).read_bytes(), payloads[names[0]]
            )
            self.assertEqual(summary["archive_sha256"], digest)

    def test_raw_api_release_zip_rejects_bad_members_and_retains_uncertain_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            slug = "gha-123-4-abcdefabcdef"
            expected = {
                f"{slug}.tar.gz": b"archive",
                f"{slug}.tar.gz.sha256": b"checksum",
                "RELEASE.provenance.json": b"provenance",
            }
            cases = {
                "traversal": [
                    ("../escape", b"x", None),
                    *[(name, data, None) for name, data in expected.items()],
                ],
                "extra": [
                    *[(name, data, None) for name, data in expected.items()],
                    ("extra", b"x", None),
                ],
                "link": [
                    (
                        name,
                        data,
                        ((stat.S_IFLNK | 0o777) << 16)
                        if name == f"{slug}.tar.gz"
                        else None,
                    )
                    for name, data in expected.items()
                ],
                "duplicate": [
                    *[(name, data, None) for name, data in expected.items()],
                    (f"{slug}.tar.gz", b"duplicate", None),
                ],
                "absolute": [
                    ("/absolute", b"x", None),
                    *[(name, data, None) for name, data in expected.items()],
                ],
                "backslash": [
                    ("nested\\escape", b"x", None),
                    *[(name, data, None) for name, data in expected.items()],
                ],
                "ratio": [
                    (f"{slug}.tar.gz", b"A" * 50_000, None),
                    (f"{slug}.tar.gz.sha256", b"checksum", None),
                    ("RELEASE.provenance.json", b"provenance", None),
                ],
            }
            for label, members in cases.items():
                with self.subTest(label=label):
                    archive, digest = self._zip_fixture(root, slug, members)
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle.extract_release_artifact(
                            archive,
                            root / f"release-{label}",
                            release_slug=slug,
                            expected_archive_sha256=digest,
                        )
            valid_archive, valid_digest = self._zip_fixture(
                root,
                slug,
                [(name, data, None) for name, data in expected.items()],
            )
            preexisting = root / "preexisting"
            preexisting.mkdir()
            (preexisting / "sentinel").write_bytes(b"keep")
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.extract_release_artifact(
                    valid_archive,
                    preexisting,
                    release_slug=slug,
                    expected_archive_sha256=valid_digest,
                )
            self.assertEqual((preexisting / "sentinel").read_bytes(), b"keep")
            output = root / "retained"
            original_hook = bundle.INJECTION_HOOK

            def interrupt(point: str) -> None:
                if point in {
                    "release_extract_after_rename",
                    "release_extract_before_parent_fsync",
                    "release_extract_after_parent_fsync",
                }:
                    raise KeyboardInterrupt(point)

            bundle.INJECTION_HOOK = interrupt
            try:
                for point in (
                    "release_extract_after_rename",
                    "release_extract_before_parent_fsync",
                    "release_extract_after_parent_fsync",
                ):
                    with self.subTest(point=point):
                        with self.assertRaises(KeyboardInterrupt):
                            bundle.extract_release_artifact(
                                valid_archive,
                                output,
                                release_slug=slug,
                                expected_archive_sha256=valid_digest,
                            )
                        self.assertTrue(output.is_dir())
                        self.assertEqual(
                            {path.name: path.read_bytes() for path in output.iterdir()}, expected
                        )
                        shutil.rmtree(output)
            finally:
                bundle.INJECTION_HOOK = original_hook


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
