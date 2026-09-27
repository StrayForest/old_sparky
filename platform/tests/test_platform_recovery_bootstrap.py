from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from unittest import mock

from tools import platform_recovery_bootstrap as recovery


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLATFORM_ROOT.parent
TOOLS = PLATFORM_ROOT / "tools"
SOURCE_SHA = "a" * 40

# This is the complete changed-file set of the recovery-bootstrap patch at
# the merge base.  Keep the real set here so the route test exercises the
# exact pull-request and trusted-dev-push inputs, including the host-key scan
# contract that is easy to omit from one of the independent consumers.
RECOVERY_BOOTSTRAP_PATCH_FILES = frozenset(
    {
        ".github/workflows/platform-production-autodeploy.yml",
        ".github/workflows/platform-production-recovery-bootstrap-abort.yml",
        ".github/workflows/platform-production-recovery-bootstrap-build.yml",
        ".github/workflows/platform-production-release-recover.yml",
        "platform/docs/README.md",
        "platform/docs/adr/recovery-bootstrap-retained-abort.md",
        "platform/docs/deployment-runbook.md",
        "platform/docs/release-state-machine.md",
        "platform/docs/test-suite-governance.md",
        "platform/tests/test_platform_live_qa_runtime_install.py",
        "platform/tests/test_platform_recovery_bootstrap.py",
        "platform/tests/test_platform_release_build_diagnostics.py",
        "platform/tests/test_platform_release_systemd_state.py",
        "platform/tests/test_platform_release_venv_rollback.py",
        "platform/tests/test_platform_ssh_host_key_scan.py",
        "platform/tools/platform_abort_retained_only.sh",
        "platform/tools/platform_build_live_qa_runtime.py",
        "platform/tools/platform_ci_classifier.py",
        "platform/tools/platform_live_qa_guard.py",
        "platform/tools/platform_live_qa_runtime_install.py",
        "platform/tools/platform_production_classifier_artifact.py",
        "platform/tools/platform_recovery_bootstrap.py",
        "platform/tools/platform_release_restore_runtime.sh",
        "platform/tools/platform_release_rollback.sh",
        "platform/tools/platform_release_transaction.py",
        "platform/tools/platform_test_catalog.py",
    }
)


def provenance() -> dict[str, object]:
    return {
        "repository": "StrayForest/old_sparky",
        "workflow": "Platform security and build",
        "job": "Verification contract",
        "run_id": "12345",
        "run_attempt": "2",
        "source_sha": SOURCE_SHA,
        "artifact_name": "platform-ci-route-12345-2",
        "artifact_sha256": "b" * 64,
        "deployable": False,
    }


class RecoveryBootstrapBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        (self.source / "platform" / "tools").mkdir(parents=True)
        for name in recovery.RECOVERY_FILES:
            source = TOOLS / name
            destination = self.source / "platform" / "tools" / name
            shutil.copy2(source, destination, follow_symlinks=False)
            destination.chmod(0o755 if name.endswith(".sh") else 0o644)
        self.bundle = self.root / "bundle.zip"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self) -> dict[str, object]:
        return recovery.build_bundle(
            self.source,
            source_sha=SOURCE_SHA,
            provenance=provenance(),
            output=self.bundle,
        )

    def test_build_is_closed_deterministic_and_digest_bound(self) -> None:
        first = self.build()
        original = self.bundle.read_bytes()
        second_bundle = self.root / "bundle-2.zip"
        second = recovery.build_bundle(
            self.source,
            source_sha=SOURCE_SHA,
            provenance=provenance(),
            output=second_bundle,
        )
        self.assertEqual(original, second_bundle.read_bytes())
        self.assertEqual(first["bundle_sha256"], second["bundle_sha256"])
        result = recovery.verify_bundle(
            self.bundle,
            expected_source_sha=SOURCE_SHA,
            expected_provenance=provenance(),
        )
        self.assertEqual(result["bundle_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(set(result["members"]), set(recovery.RECOVERY_FILES) | {"manifest.json"})
        self.assertFalse(result["manifest"]["deployable"])

    def test_source_symlink_hardlink_and_special_file_are_rejected(self) -> None:
        target = self.source / "platform" / "tools" / recovery.RECOVERY_FILES[0]
        target.unlink()
        target.symlink_to(TOOLS / recovery.RECOVERY_FILES[0])
        with self.assertRaises(recovery.RecoveryBootstrapError):
            self.build()
        target.unlink()
        os_link = self.source / "hardlink"
        os_link.write_bytes(b"x")
        target.hardlink_to(os_link)
        with self.assertRaises(recovery.RecoveryBootstrapError):
            self.build()
        target.unlink()
        target.mkdir()
        with self.assertRaises(recovery.RecoveryBootstrapError):
            self.build()

    def test_archive_rejects_traversal_duplicate_and_wrong_modes(self) -> None:
        self.build()
        def rewrite(mutator):
            altered = self.root / "altered.zip"
            with zipfile.ZipFile(self.bundle) as source, zipfile.ZipFile(altered, "w") as output:
                for info in source.infolist():
                    data = source.read(info)
                    mutator(output, info, data)
            return altered

        def duplicate(output, info, data):
            output.writestr(info, data)
            if info.filename.endswith("/manifest.json"):
                output.writestr(info, data)

        duplicate_archive = rewrite(duplicate)
        with self.assertRaises(recovery.RecoveryBootstrapError):
            recovery.verify_bundle(duplicate_archive)

        def traversal(output, info, data):
            if info.filename.endswith("/platform_recovery_bootstrap.py"):
                info.filename = "platform-recovery-bootstrap/../escape"
            output.writestr(info, data)

        traversal_archive = rewrite(traversal)
        with self.assertRaises(recovery.RecoveryBootstrapError):
            recovery.verify_bundle(traversal_archive)

        def wrong_mode(output, info, data):
            if info.filename.endswith("/platform_abort_retained_only.sh"):
                info.external_attr = (stat.S_IFREG | 0o444) << 16
            output.writestr(info, data)

        wrong_mode_archive = rewrite(wrong_mode)
        with self.assertRaises(recovery.RecoveryBootstrapError):
            recovery.verify_bundle(wrong_mode_archive)

        aggregate_archive = self.root / "aggregate-limit.zip"
        with zipfile.ZipFile(self.bundle) as source, zipfile.ZipFile(aggregate_archive, "w") as output:
            for info in source.infolist():
                data = source.read(info)
                if info.filename.endswith("/manifest.json"):
                    manifest = json.loads(data.decode("ascii"))
                    manifest["limits"]["max_total_member_bytes"] = 1
                    data = recovery._canonical_json(manifest)
                output.writestr(info, data)
        with mock.patch.object(recovery, "MAX_TOTAL_MEMBER_BYTES", 1):
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.verify_bundle(aggregate_archive)

    def test_manifest_duplicate_keys_and_provenance_schema_are_rejected(self) -> None:
        self.build()
        altered = self.root / "manifest-duplicate.zip"
        with zipfile.ZipFile(self.bundle) as source, zipfile.ZipFile(altered, "w") as output:
            for info in source.infolist():
                data = source.read(info)
                if info.filename.endswith("/manifest.json"):
                    data = data.replace(b'"schema":1', b'"schema":1,"schema":1')
                output.writestr(info, data)
        with self.assertRaises(recovery.RecoveryBootstrapError):
            recovery.verify_bundle(altered)

        bad = provenance()
        bad["deployable"] = True
        with self.assertRaises(recovery.RecoveryBootstrapError):
            recovery._provenance_schema(bad)


class RecoveryBootstrapInstallTests(unittest.TestCase):
    def test_install_uses_digest_generation_and_is_atomic(self) -> None:
        if not hasattr(recovery.os, "geteuid") or recovery.os.geteuid() != 0:
            self.skipTest("install contract requires root")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "platform" / "tools").mkdir(parents=True)
            for name in recovery.RECOVERY_FILES:
                shutil.copy2(TOOLS / name, source / "platform" / "tools" / name)
            bundle = root / "bundle.zip"
            result = recovery.build_bundle(source, source_sha=SOURCE_SHA, provenance=provenance(), output=bundle)
            app = root / "app"
            (app / "shared").mkdir(parents=True)
            app.chmod(0o755)
            (app / "shared").chmod(0o755)
            installed = recovery.install_bundle(bundle, app_dir=app, expected_bundle_sha=result["bundle_sha256"])
            self.assertEqual(installed.name, result["bundle_sha256"])
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o555)
            self.assertEqual(recovery.install_bundle(bundle, app_dir=app), installed)
            member = installed / "platform_recovery_bootstrap.py"
            member.chmod(0o644)
            tampered = member.read_bytes() + b"\n# tampered generation\n"
            member.write_bytes(tampered)
            member.chmod(0o444)
            manifest_path = installed / "manifest.json"
            manifest_path.chmod(0o644)
            manifest = json.loads(manifest_path.read_text(encoding="ascii"))
            for record in manifest["files"]:
                if record["path"] == member.name:
                    record["sha256"] = hashlib.sha256(tampered).hexdigest()
                    break
            manifest_path.write_bytes(recovery._canonical_json(manifest))
            manifest_path.chmod(0o444)
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.install_bundle(bundle, app_dir=app)
            self.assertFalse(any(path.name.startswith(".") for path in (app / "shared" / ".release-recovery" / "generations").iterdir()))


class RecoveryBootstrapContractTests(unittest.TestCase):
    @staticmethod
    def _set_assignment(source: str, name: str) -> frozenset[str]:
        tree = ast.parse(source)
        assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
        ]
        if len(assignments) != 1:
            raise AssertionError(f"expected one {name} assignment")
        value = assignments[0].value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
        ):
            if len(value.args) != 1 or value.keywords:
                raise AssertionError(f"{name} frozenset expression is malformed")
            value = value.args[0]
        parsed = ast.literal_eval(value)
        if not isinstance(parsed, (set, frozenset, list, tuple)):
            raise AssertionError(f"{name} is not a literal set")
        if not all(isinstance(item, str) for item in parsed):
            raise AssertionError(f"{name} contains a non-string path")
        return frozenset(parsed)

    @classmethod
    def _workflow_recovery_set(cls, source: str) -> frozenset[str]:
        marker = "          recovery_bootstrap_files = {\n"
        start = source.index(marker)
        end = source.index("          }\n", start) + len("          }\n")
        return cls._set_assignment(textwrap.dedent(source[start:end]), "recovery_bootstrap_files")

    @classmethod
    def _workflow_bundle_set(cls, source: str) -> frozenset[str]:
        marker = "          expected = {\n"
        start = source.index(marker)
        end = source.index("          }\n", start) + len("          }\n")
        return cls._set_assignment(textwrap.dedent(source[start:end]), "expected")

    def test_fixed_entrypoint_has_only_abort_retained_capability(self) -> None:
        script = (TOOLS / "platform_abort_retained_only.sh").read_text(encoding="utf-8")
        self.assertIn("abort_retained_only", script)
        self.assertNotIn("git checkout", script)
        self.assertNotIn("systemctl restart", script)
        self.assertNotIn("--downgrade", script)
        self.assertIn("--generation", script)

    def test_build_workflow_is_trusted_default_branch_secret_free_and_non_deployable(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-build.yml").read_text(encoding="utf-8")
        self.assertIn("github.event.workflow_run.head_branch == 'dev'", workflow)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", workflow)
        self.assertIn('"deployable":False', workflow)
        self.assertIn(
            'evidence_name="platform-recovery-bootstrap-evidence-${SOURCE_SHA}-${SECURITY_RUN_ID}-${SECURITY_RUN_ATTEMPT}.json"',
            workflow,
        )
        self.assertIn(
            'evidence="$RUNNER_TEMP/$evidence_name"',
            workflow,
        )
        self.assertIn(
            'path: ${{ runner.temp }}/${{ steps.bundle.outputs.evidence_name }}',
            workflow,
        )
        self.assertIn("actions: read", workflow)
        self.assertNotIn("PROD_SSH_KEY", workflow)
        self.assertNotIn("secrets.", workflow)

    def test_manual_abort_validates_artifacts_before_secrets_and_uses_bundle_only(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-abort.yml").read_text(encoding="utf-8")
        evidence = workflow.index("Validate exact successful security run")
        secrets = workflow.index("secrets.PROD_SSH_HOST")
        self.assertLess(evidence, secrets)
        self.assertIn("gh attestation verify", workflow)
        signer_identity = (
            "StrayForest/old_sparky/"
            ".github/workflows/platform-production-recovery-bootstrap-build.yml"
        )
        signer_lines = [
            line.strip()
            for line in workflow.splitlines()
            if "--signer-workflow" in line
        ]
        self.assertEqual(signer_lines, [f"--signer-workflow {signer_identity}"])
        self.assertNotIn(
            "--signer-workflow .github/workflows/platform-production-recovery-bootstrap-build.yml",
            workflow,
        )
        self.assertNotIn(
            "--signer-workflow wrong-owner/wrong-repo/.github/workflows/platform-production-recovery-bootstrap-build.yml",
            workflow,
        )
        self.assertIn("actions: read", workflow)
        self.assertIn("attestations: read", workflow)
        self.assertIn("ABORT-RECOVERY-BOOTSTRAP-RETAINED-ONLY", workflow)
        self.assertIn(
            "evidence_name=platform-recovery-bootstrap-evidence-{source_sha}-{run_id}-{attempt}.json\\n",
            workflow,
        )
        self.assertNotIn(
            "evidence_name=platform-recovery-bootstrap-evidence-{source_sha}-{run_id}-{attempt}\\n",
            workflow,
        )
        self.assertIn("platform_recovery_bootstrap.py\" install", workflow)
        self.assertIn("platform_abort_retained_only.sh", workflow)
        self.assertIn("platform_release_lock.sh\" --run", workflow)
        self.assertNotIn("git checkout", workflow)
        self.assertNotIn("platform_release_deploy", workflow)
        self.assertNotIn("downgrade", workflow)
        self.assertIn("for ssh_attempt in 1 2; do", workflow)
        self.assertIn('known_hosts.scan.1', workflow)
        self.assertIn('known_hosts.scan.2', workflow)
        self.assertIn('NF == 3 && $1 == host && $2 == "ssh-ed25519"', workflow)
        self.assertIn("ssh-keygen -lf \"$ssh_scan_attempt\" -E sha256", workflow)
        self.assertIn("mktemp -d --tmpdir=/tmp oldsparky-recovery-bootstrap.XXXXXX", workflow)
        self.assertIn("trap cleanup_remote_stage EXIT", workflow)
        self.assertIn("trap cleanup_stage EXIT", workflow)
        self.assertIn("os.lstat", workflow)
        self.assertIn("stat.S_ISREG", workflow)
        self.assertIn("stat.S_ISLNK", workflow)
        self.assertIn("metadata.st_uid != 0", workflow)
        self.assertIn("metadata.st_nlink != 1", workflow)
        self.assertGreaterEqual(workflow.count("metadata.st_nlink < 2"), 4)
        self.assertIn("os.O_EXCL", workflow)
        self.assertIn("os.O_NOFOLLOW", workflow)
        self.assertIn("source.infolist()", workflow)
        self.assertIn("info.create_system != 3", workflow)
        self.assertIn("len(set(names)) != len(names)", workflow)
        self.assertIn("MAX_COMPRESSION_RATIO", workflow)
        self.assertIn("MAX_TOTAL_MEMBER_BYTES", workflow)
        self.assertIn("evidence_name", workflow)
        self.assertIn("bundle_name", workflow)
        self.assertIn("primary_status=%s cleanup_status=%s", workflow)
        self.assertNotIn(
            'stage="/tmp/oldsparky-recovery-bootstrap-stage-${expected_sha}"',
            workflow,
        )
        self.assertNotIn("destination.write_bytes", workflow)
        expected = frozenset(
            f"{recovery.MEMBER_ROOT}/{name}"
            for name in ("manifest.json", *recovery.RECOVERY_FILES)
        )
        self.assertEqual(expected, self._workflow_bundle_set(workflow))
        loop = next(
            line.strip()
            for line in workflow.splitlines()
            if line.strip().startswith("for name in manifest.json ")
        )
        loop_names = frozenset(
            loop.removeprefix("for name in ").split("; do", 1)[0].split()
        )
        self.assertEqual(
            loop_names,
            frozenset(("manifest.json", *recovery.RECOVERY_FILES)),
        )
        self.assertNotIn("platform_build_live_qa_runtime.py", workflow)

        transaction_source = (
            TOOLS / "platform_release_transaction.py"
        ).read_text(encoding="utf-8")
        bootstrap_source = (
            TOOLS / "platform_recovery_bootstrap.py"
        ).read_text(encoding="utf-8")
        self.assertIn("--retain-receipt", transaction_source)
        retained_cleanup = bootstrap_source.index('"--retain-receipt"')
        systemd_clear = bootstrap_source.index('"clear", "--state"')
        final_cleanup = bootstrap_source.rindex('"complete-recovery"')
        self.assertLess(retained_cleanup, systemd_clear)
        self.assertLess(systemd_clear, final_cleanup)

        if os.geteuid() == 0:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                app = root / "app"
                releases = app / "releases"
                shared = app / "shared"
                current = releases / "current-release"
                previous = releases / "previous-release"
                candidate = releases / "candidate-release"
                releases.mkdir(parents=True)
                shared.mkdir()
                current.mkdir()
                previous.mkdir()
                candidate.mkdir()
                (app / "current").symlink_to(current)
                (app / "previous").symlink_to(previous)
                shared_venv = shared / "venv"
                shared_venv.mkdir()

                def identity(path: Path) -> dict[str, int]:
                    metadata = path.lstat()
                    return {"dev": metadata.st_dev, "ino": metadata.st_ino}

                receipt = {
                    "version": 2,
                    "operation": "install",
                    "phase": "recovery-restored",
                    "app_dir": str(app),
                    "current_before": str(current),
                    "previous_before": str(previous),
                    "candidate_release": str(candidate),
                    "shared_venv": str(shared_venv),
                    "peer": str(shared / ".venv-install-candidate-release.none"),
                    "snapshot": str(candidate / ".rollback/shared-venv-before-install"),
                    "transition": "none",
                    "shared_before": identity(shared_venv),
                    "peer_before": None,
                    "current_before_identity": identity(current),
                    "previous_before_identity": identity(previous),
                    "candidate_identity": identity(candidate),
                    "remove_env_on_recovery": False,
                    "service_state_before": {
                        "deadlock-api": "active",
                        "deadlock-worker": "inactive",
                        "deadlock-web": "active",
                    },
                    "quiesced_services": [
                        "deadlock-api",
                        "deadlock-worker",
                        "deadlock-web",
                    ],
                    "timer_active_before": False,
                }
                self.assertEqual(
                    recovery._validate_receipt_identity(receipt, app), current
                )

                candidate.rmdir()
                candidate.symlink_to(current)
                with self.assertRaises(recovery.RecoveryBootstrapError):
                    recovery._validate_receipt_identity(receipt, app)
                candidate.unlink()
                candidate.write_text("wrong object", encoding="ascii")
                with self.assertRaises(recovery.RecoveryBootstrapError):
                    recovery._validate_receipt_identity(receipt, app)
                candidate.unlink()
                receipt["phase"] = "prepared"
                with self.assertRaises(recovery.RecoveryBootstrapError):
                    recovery._validate_receipt_identity(receipt, app)

            # Exercise both durable cleanup boundaries with a one-shot fault
            # after the side effect. Each retry must skip already-cleared
            # state and remove only the final operation receipt.
            for failure_point in ("retain", "clear"):
                with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    app = root / "app"
                    releases = app / "releases"
                    shared = app / "shared"
                    candidate = releases / "candidate-release"
                    peer = shared / ".venv-install-candidate-release.none"
                    releases.mkdir(parents=True)
                    shared.mkdir()
                    candidate.mkdir()
                    peer.mkdir()
                    state = shared / ".release-operation.json"
                    systemd_state = shared / ".release-systemd-state.json"
                    state.write_text("{}", encoding="ascii")
                    systemd_state.write_text("{}", encoding="ascii")
                    receipt = {
                        "operation_id": "a" * 32,
                        "previous_before": None,
                        "candidate_release": str(candidate),
                        "peer": str(peer),
                    }
                    calls: list[list[str]] = []
                    failed = False

                    def fake_run(command: list[str], **_kwargs: object) -> None:
                        nonlocal failed
                        calls.append(command)
                        if "complete-recovery" in command:
                            if "--retain-receipt" in command:
                                if candidate.exists():
                                    candidate.rmdir()
                                if peer.exists():
                                    peer.rmdir()
                                if failure_point == "retain" and not failed:
                                    failed = True
                                    raise subprocess.CalledProcessError(1, command)
                            else:
                                state.unlink()
                        elif "clear" in command:
                            systemd_state.unlink()
                            if failure_point == "clear" and not failed:
                                failed = True
                                raise subprocess.CalledProcessError(1, command)

                    with (
                        mock.patch.object(recovery.os, "geteuid", return_value=0),
                        mock.patch.object(recovery, "_validate_generation_tree"),
                        mock.patch.object(recovery, "_receipt_json", return_value=receipt),
                        mock.patch.object(recovery, "_validate_receipt_identity", return_value=app / "releases" / "current-release"),
                        mock.patch.object(recovery, "_safe_receipt"),
                        mock.patch.object(recovery, "_release_pointer", return_value=app / "releases" / "current-release"),
                        mock.patch.object(recovery.subprocess, "run", side_effect=fake_run),
                    ):
                        with self.assertRaises(subprocess.CalledProcessError):
                            recovery.abort_retained_only(
                                app_dir=app,
                                generation=root / ("a" * 64),
                            )
                        self.assertTrue(state.exists())
                        if failure_point == "retain":
                            self.assertTrue(systemd_state.exists())
                        else:
                            self.assertFalse(systemd_state.exists())
                        recovery.abort_retained_only(
                            app_dir=app,
                            generation=root / ("a" * 64),
                        )
                    self.assertFalse(state.exists())
                    self.assertFalse(systemd_state.exists())
                    self.assertGreaterEqual(len(calls), 4)

    def test_legacy_v2_bridge_cleans_only_with_exact_no_systemd_state(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("legacy recovery bridge contract requires root")

        def make_case() -> tuple[Path, Path, Path, Path, dict[str, object]]:
            temporary = tempfile.TemporaryDirectory()
            root = Path(temporary.name)
            self.addCleanup(temporary.cleanup)
            app = root / "app"
            releases = app / "releases"
            shared = app / "shared"
            current = releases / "current-release"
            previous = releases / "previous-release"
            candidate = releases / "candidate-release"
            releases.mkdir(parents=True)
            shared.mkdir()
            current.mkdir()
            previous.mkdir()
            candidate.mkdir()
            (app / "current").symlink_to(current)
            (app / "previous").symlink_to(previous)
            shared_venv = shared / "venv"
            shared_venv.mkdir()
            peer = shared / ".venv-install-candidate-release.none"

            def identity(path: Path) -> dict[str, int]:
                metadata = path.lstat()
                return {"dev": metadata.st_dev, "ino": metadata.st_ino}

            receipt: dict[str, object] = {
                "version": 2,
                "operation": "install",
                "phase": "recovery-restored",
                "app_dir": str(app),
                "current_before": str(current),
                "previous_before": str(previous),
                "candidate_release": str(candidate),
                "shared_venv": str(shared_venv),
                "peer": str(peer),
                "snapshot": str(candidate / ".rollback/shared-venv-before-install"),
                "transition": "none",
                "shared_before": identity(shared_venv),
                "peer_before": None,
                "current_before_identity": identity(current),
                "previous_before_identity": identity(previous),
                "candidate_identity": identity(candidate),
                "remove_env_on_recovery": False,
                "service_state_before": {
                    "deadlock-api": "active",
                    "deadlock-worker": "inactive",
                    "deadlock-web": "active",
                },
                "quiesced_services": [
                    "deadlock-api",
                    "deadlock-worker",
                    "deadlock-web",
                ],
                "timer_active_before": False,
            }
            state = shared / ".release-operation.json"
            state.write_text("legacy receipt\n", encoding="ascii")
            state.chmod(0o600)
            return app, current, previous, candidate, receipt

        app, current, previous, candidate, receipt = make_case()
        state = app / "shared/.release-operation.json"
        calls: list[list[str]] = []

        def cleanup_run(command: list[str], **_kwargs: object) -> None:
            calls.append(command)
            if "--retain-receipt" in command:
                candidate.rmdir()
            else:
                state.unlink()

        with (
            mock.patch.object(recovery.os, "geteuid", return_value=0),
            mock.patch.object(recovery, "_validate_generation_tree"),
            mock.patch.object(recovery, "_receipt_json", return_value=receipt),
            mock.patch.object(recovery.subprocess, "run", side_effect=cleanup_run),
        ):
            recovery.abort_retained_only(
                app_dir=app,
                generation=app / ("a" * 64),
            )

        self.assertFalse(state.exists())
        self.assertFalse(candidate.exists())
        self.assertEqual((app / "current").resolve(), current)
        self.assertEqual((app / "previous").resolve(), previous)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("complete-recovery" in command for command in calls))
        self.assertTrue(all("systemctl" not in command for command in calls))
        self.assertTrue(all("platform_release_restore_runtime.sh" not in command for command in calls))

        for label in ("systemd", "candidate-active", "peer", "malformed"):
            with self.subTest(label=label):
                app, current, _previous, candidate, receipt = make_case()
                state = app / "shared/.release-operation.json"
                systemd_state = app / "shared/.release-systemd-state.json"
                if label == "systemd":
                    systemd_state.write_text("{}\n", encoding="ascii")
                    systemd_state.chmod(0o600)
                elif label == "candidate-active":
                    (app / "current").unlink()
                    (app / "current").symlink_to(candidate)
                elif label == "peer":
                    Path(str(receipt["peer"])).mkdir()
                elif label == "malformed":
                    receipt["phase"] = "prepared"
                with (
                    mock.patch.object(recovery.os, "geteuid", return_value=0),
                    mock.patch.object(recovery, "_validate_generation_tree"),
                    mock.patch.object(recovery, "_receipt_json", return_value=receipt),
                    mock.patch.object(recovery.subprocess, "run") as run,
                ):
                    with self.assertRaises(recovery.RecoveryBootstrapError):
                        recovery.abort_retained_only(
                            app_dir=app,
                            generation=app / ("b" * 64),
                        )
                self.assertTrue(state.exists())
                self.assertTrue(candidate.exists())
                run.assert_not_called()

    def test_recovery_bootstrap_route_is_non_deployable_and_mixed_runtime_is_deployable(self) -> None:
        sys.path.insert(0, str(TOOLS))
        from tools import platform_ci_classifier as classifier

        paths = sorted(RECOVERY_BOOTSTRAP_PATCH_FILES)
        for event, branch in (
            ("pull_request", "feature/recovery-bootstrap"),
            ("push", "dev"),
        ):
            with self.subTest(event=event):
                manifest = classifier.classify(
                    paths,
                    event=event,
                    target_sha="a" * 40,
                    branch=branch,
                )
                self.assertEqual(set(manifest["files"]), RECOVERY_BOOTSTRAP_PATCH_FILES)
                self.assertEqual(manifest["class"], "full")
                self.assertFalse(manifest["deployable"])
                self.assertFalse(manifest["fallback"])
                classifier.validate_manifest(manifest, expected_target_sha="a" * 40)

        mixed = classifier.classify(
            paths + ["platform/apps/platform_api/app/main.py"],
            event="push",
            target_sha="a" * 40,
            branch="dev",
        )
        self.assertEqual(mixed["class"], "full")
        self.assertTrue(mixed["deployable"])
        self.assertFalse(mixed["fallback"])
        unknown = classifier.classify(
            paths + ["unknown-root-config.toml"],
            event="push",
            target_sha="a" * 40,
            branch="dev",
        )
        self.assertEqual(unknown["class"], "full")
        self.assertFalse(unknown["deployable"])
        self.assertTrue(unknown["fallback"])

        classifier_source = (TOOLS / "platform_ci_classifier.py").read_text(encoding="utf-8")
        artifact_source = (
            TOOLS / "platform_production_classifier_artifact.py"
        ).read_text(encoding="utf-8")
        auto_deploy_source = (
            REPO_ROOT / ".github/workflows/platform-production-autodeploy.yml"
        ).read_text(encoding="utf-8")
        canonical = frozenset(classifier.RECOVERY_BOOTSTRAP_FILES)
        self.assertEqual(canonical, self._set_assignment(classifier_source, "RECOVERY_BOOTSTRAP_FILES"))
        self.assertEqual(canonical, self._set_assignment(artifact_source, "RECOVERY_BOOTSTRAP_FILES"))
        self.assertEqual(canonical, self._workflow_recovery_set(auto_deploy_source))


if __name__ == "__main__":
    unittest.main()
