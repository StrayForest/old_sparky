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
# The digest assertion below makes this a static merge-base contract: a
# missing owner/test path cannot be hidden by changing the fixture's count or
# by consulting the mutable checkout's git state at test time.
RECOVERY_BOOTSTRAP_PATCH_FILES = frozenset(
    {
        ".github/workflows/platform-production-autodeploy.yml",
        ".github/workflows/platform-production-deploy.yml",
        ".github/workflows/platform-production-recovery-bootstrap-abort.yml",
        ".github/workflows/platform-production-recovery-bootstrap-build.yml",
        ".github/workflows/platform-production-release-abort.yml",
        ".github/workflows/platform-production-release-recover.yml",
        "platform/docs/README.md",
        "platform/docs/adr/recovery-bootstrap-retained-abort.md",
        "platform/docs/deployment-runbook.md",
        "platform/docs/release-state-machine.md",
        "platform/docs/test-suite-governance.md",
        "platform/tests/test_platform_live_qa_runtime_install.py",
        "platform/tests/test_platform_live_qa_wrappers.py",
        "platform/tests/test_platform_recovery_bootstrap.py",
        "platform/tests/test_platform_release_audit_hardening.py",
        "platform/tests/test_platform_release_build_contract.py",
        "platform/tests/test_platform_release_build_diagnostics.py",
        "platform/tests/test_platform_release_recovery_boundaries.py",
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
        "platform/tools/platform_production_deploy_supervisor.sh",
        "platform/tools/platform_release_rollback.sh",
        "platform/tools/platform_release_deploy.sh",
        "platform/tools/platform_release_preflight.sh",
        "platform/tools/platform_release_systemd_state.py",
        "platform/tools/platform_recover_pending.sh",
        "platform/tools/platform_release_transaction.py",
        "platform/tools/platform_run_alembic.sh",
        "platform/tools/platform_test_catalog.py",
        "platform/tools/platform_workflow_input_guard.py",
        "platform/contracts/host_tools_pin.json",
    }
)
RECOVERY_BOOTSTRAP_PATCH_FILE_COUNT = 40
RECOVERY_BOOTSTRAP_PATCH_FILE_DIGEST = (
    "662c3320b3b725894dffbf793fff84f1d934ac260408dcb2c777aeda3ab0af30"
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

    def test_archive_replacement_between_path_check_and_open_fails_closed(self) -> None:
        self.build()
        replacement = self.root / "replacement.zip"
        shutil.copy2(self.bundle, replacement)
        original_open = recovery.os.open

        def replace_before_open(path, flags, *arguments):
            if Path(path) == self.bundle:
                self.bundle.unlink()
                self.bundle.symlink_to(replacement)
            return original_open(path, flags, *arguments)

        with mock.patch.object(recovery.os, "open", side_effect=replace_before_open):
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.verify_bundle(self.bundle)

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
            for name in ("platform_release_systemd_state.py", "platform_release_transaction.py"):
                self.assertEqual(stat.S_IMODE((installed / name).stat().st_mode), 0o444)
            self.assertEqual(
                recovery.main(
                    [
                        "validate-generation",
                        "--generation",
                        str(installed),
                        "--bundle-sha",
                        result["bundle_sha256"],
                    ]
                ),
                0,
            )
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
            'evidence_name="platform-recovery-bootstrap-evidence-${SOURCE_SHA}-${SECURITY_RUN_ID}-${SECURITY_RUN_ATTEMPT}-${RECOVERY_BUILD_RUN_ID}-${RECOVERY_BUILD_RUN_ATTEMPT}.json"',
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
        self.assertIn("--source-ref refs/heads/dev", workflow)
        self.assertIn("--source-digest \"$SOURCE_SHA\"", workflow)
        self.assertIn("recovery_job_id", workflow)
        self.assertIn("RECOVERY_BUILD_JOB_ID", workflow)
        self.assertIn("attempt jobs API response", workflow)
        self.assertIn("bundle_stat_before", workflow)
        self.assertIn("bundle_stat_after", workflow)
        self.assertIn("bundle_digest_before", workflow)
        self.assertIn("bundle_digest_after", workflow)
        for extension in (
            'extensions.get("issuer")',
            'extensions.get("sourceRepositoryURI")',
            'extensions.get("sourceRepositoryRef")',
            'extensions.get("sourceRepositoryDigest")',
            'extensions.get("buildConfigURI")',
            'extensions.get("buildSignerURI")',
            'extensions.get("runInvocationURI")',
        ):
            self.assertIn(extension, workflow)
        self.assertNotIn('extensions.get("Issuer")', workflow)
        self.assertNotIn('extensions.get("SourceRepositoryURI")', workflow)
        self.assertNotIn('statement.get("predicateType")', workflow)
        signer_identity = (
            "StrayForest/old_sparky/"
            ".github/workflows/platform-production-recovery-bootstrap-build.yml"
        )
        signer_lines = [
            line.strip().removesuffix("\\").rstrip()
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
        legacy_abort = (REPO_ROOT / ".github/workflows/platform-production-release-abort.yml").read_text(encoding="utf-8")
        self.assertIn('0:0:555:2', legacy_abort)
        self.assertNotIn('0:0:755:2', legacy_abort)
        self.assertIn('0:0:444:1', legacy_abort)
        release_recover = (REPO_ROOT / ".github/workflows/platform-production-release-recover.yml").read_text(encoding="utf-8")
        self.assertIn('"platform_release_systemd_state.py:444"', release_recover)
        self.assertIn('"platform_release_transaction.py:444"', release_recover)
        self.assertIn('"platform_recover_pending.sh:555"', release_recover)
        self.assertIn("validate-generation", release_recover)
        self.assertIn("platform_recover_pending.sh", release_recover)
        self.assertIn("security_run_id", release_recover)
        self.assertIn("security_run_attempt", release_recover)
        self.assertIn("cleanup_remote_upload", release_recover)
        self.assertIn("trap cleanup_remote_upload EXIT", release_recover)
        self.assertIn("timeout --foreground 10s ssh", release_recover)
        self.assertIn('rm -f -- "$stage/bundle.zip" || true', release_recover)
        self.assertNotIn("$runtime/current/tools", release_recover)
        self.assertIn(
            "evidence_name=platform-recovery-bootstrap-evidence-{sha}-{os.environ['SECURITY_RUN_ID']}-{os.environ['SECURITY_RUN_ATTEMPT']}-{run_id}-{attempt}.json",
            workflow,
        )
        self.assertNotIn(
            "evidence_name=platform-recovery-bootstrap-evidence-{sha}-{os.environ['SECURITY_RUN_ID']}-{os.environ['SECURITY_RUN_ATTEMPT']}-{run_id}-{attempt}\\n",
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

    def test_attestation_policy_rejects_source_run_attempt_job_and_digest_drift(self) -> None:
        workflow = (
            REPO_ROOT
            / ".github/workflows/platform-production-recovery-bootstrap-abort.yml"
        ).read_text(encoding="utf-8")
        policy_marker = '/usr/bin/python3 -I -B - "$attestation_json"'
        policy_command = workflow.index(policy_marker)
        policy_heredoc = workflow.index("<<'PY'", policy_command)
        policy_start = workflow.index("\n", policy_heredoc) + 1
        policy_end = workflow.index("\n          PY", policy_start)
        policy = textwrap.dedent(workflow[policy_start:policy_end])
        source_sha = "a" * 40
        bundle_digest = "b" * 64
        run_id = "12345"
        attempt = "2"
        build_uri = (
            "https://github.com/StrayForest/old_sparky/"
            ".github/workflows/platform-production-recovery-bootstrap-build.yml"
            "@refs/heads/dev"
        )
        extensions = {
            "issuer": "https://token.actions.githubusercontent.com",
            "sourceRepositoryURI": "https://github.com/StrayForest/old_sparky",
            "sourceRepositoryRef": "refs/heads/dev",
            "sourceRepositoryDigest": source_sha,
            "buildConfigURI": build_uri,
            "buildSignerURI": build_uri,
            "runInvocationURI": (
                "https://github.com/StrayForest/old_sparky/"
                f"actions/runs/{run_id}/attempts/{attempt}"
            ),
        }
        payload = [
            {
                "verificationResult": {
                    "signature": {"certificate": {"extensions": extensions}},
                    "verifiedTimestamps": [{"timestamp": "2026-09-27T00:00:00Z"}],
                    "statement": {
                        "subject": [{"digest": {"sha256": bundle_digest}}],
                        "predicateType": "https://slsa.dev/provenance/v1",
                    },
                }
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attestation = root / "attestation.json"
            attestation.write_text(json.dumps(payload), encoding="utf-8")

            def run_policy() -> subprocess.CompletedProcess[str]:
                attestation.write_text(json.dumps(payload), encoding="utf-8")
                env = os.environ.copy()
                env["SOURCE_SHA"] = source_sha
                return subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-",
                        str(attestation),
                        bundle_digest,
                        run_id,
                        attempt,
                    ],
                    input=policy,
                    text=True,
                    capture_output=True,
                    env=env,
                    check=False,
                )

            self.assertEqual(run_policy().returncode, 0)
            for field, bad_value in (
                ("issuer", "https://token.actions.githubusercontent.com.invalid"),
                ("sourceRepositoryURI", "https://github.com/other/repo"),
                ("sourceRepositoryRef", "refs/heads/main"),
                ("sourceRepositoryDigest", "c" * 40),
                ("buildConfigURI", build_uri.replace("refs/heads/dev", "refs/heads/main")),
                ("buildSignerURI", "https://github.com/other/repo/.github/workflows/wrong.yml@refs/heads/dev"),
                (
                    "runInvocationURI",
                    "https://github.com/StrayForest/old_sparky/actions/runs/12345/attempts/3",
                ),
            ):
                with self.subTest(field=field):
                    original = extensions[field]
                    extensions[field] = bad_value
                    try:
                        self.assertNotEqual(run_policy().returncode, 0)
                    finally:
                        extensions[field] = original
            original_runner = extensions["runInvocationURI"]
            extensions["runInvocationURI"] = original_runner.replace("12345", "54321")
            try:
                self.assertNotEqual(run_policy().returncode, 0)
            finally:
                extensions["runInvocationURI"] = original_runner
            subject = payload[0]["verificationResult"]["statement"]["subject"][0]["digest"]
            subject["sha256"] = "c" * 64
            try:
                self.assertNotEqual(run_policy().returncode, 0)
            finally:
                subject["sha256"] = bundle_digest

            jobs_marker = '"$metadata/recovery-jobs.json" "$GITHUB_OUTPUT" "$recovery_run_id" "$recovery_run_attempt" "$source_sha" <<\'PY\''
            jobs_heredoc = workflow.index(jobs_marker)
            jobs_start = workflow.index("\n", jobs_heredoc) + 1
            jobs_end = workflow.index("\n          PY", jobs_start)
            jobs_policy = textwrap.dedent(workflow[jobs_start:jobs_end])
            jobs = root / "jobs.json"
            output = root / "output"
            jobs.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "id": 42,
                                "name": "Build retained-release recovery bootstrap evidence",
                                "conclusion": "success",
                                "run_id": 12345,
                                "run_attempt": 2,
                                "status": "completed",
                                "head_sha": SOURCE_SHA,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["RECOVERY_BUILD_JOB"] = "Build retained-release recovery bootstrap evidence"
            valid_jobs = subprocess.run(
                [sys.executable, "-I", "-B", "-", str(jobs), str(output), "12345", "2", SOURCE_SHA],
                input=jobs_policy,
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(valid_jobs.returncode, 0, valid_jobs.stderr)
            jobs.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "id": 42,
                                "name": "Build retained-release recovery bootstrap evidence",
                                "conclusion": "success",
                                "run_id": 12345,
                                "run_attempt": 2,
                                "status": "completed",
                                "head_sha": SOURCE_SHA,
                            },
                            {
                                "id": 43,
                                "name": "Build retained-release recovery bootstrap evidence",
                                "conclusion": "success",
                                "run_id": 12345,
                                "run_attempt": 2,
                                "status": "completed",
                                "head_sha": SOURCE_SHA,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            duplicate_jobs = subprocess.run(
                [sys.executable, "-I", "-B", "-", str(jobs), str(output), "12345", "2", SOURCE_SHA],
                input=jobs_policy,
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertNotEqual(duplicate_jobs.returncode, 0)
            jobs.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "id": 42,
                                "name": "Build retained-release recovery bootstrap evidence",
                                "conclusion": "failure",
                                "run_id": 12345,
                                "run_attempt": 2,
                                "status": "completed",
                                "head_sha": SOURCE_SHA,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            invalid_jobs = subprocess.run(
                [sys.executable, "-I", "-B", "-", str(jobs), str(output), "12345", "2", SOURCE_SHA],
                input=jobs_policy,
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertNotEqual(invalid_jobs.returncode, 0)
            for field, bad_value in (
                ("id", None),
                ("run_id", 54321),
                ("run_attempt", 3),
            ):
                with self.subTest(recovery_job_field=field):
                    valid_row = {
                        "id": 42,
                        "name": "Build retained-release recovery bootstrap evidence",
                        "conclusion": "success",
                        "run_id": 12345,
                        "run_attempt": 2,
                        "status": "completed",
                        "head_sha": SOURCE_SHA,
                    }
                    valid_row[field] = bad_value
                    jobs.write_text(json.dumps({"jobs": [valid_row]}), encoding="utf-8")
                    malformed_identity = subprocess.run(
                        [sys.executable, "-I", "-B", "-", str(jobs), str(output), "12345", "2", SOURCE_SHA],
                        input=jobs_policy,
                        text=True,
                        capture_output=True,
                        env=env,
                        check=False,
                    )
                    self.assertNotEqual(malformed_identity.returncode, 0)

            evidence_marker = '"$artifact_dir" "$GITHUB_OUTPUT" "$EVIDENCE_NAME" "$BUNDLE_NAME" <<\'PY\''
            evidence_heredoc = workflow.index(evidence_marker)
            evidence_start = workflow.index("\n", evidence_heredoc) + 1
            evidence_end = workflow.index("\n          PY", evidence_start)
            evidence_policy = textwrap.dedent(workflow[evidence_start:evidence_end])
            bundle_name = "platform-recovery-bootstrap-test.zip"
            evidence_name = "platform-recovery-bootstrap-evidence-test.json"
            bundle_bytes = b"immutable bundle fixture\n"
            expected_bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
            evidence_payload = {
                "schema": 1,
                "capability": "recovery_bootstrap",
                "capabilities": ["abort_retained_only", "recover_pending"],
                "deployable": False,
                "bundle_name": bundle_name,
                "bundle_sha256": expected_bundle_sha,
                "recovery_run_id": "12345",
                "recovery_run_attempt": "2",
                "recovery_job_id": "42",
                "provenance": {
                    "repository": "StrayForest/old_sparky",
                    "workflow": "Platform security and build",
                    "job": "Verification contract",
                    "run_id": "12345",
                    "run_attempt": "2",
                    "source_sha": SOURCE_SHA,
                    "artifact_name": "platform-ci-route-12345-2",
                    "artifact_sha256": "b" * 64,
                    "deployable": False,
                },
            }

            def write_artifacts(case_root: Path, payload: dict[str, object]) -> None:
                def archive(path: Path, name: str, data: bytes) -> None:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    with zipfile.ZipFile(path, "w") as archive_file:
                        archive_file.writestr(info, data)

                archive(case_root / "evidence.zip", evidence_name, json.dumps(payload).encode("ascii"))
                archive(case_root / "bundle.zip", bundle_name, bundle_bytes)

            def run_evidence(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
                case_root = root / f"evidence-case-{len(list(root.glob('evidence-case-*')))}"
                case_root.mkdir()
                write_artifacts(case_root, payload)
                case_output = case_root / "output"
                policy_env = os.environ.copy()
                policy_env.update(
                    {
                        "RECOVERY_RUN_ID": "12345",
                        "RECOVERY_RUN_ATTEMPT": "2",
                        "RECOVERY_BUILD_JOB_ID": "42",
                        "SECURITY_RUN_ID": "12345",
                        "SECURITY_RUN_ATTEMPT": "2",
                        "SOURCE_SHA": SOURCE_SHA,
                        "ROUTE_DIGEST": "b" * 64,
                    }
                )
                return subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-",
                        str(case_root),
                        str(case_output),
                        evidence_name,
                        bundle_name,
                    ],
                    input=evidence_policy,
                    text=True,
                    capture_output=True,
                    env=policy_env,
                    check=False,
                )

            self.assertEqual(run_evidence(evidence_payload).returncode, 0)
            evidence_payload["recovery_job_id"] = "43"
            mismatched_evidence = run_evidence(evidence_payload)
            self.assertNotEqual(mismatched_evidence.returncode, 0)

    def test_provenance_workflows_have_closed_exact_job_identity_predicates(self) -> None:
        workflow_paths = (
            REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-build.yml",
            REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-abort.yml",
            REPO_ROOT / ".github/workflows/platform-production-release-recover.yml",
        )
        for path in workflow_paths:
            text_value = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertIn('type(row.get("id")) is int', text_value)
                self.assertIn('row.get("run_id") ==', text_value)
                self.assertIn('row.get("run_attempt") ==', text_value)

    def test_recovery_bootstrap_route_is_non_deployable_and_mixed_runtime_is_deployable(self) -> None:
        sys.path.insert(0, str(TOOLS))
        from tools import platform_ci_classifier as classifier

        patch_file_digest = hashlib.sha256(
            "\n".join(sorted(RECOVERY_BOOTSTRAP_PATCH_FILES)).encode()
        ).hexdigest()
        self.assertEqual(
            len(RECOVERY_BOOTSTRAP_PATCH_FILES),
            RECOVERY_BOOTSTRAP_PATCH_FILE_COUNT,
        )
        self.assertEqual(patch_file_digest, RECOVERY_BOOTSTRAP_PATCH_FILE_DIGEST)
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
