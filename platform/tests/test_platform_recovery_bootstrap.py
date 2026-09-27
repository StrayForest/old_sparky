from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
import zipfile

from tools import platform_recovery_bootstrap as recovery


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLATFORM_ROOT.parent
TOOLS = PLATFORM_ROOT / "tools"
SOURCE_SHA = "a" * 40


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
            self.assertFalse(any(path.name.startswith(".") for path in (app / "shared" / ".release-recovery" / "generations").iterdir()))


class RecoveryBootstrapContractTests(unittest.TestCase):
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
        self.assertIn("actions: read", workflow)
        self.assertNotIn("PROD_SSH_KEY", workflow)
        self.assertNotIn("secrets.", workflow)

    def test_manual_abort_validates_artifacts_before_secrets_and_uses_bundle_only(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-abort.yml").read_text(encoding="utf-8")
        evidence = workflow.index("Validate exact successful security run")
        secrets = workflow.index("secrets.PROD_SSH_HOST")
        self.assertLess(evidence, secrets)
        self.assertIn("gh attestation verify", workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("attestations: read", workflow)
        self.assertIn("ABORT-RECOVERY-BOOTSTRAP-RETAINED-ONLY", workflow)
        self.assertIn("platform_recovery_bootstrap.py\" install", workflow)
        self.assertIn("platform_abort_retained_only.sh", workflow)
        self.assertIn("platform_release_lock.sh\" --run", workflow)
        self.assertNotIn("git checkout", workflow)
        self.assertNotIn("platform_release_deploy", workflow)
        self.assertNotIn("downgrade", workflow)

    def test_recovery_bootstrap_route_is_non_deployable_and_mixed_runtime_is_deployable(self) -> None:
        sys.path.insert(0, str(TOOLS))
        from tools import platform_ci_classifier as classifier

        paths = ["platform/tools/platform_recovery_bootstrap.py"]
        manifest = classifier.classify(paths, event="push", target_sha="a" * 40, branch="dev")
        self.assertFalse(manifest["deployable"])
        self.assertFalse(manifest["fallback"])
        mixed = classifier.classify(paths + ["platform/apps/platform_api/app/main.py"], event="push", target_sha="a" * 40, branch="dev")
        self.assertTrue(mixed["deployable"])


if __name__ == "__main__":
    unittest.main()
