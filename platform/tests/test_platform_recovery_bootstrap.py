from __future__ import annotations

import ast
from contextlib import ExitStack
import hashlib
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tools import platform_recovery_bootstrap as recovery


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLATFORM_ROOT.parent
TOOLS = PLATFORM_ROOT / "tools"
SOURCE_SHA = "a" * 40

# This is the complete changed-file set of the recovery-bootstrap
# patch at the reviewed merge base. Keep the real set here so the route test
# exercises the exact pull-request and trusted-dev-push inputs, including the
# host-key scan contract that is easy to omit from one of the independent
# consumers. The patch also touched the candidate-owned live-QA installer and
# the app-owned external-load dispatcher; either now promotes a dev push to a
# deployable app route.
# The digest assertion below makes this a static merge-base contract: a
# missing owner/test path cannot be hidden by changing the fixture's count or
# by consulting the mutable checkout's git state at test time.
RECOVERY_BOOTSTRAP_PATCH_FILES = frozenset(
    {
        ".github/workflows/platform-production-autodeploy.yml",
        ".github/workflows/platform-production-deploy.yml",
        ".github/workflows/platform-production-recovery-bootstrap-abort.yml",
        ".github/workflows/platform-production-recovery-bootstrap-build.yml",
        ".github/workflows/platform-production-recovery-bootstrap-publish.yml",
        ".github/workflows/platform-production-release-abort.yml",
        ".github/workflows/platform-production-release-recover.yml",
        "platform/contracts/host_tools_pin.json",
        "platform/alembic/env.py",
        "platform/deploy/systemd/deadlock-cloudflare-ips.service",
        "platform/deploy/systemd/deadlock-health-monitor.service",
        "platform/docs/README.md",
        "platform/docs/adr/production-host-tools-provisioning.md",
        "platform/docs/adr/recovery-bootstrap-retained-abort.md",
        "platform/docs/deployment-runbook.md",
        "platform/docs/release-state-machine.md",
        "platform/docs/test-suite-governance.md",
        "platform/tests/test_platform_live_qa_runtime_install.py",
        "platform/tests/test_platform_live_qa_wrappers.py",
        "platform/tests/test_platform_ci_classifier.py",
        "platform/tests/test_platform_host_tools_bundle.py",
        "platform/tests/test_platform_db.py",
        "platform/tests/test_platform_recovery_bootstrap.py",
        "platform/tests/test_platform_recovery_workflow_caller.py",
        "platform/tests/test_platform_release_audit_hardening.py",
        "platform/tests/test_platform_release_build_contract.py",
        "platform/tests/test_platform_release_build_diagnostics.py",
        "platform/tests/test_platform_release_recovery_boundaries.py",
        "platform/tests/test_platform_storage_maintenance.py",
        "platform/tests/test_platform_release_systemd_state.py",
        "platform/tests/test_platform_release_venv_rollback.py",
        "platform/tests/test_platform_ssh_host_key_scan.py",
        "platform/tests/test_platform_cloudflare_ips.py",
        "platform/tests/test_platform_install_nginx.py",
        "platform/tools/platform_abort_retained_only.sh",
        "platform/tools/platform_build_live_qa_runtime.py",
        "platform/tools/platform_install_nginx.py",
        "platform/tools/platform_install_logging.sh",
        "platform/tools/platform_install_systemd_units.sh",
        "platform/tools/platform_ci_classifier.py",
        "platform/tools/platform_live_qa_guard.py",
        "platform/tools/platform_live_qa_runtime_install.py",
        "platform/tools/platform_production_classifier_artifact.py",
        "platform/tools/platform_recovery_bootstrap.py",
        "platform/tools/platform_tournament_list_read_model_recovery.py",
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
        "platform/tools/platform_verify_contract.py",
        "platform/tools/platform_workflow_input_guard.py",
        "platform/tools/platform_workflow_remote_dispatch.py",
        "platform/tools/platform_update_cloudflare_ips.py",
        "platform/tools/platform_deploy_smoke_impl.py",
        "platform/tools/platform_health_monitor.py",
        "platform/tools/platform_validate_edge_policy.py",
        "platform/python_packages/platform_infra/db.py",
    }
)
RECOVERY_BOOTSTRAP_PATCH_FILE_COUNT = 63
RECOVERY_BOOTSTRAP_PATCH_FILE_DIGEST = (
    "d542d27ef613aae1c67ae9778bae97f4f68340d05a3e87fe9eb2c7704cd65bfb"
)

# These paths are deliberately present in the recovery route allowlist but are
# outside this historical patch fixture. The remaining allowlisted paths plus
# the documentation files below derive the complete committed patch fixture
# without consulting the mutable checkout's git history.
RECOVERY_BOOTSTRAP_ALLOWLIST_ONLY_FILES = frozenset(
    {
        "platform/tests/test_platform_live_qa_guard.py",
        "platform/tools/platform_release_lock.sh",
        "platform/tests/test_platform_workflow_provenance.py",
        "platform/tools/platform_deploy_baseline.py",
        "platform/tools/platform_baseline_runtime_proof.py",
        "platform/tools/platform_host_tools_bundle.py",
        "platform/tools/platform_validate_release_artifact.py",
        "platform/tools/platform_workflow_provenance.py",
    }
)
RECOVERY_BOOTSTRAP_PATCH_DOCS = frozenset(
    {
        "platform/docs/README.md",
        "platform/docs/adr/production-host-tools-provisioning.md",
        "platform/docs/adr/recovery-bootstrap-retained-abort.md",
        "platform/docs/deployment-runbook.md",
        "platform/docs/release-state-machine.md",
        "platform/docs/test-suite-governance.md",
    }
)

# This is the exact topology/recovery patch delta reviewed independently from
# the complete merge-base fixture above. Keep it separate: the pin-only subset
# remains a no-op, while runtime supervisor/dispatcher changes deploy.
RECOVERY_BOOTSTRAP_CURRENT_DELTA_FILES = frozenset(
    {
        ".github/workflows/platform-production-recovery-bootstrap-abort.yml",
        ".github/workflows/platform-production-deploy.yml",
        ".github/workflows/platform-production-recovery-bootstrap-build.yml",
        ".github/workflows/platform-production-recovery-bootstrap-publish.yml",
        ".github/workflows/platform-production-release-recover.yml",
        ".github/workflows/platform-production-autodeploy.yml",
        "platform/contracts/host_tools_pin.json",
        "platform/alembic/env.py",
        "platform/deploy/systemd/deadlock-cloudflare-ips.service",
        "platform/deploy/systemd/deadlock-health-monitor.service",
        "platform/docs/adr/production-host-tools-provisioning.md",
        "platform/docs/adr/recovery-bootstrap-retained-abort.md",
        "platform/docs/deployment-runbook.md",
        "platform/tests/test_platform_cloudflare_ips.py",
        "platform/tests/test_platform_install_nginx.py",
        "platform/tests/test_platform_live_qa_wrappers.py",
        "platform/tests/test_platform_recovery_bootstrap.py",
        "platform/tests/test_platform_db.py",
        "platform/tests/test_platform_release_audit_hardening.py",
        "platform/tests/test_platform_release_recovery_boundaries.py",
        "platform/tests/test_platform_storage_maintenance.py",
        "platform/tools/platform_ci_classifier.py",
        "platform/tools/platform_install_nginx.py",
        "platform/tools/platform_install_logging.sh",
        "platform/tools/platform_install_systemd_units.sh",
        "platform/tools/platform_production_classifier_artifact.py",
        "platform/tools/platform_production_deploy_supervisor.sh",
        "platform/tools/platform_recover_pending.sh",
        "platform/tools/platform_recovery_bootstrap.py",
        "platform/tools/platform_tournament_list_read_model_recovery.py",
        "platform/tools/platform_release_restore_runtime.sh",
        "platform/tools/platform_release_transaction.py",
        "platform/tools/platform_run_alembic.sh",
        "platform/tools/platform_release_preflight.sh",
        "platform/tools/platform_test_catalog.py",
        "platform/tools/platform_update_cloudflare_ips.py",
        "platform/tools/platform_deploy_smoke_impl.py",
        "platform/tools/platform_health_monitor.py",
        "platform/tools/platform_validate_edge_policy.py",
        "platform/python_packages/platform_infra/db.py",
        "platform/tools/platform_workflow_remote_dispatch.py",
    }
)
RECOVERY_BOOTSTRAP_CURRENT_DELTA_FILE_COUNT = 41
RECOVERY_BOOTSTRAP_CURRENT_DELTA_FILE_DIGEST = (
    "b652927afc218ffa583265a74facec3ca531c453b20fb9519bee8029dc4695bd"
)


def provenance() -> dict[str, object]:
    return {
        "repository": "StrayForest/old_sparky",
        "workflow": "Platform security and build",
        "job": "Verification contract",
        "run_id": "12345",
        "run_attempt": "2",
        "recovery_workflow_sha": "c" * 40,
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

    def test_recovery_child_timeout_terminates_its_process_group(self) -> None:
        child = mock.Mock(pid=4321)
        child.wait.side_effect = [subprocess.TimeoutExpired(["helper"], 1), None]
        with (
            mock.patch.object(recovery.subprocess, "Popen", return_value=child) as popen,
            mock.patch.object(recovery.os, "killpg") as killpg,
        ):
            with self.assertRaises(recovery.RecoveryChildError) as raised:
                recovery._run_recovery_child(["helper"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(4321, recovery.signal.SIGTERM)
        self.assertEqual(raised.exception.stage, "recovery_child")
        self.assertEqual(raised.exception.outcome, "timeout")
        self.assertIsNone(raised.exception.child_exit)

    def test_recovery_child_diagnostic_is_closed_and_redacts_child_details(self) -> None:
        command = [
            "/usr/bin/python3",
            "-I",
            "/private/argv-path/platform_release_transaction.py",
            "restore-legacy-services",
            "--state",
            "/private/receipt-path/.release-operation.json",
        ]
        cases: tuple[tuple[str, int | None], ...] = (
            ("spawn_failed", None),
            ("timeout", None),
            ("child_exit", 17),
        )
        for outcome, child_exit in cases:
            with self.subTest(outcome=outcome):
                child = mock.Mock(pid=7890)
                if outcome == "spawn_failed":
                    popen_patch = mock.patch.object(
                        recovery.subprocess,
                        "Popen",
                        side_effect=OSError("PRIVATE_EXCEPTION /private/error-path"),
                    )
                elif outcome == "timeout":
                    child.wait.side_effect = [
                        subprocess.TimeoutExpired(command, 120),
                        None,
                    ]
                    popen_patch = mock.patch.object(
                        recovery.subprocess,
                        "Popen",
                        return_value=child,
                    )
                else:
                    child.wait.return_value = child_exit
                    popen_patch = mock.patch.object(
                        recovery.subprocess,
                        "Popen",
                        return_value=child,
                    )

                with popen_patch, mock.patch.object(recovery.os, "killpg"):
                    with self.assertRaises(recovery.RecoveryChildError) as raised:
                        recovery._run_recovery_child(
                            command,
                            stage="legacy_restore_services",
                        )

                error = raised.exception
                self.assertEqual(error.stage, "legacy_restore_services")
                self.assertEqual(error.outcome, outcome)
                self.assertEqual(error.child_exit, child_exit)
                self.assertEqual(
                    error.args,
                    ("retained recovery child did not complete",),
                )
                self.assertEqual(
                    set(error.__dict__),
                    {"stage", "outcome", "child_exit"},
                )
                self.assertNotIn("/private/", str(error))
                self.assertNotIn("PRIVATE_EXCEPTION", str(error))

                stderr = io.StringIO()
                with (
                    mock.patch.object(
                        recovery,
                        "abort_retained_only",
                        side_effect=error,
                    ),
                    mock.patch("sys.stderr", stderr),
                ):
                    status = recovery.main(
                        [
                            "abort_retained_only",
                            "--app-dir",
                            "/private/app-path",
                        ]
                    )
                self.assertEqual(status, 2)
                child_status = "none" if child_exit is None else str(child_exit)
                self.assertEqual(
                    stderr.getvalue(),
                    "RECOVERY_BOOTSTRAP_DIAGNOSTIC schema=1 "
                    f"stage=legacy_restore_services outcome={outcome} "
                    f"child_exit={child_status}\n"
                    "RECOVERY_BOOTSTRAP schema=1 status=failed "
                    "capability=abort_retained_only deployable=false\n",
                )
                self.assertNotIn("/private/", stderr.getvalue())
                self.assertNotIn("PRIVATE_EXCEPTION", stderr.getvalue())


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


class RecoveryLegacyReadinessTests(unittest.TestCase):
    @staticmethod
    def _record() -> dict[str, object]:
        return {
            "phase": "recovery-restored",
            "operation": "install",
            "candidate_path": Path("/private/nonexistent-candidate"),
            "service_state_before": {
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
            },
            "service_enabled_before": {
                "deadlock-api": "enabled",
                "deadlock-worker": "disabled",
                "deadlock-web": "enabled",
            },
            "timer_active_before": True,
            "timer_enabled_before": "enabled",
        }

    def _mark_patches(
        self,
        transaction: object,
        record: dict[str, object],
        *,
        time_module: object,
    ) -> tuple[tuple[object, ...], mock.Mock]:
        service_state = record["service_state_before"]
        service_enabled = record["service_enabled_before"]
        enabled = dict(service_enabled)
        enabled["deadlock-cloudflare-ips.timer"] = "enabled"
        states = dict(service_state)
        states["deadlock-cloudflare-ips.timer"] = "active"
        write_record = mock.Mock()
        return (
            (
                mock.patch.object(
                    transaction, "_systemctl_path", side_effect=lambda value: value
                ),
                mock.patch.object(transaction, "_load_record", return_value=record),
                mock.patch.object(transaction, "_validate_legacy_liveqa_recovery"),
                mock.patch.object(
                    transaction,
                    "_read_systemctl_enabled",
                    side_effect=lambda _path, unit: enabled[unit],
                ),
                mock.patch.object(
                    transaction,
                    "_read_systemctl_state",
                    side_effect=lambda _path, unit: states[unit],
                ),
                mock.patch.object(
                    transaction, "_record_for_write", side_effect=lambda value: value
                ),
                mock.patch.object(transaction, "_write_record", new=write_record),
                mock.patch.object(transaction, "time", time_module),
            ),
            write_record,
        )

    def test_transient_readiness_failures_retry_api_then_web(self) -> None:
        from tools import platform_release_transaction as transaction

        record = self._record()
        calls: list[tuple[list[str], float]] = []
        returns = iter((22, 0, 22, 0))

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs["timeout"]))
            return subprocess.CompletedProcess(command, next(returns))

        clock = mock.Mock(monotonic=mock.Mock(return_value=0.0), sleep=mock.Mock())
        patches, write_record = self._mark_patches(transaction, record, time_module=clock)
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with mock.patch.object(transaction.subprocess, "run", side_effect=run):
                transaction.mark_legacy_services_restored(
                    Path("/private/state"), systemctl="/usr/bin/systemctl"
                )

        self.assertEqual(record["phase"], "legacy-services-restored")
        write_record.assert_called_once()
        self.assertEqual(
            [command[-1] for command, _timeout in calls],
            [
                "http://127.0.0.1:8010/api/v1/health/ready",
                "http://127.0.0.1:8010/api/v1/health/ready",
                "http://127.0.0.1:3000/",
                "http://127.0.0.1:3000/",
            ],
        )
        for command, timeout in calls:
            options = [command[index : index + 2] for index in range(len(command) - 1)]
            self.assertIn(["--max-time", "2"], options)
            self.assertLessEqual(timeout, 3.0)
        self.assertEqual(clock.sleep.call_args_list, [mock.call(1.0), mock.call(1.0)])

    def test_permanent_timeouts_share_one_bounded_budget_without_marking_phase(self) -> None:
        from tools import platform_release_transaction as transaction

        record = self._record()
        now = {"value": 0.0}
        timeouts: list[float] = []

        def monotonic() -> float:
            return now["value"]

        def sleep(seconds: float) -> None:
            now["value"] += seconds

        def run(command: list[str], **kwargs: object) -> None:
            timeout = kwargs["timeout"]
            timeouts.append(timeout)
            now["value"] += timeout
            raise subprocess.TimeoutExpired(command, timeout)

        clock = mock.Mock(monotonic=monotonic, sleep=sleep)
        patches, write_record = self._mark_patches(transaction, record, time_module=clock)
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with mock.patch.object(transaction.subprocess, "run", side_effect=run):
                with self.assertRaises(transaction.TransactionError):
                    transaction.mark_legacy_services_restored(
                        Path("/private/state"), systemctl="/usr/bin/systemctl"
                    )

        self.assertEqual(record["phase"], "recovery-restored")
        write_record.assert_not_called()
        self.assertEqual(now["value"], transaction.LEGACY_READINESS_TIMEOUT_SECONDS)
        self.assertEqual(timeouts, [3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 2.0])

    def test_readiness_success_at_deadline_is_rejected_without_marking_phase(self) -> None:
        from tools import platform_release_transaction as transaction

        record = self._record()
        record["service_state_before"] = {
            "deadlock-api": "active",
            "deadlock-worker": "inactive",
            "deadlock-web": "inactive",
        }
        now = iter((0.0, 0.0, transaction.LEGACY_READINESS_TIMEOUT_SECONDS))
        clock = mock.Mock(monotonic=mock.Mock(side_effect=lambda: next(now)), sleep=mock.Mock())
        patches, write_record = self._mark_patches(
            transaction,
            record,
            time_module=clock,
        )
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with mock.patch.object(
                transaction.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["curl"], 0),
            ):
                with self.assertRaises(transaction.TransactionError):
                    transaction.mark_legacy_services_restored(
                        Path("/private/state"), systemctl="/usr/bin/systemctl"
                    )

        self.assertEqual(record["phase"], "recovery-restored")
        write_record.assert_not_called()
        clock.sleep.assert_not_called()

    def test_inactive_snapshot_skips_readiness_endpoints(self) -> None:
        from tools import platform_release_transaction as transaction

        clock = mock.Mock(monotonic=mock.Mock(return_value=0.0), sleep=mock.Mock())
        with (
            mock.patch.object(transaction, "time", clock),
            mock.patch.object(transaction.subprocess, "run") as run,
        ):
            transaction._verify_legacy_readiness(
                {
                    "service_state_before": {
                        "deadlock-api": "inactive",
                        "deadlock-worker": "inactive",
                        "deadlock-web": "inactive",
                    }
                }
            )
        run.assert_not_called()
        clock.sleep.assert_not_called()


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

    def test_initial_receipt_stale_systemd_pair_is_retained_then_retryable(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("initial recovery contract requires root")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app"
            releases = app / "releases"
            shared = app / "shared"
            candidate = releases / "initial-candidate"
            releases.mkdir(parents=True)
            shared.mkdir()
            candidate.mkdir()
            for path in (app, releases, shared, candidate):
                path.chmod(0o755)
            state = shared / ".release-operation.json"
            stale_systemd = shared / ".release-systemd-state.json"
            snapshot = {
                unit: {"active": "inactive", "enabled": "disabled"}
                for unit in recovery.INITIAL_SYSTEMD_UNITS
            }
            metadata = candidate.lstat()
            receipt = {
                "version": 2,
                "operation": "install",
                "operation_id": "a" * 32,
                "phase": "staged",
                "app_dir": str(app),
                "current_before": None,
                "previous_before": None,
                "candidate_release": str(candidate),
                "shared_venv": str(shared / "venv"),
                "peer": str(shared / ".venv-install-initial-candidate.none"),
                "snapshot": str(candidate / ".rollback/shared-venv-before-install"),
                "transition": "none",
                "shared_before": None,
                "peer_before": None,
                "current_before_identity": None,
                "previous_before_identity": None,
                "candidate_identity": {"dev": metadata.st_dev, "ino": metadata.st_ino},
                "remove_env_on_recovery": False,
                "service_state_before": {
                    "deadlock-api": "inactive",
                    "deadlock-worker": "inactive",
                    "deadlock-web": "inactive",
                },
                "service_enabled_before": {
                    "deadlock-api": "disabled",
                    "deadlock-worker": "disabled",
                    "deadlock-web": "disabled",
                },
                "quiesced_services": [
                    "deadlock-api",
                    "deadlock-worker",
                    "deadlock-web",
                ],
                "timer_active_before": False,
                "timer_enabled_before": "disabled",
                "systemd_state_before": snapshot,
            }
            state.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="ascii")
            state.chmod(0o600)
            stale_systemd.write_text("{}\n", encoding="ascii")
            stale_systemd.chmod(0o600)
            generation = root / ("b" * 64)

            with (
                mock.patch.object(recovery.os, "geteuid", return_value=0),
                mock.patch.object(recovery, "_validate_generation_tree"),
                mock.patch.object(recovery, "_run_recovery_child") as child,
            ):
                with self.assertRaises(recovery.RecoveryBootstrapError):
                    recovery.abort_retained_only(app_dir=app, generation=generation)
                child.assert_not_called()
                self.assertTrue(state.exists())
                self.assertTrue(candidate.exists())
                self.assertTrue(stale_systemd.exists())

                stale_systemd.unlink()

                def complete_cleanup(command: list[str]) -> None:
                    if "complete-recovery" not in command:
                        return
                    if "--retain-receipt" in command:
                        candidate.rmdir()
                    else:
                        state.unlink()

                child.side_effect = complete_cleanup
                recovery.abort_retained_only(app_dir=app, generation=generation)

            commands = [call.args[0] for call in child.call_args_list]
            restore_index = next(
                index
                for index, command in enumerate(commands)
                if "restore-initial-systemd" in command
            )
            cleanup_index = next(
                index
                for index, command in enumerate(commands)
                if "complete-recovery" in command and "--retain-receipt" in command
            )
            self.assertLess(restore_index, cleanup_index)
            self.assertFalse(state.exists())
            self.assertFalse(candidate.exists())

    def test_build_workflow_is_trusted_default_branch_secret_free_and_non_deployable(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-build.yml").read_text(encoding="utf-8")
        publisher = (REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-publish.yml").read_text(encoding="utf-8")
        self.assertIn("github.event.workflow_run.head_branch == 'dev'", workflow)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", workflow)
        self.assertIn('"deployable": False', workflow)
        self.assertNotIn("needs: build", workflow)
        self.assertNotIn("Upload closed recovery evidence", workflow)
        self.assertIn("workflow_run", publisher)
        self.assertIn("publisher-validate", publisher)
        self.assertIn("Upload closed recovery evidence", publisher)
        self.assertIn("PRODUCER_RUN_ID", publisher)
        self.assertIn("PUBLISHER_WORKFLOW_SHA", publisher)
        self.assertIn('row.get("status") == "in_progress"', publisher)
        self.assertIn('row.get("conclusion") is None', publisher)
        self.assertIn('--artifact-sha256 "$BUNDLE_ARTIFACT_SHA256"', publisher)
        self.assertIn('--provenance "$PROVENANCE_PATH"', publisher)
        self.assertIn('GH_TOKEN: ${{ github.token }}', publisher)
        self.assertIn("recovery producer", (TOOLS / "platform_recovery_bootstrap.py").read_text(encoding="utf-8"))
        self.assertIn("actions: read", workflow)
        self.assertNotIn("PROD_SSH_KEY", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("secrets.", publisher)

    def test_build_workflow_completed_trigger_gate_and_producer_handoff(self) -> None:
        workflow = (
            REPO_ROOT
            / ".github/workflows/platform-production-recovery-bootstrap-build.yml"
        ).read_text(encoding="utf-8")
        gate_start = workflow.index("    if: >-", workflow.index("  build:"))
        gate_end = workflow.index("    runs-on:", gate_start)
        gate = workflow[gate_start:gate_end]
        eligible = {
            "ref": "refs/heads/dev",
            "conclusion": "success",
            "event": "push",
            "head_branch": "dev",
            "repository": "StrayForest/old_sparky",
            "name": "Platform security and build",
            "path": ".github/workflows/platform-security.yml",
        }
        ineligible = dict(eligible, conclusion="failure")

        def accepted(event: dict[str, str]) -> bool:
            return (
                event["ref"] == "refs/heads/dev"
                and event["conclusion"] == "success"
                and event["event"] == "push"
                and event["head_branch"] == "dev"
                and event["repository"] == "StrayForest/old_sparky"
                and event["name"] == "Platform security and build"
                and event["path"] == ".github/workflows/platform-security.yml"
            )

        self.assertTrue(accepted(eligible))
        self.assertFalse(accepted(ineligible))
        for condition in (
            "github.event.workflow_run.conclusion == 'success'",
            "github.event.workflow_run.event == 'push'",
            "github.event.workflow_run.head_branch == 'dev'",
            "github.event.workflow_run.head_repository.full_name == 'StrayForest/old_sparky'",
            "github.event.workflow_run.name == 'Platform security and build'",
            "github.event.workflow_run.path == '.github/workflows/platform-security.yml'",
        ):
            self.assertIn(condition, gate)
        self.assertIn("status\") == \"completed\"", workflow)
        self.assertIn("conclusion\") == \"success\"", workflow)

    def test_manual_abort_validates_artifacts_before_secrets_and_uses_bundle_only(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-abort.yml").read_text(encoding="utf-8")
        evidence = workflow.index("Validate exact successful security run")
        secrets = workflow.index("secrets.PROD_SSH_HOST")
        self.assertLess(evidence, secrets)
        self.assertIn("gh attestation verify", workflow)
        self.assertIn("--source-ref refs/heads/dev", workflow)
        self.assertIn("--source-digest \"$RECOVERY_WORKFLOW_SHA\"", workflow)
        self.assertIn("recovery_job_id", workflow)
        self.assertIn("RECOVERY_BUILD_JOB_ID", workflow)
        self.assertIn("attempt jobs API response", workflow)
        self.assertIn("bundle_stat_before", workflow)
        self.assertIn("bundle_stat_after", workflow)
        self.assertIn("bundle_digest_before", workflow)
        self.assertIn("bundle_digest_after", workflow)
        for certificate_field in (
            'certificate.get("issuer")',
            'certificate.get("sourceRepositoryURI")',
            'certificate.get("sourceRepositoryRef")',
            'certificate.get("sourceRepositoryDigest")',
            'certificate.get("buildConfigURI")',
            'certificate.get("buildSignerURI")',
            'certificate.get("runInvocationURI")',
        ):
            self.assertIn(certificate_field, workflow)
        self.assertNotIn('certificate.get("extensions")', workflow)
        self.assertNotIn('extensions.get("issuer")', workflow)
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
        for provenance_field in (
            "recovery_workflow_sha",
            "publisher_run_id",
            "publisher_run_attempt",
            "publisher_workflow_sha",
            "publisher_job_id",
        ):
            self.assertIn(provenance_field, release_recover)
        self.assertIn("cleanup_remote_upload", release_recover)
        self.assertIn("trap cleanup_remote_upload EXIT", release_recover)
        self.assertIn("timeout --foreground 10s ssh", release_recover)
        self.assertIn('rm -f -- "$stage/bundle.zip" || true', release_recover)
        self.assertNotIn("$runtime/current/tools", release_recover)
        self.assertIn(
            "evidence_name=platform-recovery-bootstrap-evidence-{source_sha}-{os.environ['SECURITY_RUN_ID']}-{os.environ['SECURITY_RUN_ATTEMPT']}-{recovery_run['id']}-{recovery_run['run_attempt']}-{os.environ['PUBLISHER_RUN_ID']}-{os.environ['PUBLISHER_RUN_ATTEMPT']}.json",
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

                    def fake_popen(command: list[str], **_kwargs: object) -> object:
                        class Child:
                            pid = 1234

                            def wait(self, **_wait_kwargs: object) -> int:
                                fake_run(command)
                                return 0

                        return Child()

                    with (
                        mock.patch.object(recovery.os, "geteuid", return_value=0),
                        mock.patch.object(recovery, "_validate_generation_tree"),
                        mock.patch.object(recovery, "_receipt_json", return_value=receipt),
                        mock.patch.object(recovery, "_validate_receipt_identity", return_value=app / "releases" / "current-release"),
                        mock.patch.object(recovery, "_safe_receipt"),
                        mock.patch.object(recovery, "_release_pointer", return_value=app / "releases" / "current-release"),
                        mock.patch.object(recovery.subprocess, "Popen", side_effect=fake_popen),
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

    def test_recovery_attestation_resolves_workflow_sha_in_shell_scope(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-release-recover.yml"
        ).read_text(encoding="utf-8")
        recovery_step = workflow.split(
            "- name: Validate exact security and recovery provenance before SSH", 1
        )[1].split("- name: Validate production SSH inputs", 1)[0]
        run_lines = recovery_step.split("run: |", 1)[1].splitlines()
        assignment = next(
            line.strip()
            for line in run_lines
            if line.strip().startswith("recovery_workflow_sha=")
        )
        digest_guard = next(
            line.strip()
            for line in run_lines
            if line.strip().startswith('[[ "$recovery_workflow_sha" =~')
        )
        attestation = 'gh attestation verify "$bundle_path"'
        self.assertLess(recovery_step.index(assignment), recovery_step.index(attestation))
        self.assertLess(recovery_step.index(digest_guard), recovery_step.index(attestation))
        self.assertIn('--source-digest "$recovery_workflow_sha"', recovery_step)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "GITHUB_OUTPUT"
            for candidate, expected_status in (("a" * 40, 0), ("not-a-sha", 1)):
                output.write_text(f"recovery_workflow_sha={candidate}\n", encoding="ascii")
                command = "\n".join(
                    (
                        "set -euo pipefail",
                        f"GITHUB_OUTPUT={shlex.quote(str(output))}",
                        assignment,
                        digest_guard,
                        'printf \'%s\\n\' "$recovery_workflow_sha"',
                    )
                )
                result = subprocess.run(
                    ["bash", "-c", command],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                self.assertEqual(result.returncode, expected_status)
                if expected_status == 0:
                    self.assertEqual(result.stdout.strip(), candidate)

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

        def cleanup_popen(command: list[str], **_kwargs: object) -> object:
            class Child:
                pid = 1235

                def wait(self, **_wait_kwargs: object) -> int:
                    cleanup_run(command)
                    return 0

            return Child()

        with (
            mock.patch.object(recovery.os, "geteuid", return_value=0),
            mock.patch.object(recovery, "_validate_generation_tree"),
            mock.patch.object(recovery, "_receipt_json", return_value=receipt),
            mock.patch.object(recovery.subprocess, "Popen", side_effect=cleanup_popen),
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
                    mock.patch.object(recovery.subprocess, "Popen") as popen,
                ):
                    with self.assertRaises(recovery.RecoveryBootstrapError):
                        recovery.abort_retained_only(
                            app_dir=app,
                            generation=app / ("b" * 64),
                        )
                self.assertTrue(state.exists())
                self.assertTrue(candidate.exists())
                popen.assert_not_called()

    def test_operation_bound_legacy_liveqa_recovery_preserves_shared_state(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("legacy recovery bridge contract requires root")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            releases = app / "releases"
            shared = app / "shared"
            current = releases / "current-release"
            previous = releases / "previous-release"
            candidate = releases / "candidate-release"
            for path in (current, previous, candidate, shared):
                path.mkdir(parents=True, exist_ok=True)
            app.mkdir(exist_ok=True)
            (app / "current").symlink_to(current)
            (app / "previous").symlink_to(previous)
            # The authenticated original is intentionally an older release:
            # it has its legacy restore helper but neither managed-LiveQA input.
            (current / "tools").mkdir()
            old_helper = current / "tools/platform_release_restore_runtime.sh"
            old_helper.write_text("legacy immutable helper\n", encoding="ascii")
            shared_venv = shared / "venv"
            shared_venv.mkdir()
            snapshot = candidate / ".rollback/shared-venv-before-install"
            snapshot.mkdir(parents=True)
            candidate_peer = shared / ".venv-install-candidate-release.none"
            state = shared / ".release-operation.json"
            state.write_text("transaction receipt fixture\n", encoding="ascii")
            state.chmod(0o600)
            managed_qa = shared / "liveqa-managed-state"
            managed_qa.write_bytes(b"preserve this managed QA state\n")
            managed_before = hashlib.sha256(managed_qa.read_bytes()).hexdigest()

            def identity(path: Path) -> dict[str, int]:
                metadata = path.lstat()
                return {"dev": metadata.st_dev, "ino": metadata.st_ino}

            receipt: dict[str, object] = {
                "version": 2,
                "operation": "install",
                "operation_id": "a" * 32,
                "phase": "recovery-restored",
                "app_dir": str(app),
                "current_before": str(current),
                "previous_before": str(previous),
                "candidate_release": str(candidate),
                "shared_venv": str(shared_venv),
                "peer": str(candidate_peer),
                "snapshot": str(snapshot),
                "transition": "exchange",
                "shared_before": identity(shared_venv),
                "peer_before": identity(snapshot),
                "current_before_identity": identity(current),
                "previous_before_identity": identity(previous),
                "candidate_identity": identity(candidate),
                "remove_env_on_recovery": False,
                "service_state_before": {
                    "deadlock-api": "active",
                    "deadlock-worker": "inactive",
                    "deadlock-web": "active",
                },
                "service_enabled_before": {
                    "deadlock-api": "enabled",
                    "deadlock-worker": "disabled",
                    "deadlock-web": "enabled",
                },
                "quiesced_services": [
                    "deadlock-api", "deadlock-worker", "deadlock-web",
                ],
                "timer_active_before": True,
                "timer_enabled_before": "enabled",
                "systemd_state_before": None,
            }
            from tools import platform_release_transaction as release_transaction

            # The bound M10 operation used the exchange branch: the original
            # shared venv is restored, while peer_before is retained in the
            # rollback snapshot until receipt-bound cleanup.
            candidate_source_sha = "c" * 40
            candidate_release_metadata = {
                "source_git_commit": candidate_source_sha,
                "release_slug": candidate.name,
            }
            candidate_release_json = candidate / "RELEASE.json"
            candidate_release_json.write_text(
                json.dumps(candidate_release_metadata, sort_keys=True) + "\n",
                encoding="ascii",
            )
            candidate_release_json.chmod(0o444)
            bundle_source = root / "recovery-source"
            bundle_tools = bundle_source / "platform" / "tools"
            bundle_tools.mkdir(parents=True)
            for name in recovery.RECOVERY_FILES:
                shutil.copy2(TOOLS / name, bundle_tools / name)
            recovery_bundle = root / "recovery-bundle.zip"
            generation_record = recovery.build_bundle(
                bundle_source,
                source_sha=SOURCE_SHA,
                provenance=provenance(),
                output=recovery_bundle,
            )
            generation_manifest = generation_record["manifest"]
            self.assertIsInstance(generation_manifest, dict)
            self.assertNotEqual(
                generation_manifest["source_sha"],
                candidate_release_metadata["source_git_commit"],
            )
            generation = root / str(generation_record["bundle_sha256"])
            self.assertEqual(generation.name, generation_record["bundle_sha256"])
            exchange_record: dict[str, object] = {
                "transition": "exchange",
                "shared_venv_path": shared_venv,
                "peer_path": candidate_peer,
                "snapshot_path": snapshot,
                "shared_before": receipt["shared_before"],
                "peer_before": receipt["peer_before"],
                "phase": "recovery-restored",
            }
            release_transaction._verify_restored_venv(exchange_record)
            wrong_original = dict(exchange_record)
            wrong_original["shared_before"] = {"dev": -1, "ino": -1}
            with self.assertRaises(release_transaction.TransactionError):
                release_transaction._verify_restored_venv(wrong_original)
            missing_peer = dict(exchange_record)
            missing_peer["peer_before"] = {"dev": -2, "ino": -2}
            with self.assertRaises(release_transaction.TransactionError):
                release_transaction._verify_restored_venv(missing_peer)
            child_commands: list[list[str]] = []
            readiness_commands: list[list[str]] = []
            recovery_runtime = generation / "platform_release_restore_runtime.sh"

            def fake_popen(command: list[str], **_kwargs: object) -> object:
                class Child:
                    pid = 9123

                    def wait(self, **_wait_kwargs: object) -> int:
                        child_commands.append(command)
                        command_name = next(
                            (part for part in command if part in {
                                "mark-legacy-services-restored",
                                "verify-legacy-services",
                                "complete-recovery",
                            }),
                            None,
                        )
                        # Model the transaction CLI's bounded API/web checks;
                        # those run in the child process, not the bootstrap.
                        if command_name in {
                            "mark-legacy-services-restored",
                            "verify-legacy-services",
                            "complete-recovery",
                        }:
                            readiness_commands.extend([
                                ["/usr/bin/curl", "http://127.0.0.1:8010/api/v1/health/ready"],
                                ["/usr/bin/curl", "http://127.0.0.1:3000/"],
                            ])
                        if "mark-legacy-services-restored" in command:
                            receipt["phase"] = "legacy-services-restored"
                        elif "complete-recovery" in command and "--retain-receipt" in command:
                            shutil.rmtree(candidate)
                        elif "complete-recovery" in command:
                            state.unlink()
                        return 0

                return Child()

            def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                readiness_commands.append(command)
                return subprocess.CompletedProcess(command, 0)

            with (
                mock.patch.object(recovery.os, "geteuid", return_value=0),
                mock.patch.object(recovery, "_validate_generation_tree"),
                mock.patch.object(recovery, "_safe_receipt"),
                mock.patch.object(recovery, "_receipt_json", return_value=receipt),
                mock.patch.object(recovery.subprocess, "Popen", side_effect=fake_popen),
                mock.patch.object(recovery.subprocess, "run", side_effect=fake_run),
            ):
                recovery.abort_retained_only(app_dir=app, generation=generation)

            self.assertEqual(len(readiness_commands), 8)
            child_names = [
                next((part for part in command if part in {
                    "validate-legacy-liveqa-recovery", "restore-legacy-services",
                    "mark-legacy-services-restored", "verify-legacy-services",
                    "complete-recovery",
                }), "runtime-prepare" if command[0] == str(recovery_runtime) else "unknown")
                for command in child_commands
            ]
            self.assertEqual(
                child_names,
                [
                    "validate-legacy-liveqa-recovery", "runtime-prepare",
                    "restore-legacy-services", "mark-legacy-services-restored",
                    "verify-legacy-services",
                    "complete-recovery", "complete-recovery",
                ],
            )
            completion_commands = [
                command for command in child_commands if "complete-recovery" in command
            ]
            self.assertEqual(len(completion_commands), 2)
            for command in completion_commands:
                self.assertIn("--systemctl", command)
                self.assertEqual(
                    command[command.index("--systemctl") + 1],
                    "/usr/bin/systemctl",
                )
            runtime_command = child_commands[1]
            self.assertIn("--prepare-only", runtime_command)
            self.assertIn("--preserve-legacy-live-qa", runtime_command)
            self.assertIn("--transaction", runtime_command)
            self.assertEqual(runtime_command[runtime_command.index("--release") + 1], str(current))
            self.assertNotEqual(runtime_command[0], str(old_helper))
            self.assertFalse((current / "tools/platform_live_qa_runtime_install.py").exists())
            self.assertFalse((current / "liveqa-runtime").exists())
            self.assertTrue(old_helper.is_file())
            self.assertEqual(hashlib.sha256(managed_qa.read_bytes()).hexdigest(), managed_before)
            self.assertFalse(state.exists())
            self.assertFalse(candidate.exists())

    def test_legacy_liveqa_retry_rechecks_snapshot_and_readiness_before_cleanup(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("legacy recovery bridge contract requires root")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            releases = app / "releases"
            shared = app / "shared"
            current = releases / "current-release"
            previous = releases / "previous-release"
            candidate = releases / "candidate-release"
            for path in (current, previous, candidate, shared):
                path.mkdir(parents=True, exist_ok=True)
            app.mkdir(exist_ok=True)
            (app / "current").symlink_to(current)
            (app / "previous").symlink_to(previous)
            (current / "tools").mkdir()
            (current / "tools/platform_release_restore_runtime.sh").write_text(
                "legacy helper\n", encoding="ascii"
            )
            (shared / "venv").mkdir()
            state = shared / ".release-operation.json"
            state.write_text("receipt\n", encoding="ascii")
            state.chmod(0o600)
            managed_qa = shared / "liveqa-managed-state"
            managed_qa.write_bytes(b"must survive failed retry\n")
            managed_before = managed_qa.read_bytes()
            receipt: dict[str, object] = {
                "version": 2,
                "operation": "install",
                "operation_id": "c" * 32,
                "phase": "legacy-services-restored",
                "app_dir": str(app),
                "current_before": str(current),
                "previous_before": str(previous),
                "candidate_release": str(candidate),
                "shared_venv": str(shared / "venv"),
                "peer": str(shared / ".venv-install-candidate-release.none"),
                "snapshot": str(candidate / ".rollback/shared-venv-before-install"),
                "transition": "none",
                "shared_before": {
                    "dev": (shared / "venv").stat().st_dev,
                    "ino": (shared / "venv").stat().st_ino,
                },
                "peer_before": None,
                "current_before_identity": {"dev": current.stat().st_dev, "ino": current.stat().st_ino},
                "previous_before_identity": {"dev": previous.stat().st_dev, "ino": previous.stat().st_ino},
                "candidate_identity": {"dev": candidate.stat().st_dev, "ino": candidate.stat().st_ino},
                "remove_env_on_recovery": False,
                "service_state_before": {
                    "deadlock-api": "active", "deadlock-worker": "inactive", "deadlock-web": "active",
                },
                "service_enabled_before": {
                    "deadlock-api": "enabled", "deadlock-worker": "disabled", "deadlock-web": "enabled",
                },
                "quiesced_services": [
                    "deadlock-api", "deadlock-worker", "deadlock-web",
                ],
                "timer_active_before": True,
                "timer_enabled_before": "enabled",
                "systemd_state_before": None,
            }
            commands: list[list[str]] = []
            calls = {"ready": 0}

            def fake_popen(command: list[str], **_kwargs: object) -> object:
                class Child:
                    pid = 9124

                    def wait(self, **_wait_kwargs: object) -> int:
                        commands.append(command)
                        if "verify-legacy-services" in command:
                            # Retry validation proves both service snapshot and
                            # readiness before either cleanup command can run.
                            api = failed_readiness(["/usr/bin/curl", "api"])
                            web = failed_readiness(["/usr/bin/curl", "web"])
                            return 1 if api.returncode or web.returncode else 0
                        return 0

                return Child()

            def failed_readiness(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls["ready"] += 1
                return subprocess.CompletedProcess(command, 0 if calls["ready"] == 1 else 22)

            generation = root / ("d" * 64)
            with (
                mock.patch.object(recovery.os, "geteuid", return_value=0),
                mock.patch.object(recovery, "_validate_generation_tree"),
                mock.patch.object(recovery, "_safe_receipt"),
                mock.patch.object(recovery, "_receipt_json", return_value=receipt),
                mock.patch.object(recovery.subprocess, "Popen", side_effect=fake_popen),
            ):
                with self.assertRaises(recovery.RecoveryChildError) as raised:
                    recovery.abort_retained_only(app_dir=app, generation=generation)

            self.assertEqual(raised.exception.stage, "legacy_verify_services")
            self.assertEqual(raised.exception.outcome, "child_exit")
            self.assertEqual(raised.exception.child_exit, 1)
            self.assertEqual(calls["ready"], 2)
            self.assertEqual(len(commands), 1)
            self.assertIn("verify-legacy-services", commands[0])
            self.assertTrue(state.exists())
            self.assertTrue(candidate.exists())
            self.assertEqual(managed_qa.read_bytes(), managed_before)

    def test_operation_bound_legacy_path_rejects_nonlegacy_inputs_before_children(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("legacy recovery bridge contract requires root")

        for invalid in ("installer", "runtime-tree", "both", "bad-operation-id", "bad-phase", "pointer"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                app = root / "app"
                releases = app / "releases"
                shared = app / "shared"
                current = releases / "current-release"
                previous = releases / "previous-release"
                candidate = releases / "candidate-release"
                for path in (current, previous, candidate, shared):
                    path.mkdir(parents=True, exist_ok=True)
                app.mkdir(exist_ok=True)
                (app / "current").symlink_to(current)
                (app / "previous").symlink_to(previous)
                (current / "tools").mkdir()
                (current / "tools/platform_release_restore_runtime.sh").write_text(
                    "legacy helper\n", encoding="ascii"
                )
                (shared / "venv").mkdir()
                state = shared / ".release-operation.json"
                state.write_text("receipt\n", encoding="ascii")
                state.chmod(0o600)
                managed_qa = shared / "liveqa-managed-state"
                managed_qa.write_bytes(b"must remain untouched\n")
                managed_before = managed_qa.read_bytes()

                def identity(path: Path) -> dict[str, int]:
                    metadata = path.lstat()
                    return {"dev": metadata.st_dev, "ino": metadata.st_ino}

                receipt: dict[str, object] = {
                    "version": 2,
                    "operation": "install",
                    "operation_id": "e" * 32,
                    "phase": "recovery-restored",
                    "app_dir": str(app),
                    "current_before": str(current),
                    "previous_before": str(previous),
                    "candidate_release": str(candidate),
                    "shared_venv": str(shared / "venv"),
                    "peer": str(shared / ".venv-install-candidate-release.none"),
                    "snapshot": str(candidate / ".rollback/shared-venv-before-install"),
                    "transition": "none",
                    "shared_before": identity(shared / "venv"),
                    "peer_before": None,
                    "current_before_identity": identity(current),
                    "previous_before_identity": identity(previous),
                    "candidate_identity": identity(candidate),
                    "remove_env_on_recovery": False,
                    "service_state_before": {
                        "deadlock-api": "active", "deadlock-worker": "inactive", "deadlock-web": "active",
                    },
                    "service_enabled_before": {
                        "deadlock-api": "enabled", "deadlock-worker": "disabled", "deadlock-web": "enabled",
                    },
                    "quiesced_services": [
                        "deadlock-api", "deadlock-worker", "deadlock-web",
                    ],
                    "timer_active_before": True,
                    "timer_enabled_before": "enabled",
                    "systemd_state_before": None,
                }
                if invalid == "installer":
                    (current / "tools").mkdir(exist_ok=True)
                    (current / "tools/platform_live_qa_runtime_install.py").write_text(
                        "partial managed QA input\n", encoding="ascii"
                    )
                elif invalid == "runtime-tree":
                    (current / "liveqa-runtime").mkdir()
                elif invalid == "both":
                    (current / "tools").mkdir(exist_ok=True)
                    (current / "tools/platform_live_qa_runtime_install.py").write_text(
                        "managed QA installer\n", encoding="ascii"
                    )
                    (current / "liveqa-runtime").mkdir()
                elif invalid == "bad-operation-id":
                    receipt["operation_id"] = "not-an-operation-id"
                elif invalid == "bad-phase":
                    receipt["phase"] = "prepared"
                elif invalid == "pointer":
                    (app / "current").unlink()
                    (app / "current").symlink_to(candidate)

                with (
                    mock.patch.object(recovery.os, "geteuid", return_value=0),
                    mock.patch.object(recovery, "_validate_generation_tree"),
                    mock.patch.object(recovery, "_safe_receipt"),
                    mock.patch.object(recovery, "_receipt_json", return_value=receipt),
                    mock.patch.object(recovery.subprocess, "Popen") as popen,
                ):
                    with self.assertRaises(recovery.RecoveryBootstrapError):
                        recovery.abort_retained_only(
                            app_dir=app,
                            generation=root / ("f" * 64),
                        )
                popen.assert_not_called()
                self.assertTrue(state.exists())
                self.assertTrue(candidate.exists())
                self.assertEqual(managed_qa.read_bytes(), managed_before)

    def test_generic_phase_cannot_mark_legacy_services_restored(self) -> None:
        from tools import platform_release_transaction as transaction

        record: dict[str, object] = {"phase": "recovery-restored"}
        with (
            mock.patch.object(transaction, "_load_record", return_value=record),
            mock.patch.object(transaction, "_write_record") as write_record,
        ):
            with self.assertRaises(transaction.TransactionError):
                transaction.set_phase(
                    Path("/unused-state"),
                    expected="recovery-restored",
                    phase="legacy-services-restored",
                )
        write_record.assert_not_called()

    def test_generic_recover_cannot_rewind_legacy_marker_or_skip_readiness(self) -> None:
        from tools import platform_release_transaction as transaction

        record: dict[str, object] = {
            "phase": "legacy-services-restored",
            "operation": "install",
        }
        with (
            mock.patch.object(transaction, "_load_record", return_value=record),
            mock.patch.object(transaction, "_write_record") as write_record,
            mock.patch.object(transaction, "_cleanup_recovered_install") as cleanup,
            mock.patch.object(transaction, "_restore_pointers") as restore_pointers,
            mock.patch.object(transaction, "_restore_venv") as restore_venv,
            mock.patch.object(transaction, "_verify_recovery_pointers") as verify_recovery,
            mock.patch.object(transaction, "_verify_original_pointers") as verify_original,
        ):
            with self.assertRaises(transaction.TransactionError):
                transaction.recover(Path("/unused-state"))
            with self.assertRaises(transaction.TransactionError):
                transaction.complete_recovery(Path("/unused-state"))
        self.assertEqual(record["phase"], "legacy-services-restored")
        write_record.assert_not_called()
        cleanup.assert_not_called()
        restore_pointers.assert_not_called()
        restore_venv.assert_not_called()
        verify_recovery.assert_not_called()
        verify_original.assert_not_called()

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
        recovery_workflow_sha = "c" * 40
        bundle_digest = "b" * 64
        run_id = "12345"
        attempt = "2"
        build_uri = (
            "https://github.com/StrayForest/old_sparky/"
            ".github/workflows/platform-production-recovery-bootstrap-build.yml"
            "@refs/heads/dev"
        )
        certificate = {
            "issuer": "https://token.actions.githubusercontent.com",
            "sourceRepositoryURI": "https://github.com/StrayForest/old_sparky",
            "sourceRepositoryRef": "refs/heads/dev",
            "sourceRepositoryDigest": recovery_workflow_sha,
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
                    "signature": {"certificate": certificate},
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
                env["RECOVERY_WORKFLOW_SHA"] = recovery_workflow_sha
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
                    timeout=120,
                )

            self.assertEqual(run_policy().returncode, 0)
            for field, bad_value in (
                ("issuer", "https://token.actions.githubusercontent.com.invalid"),
                ("sourceRepositoryURI", "https://github.com/other/repo"),
                ("sourceRepositoryRef", "refs/heads/main"),
                ("sourceRepositoryDigest", "d" * 40),
                ("buildConfigURI", build_uri.replace("refs/heads/dev", "refs/heads/main")),
                ("buildSignerURI", "https://github.com/other/repo/.github/workflows/wrong.yml@refs/heads/dev"),
                (
                    "runInvocationURI",
                    "https://github.com/StrayForest/old_sparky/actions/runs/12345/attempts/3",
                ),
            ):
                with self.subTest(field=field):
                    original = certificate[field]
                    certificate[field] = bad_value
                    try:
                        self.assertNotEqual(run_policy().returncode, 0)
                    finally:
                        certificate[field] = original
            original_runner = certificate["runInvocationURI"]
            certificate["runInvocationURI"] = original_runner.replace("12345", "54321")
            try:
                self.assertNotEqual(run_policy().returncode, 0)
            finally:
                certificate["runInvocationURI"] = original_runner
            subject = payload[0]["verificationResult"]["statement"]["subject"][0]["digest"]
            subject["sha256"] = "c" * 64
            try:
                self.assertNotEqual(run_policy().returncode, 0)
            finally:
                subject["sha256"] = bundle_digest
            payload[0]["verificationResult"]["signature"]["certificate"] = {
                "extensions": dict(certificate),
            }
            try:
                self.assertNotEqual(run_policy().returncode, 0)
            finally:
                payload[0]["verificationResult"]["signature"]["certificate"] = certificate
            payload[0]["verificationResult"]["verifiedTimestamps"] = []
            try:
                self.assertNotEqual(run_policy().returncode, 0)
            finally:
                payload[0]["verificationResult"]["verifiedTimestamps"] = [{"timestamp": "2026-09-27T00:00:00Z"}]
            payload[0]["verificationResult"]["statement"]["subject"].append(
                {"digest": {"sha256": bundle_digest}}
            )
            try:
                self.assertNotEqual(run_policy().returncode, 0)
            finally:
                payload[0]["verificationResult"]["statement"]["subject"].pop()
    def test_publish_validation_is_closed_and_behaviourally_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "metadata"
            metadata.mkdir()
            source_sha = SOURCE_SHA
            security_run_id = "12345"
            security_attempt = "2"
            recovery_run_id = "67890"
            recovery_attempt = "3"
            recovery_workflow_sha = "c" * 40
            security_job_name = "Verification contract"
            recovery_job_name = "Build retained-release recovery bootstrap evidence"
            repository = "StrayForest/old_sparky"
            route_digest = "b" * 64

            valid = {
                "run.json": {
                    "id": int(security_run_id),
                    "run_attempt": int(security_attempt),
                    "head_sha": source_sha,
                    "head_branch": "dev",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "name": "Platform security and build",
                    "path": ".github/workflows/platform-security.yml",
                    "display_title": "Merge pull request #125 from StrayForest/codex/recovery-attestation-j…",
                    "repository": {"full_name": repository},
                },
                "jobs.json": {
                    "total_count": 1,
                    "jobs": [
                        {
                            "id": 11,
                            "name": security_job_name,
                            "run_id": int(security_run_id),
                            "run_attempt": int(security_attempt),
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": source_sha,
                        }
                    ],
                },
                "artifacts.json": {
                    "total_count": 1,
                    "artifacts": [
                        {
                            "id": 22,
                            "name": f"platform-ci-route-{security_run_id}-{security_attempt}",
                            "expired": False,
                            "digest": f"sha256:{route_digest}",
                            "workflow_run": {
                                "id": int(security_run_id),
                                "head_sha": source_sha,
                                "repository": {"full_name": repository},
                            },
                        }
                    ],
                },
                "recovery-jobs.json": {
                    "total_count": 1,
                    "jobs": [
                        {
                            "id": 33,
                            "name": recovery_job_name,
                            "run_id": int(recovery_run_id),
                            "run_attempt": int(recovery_attempt),
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": recovery_workflow_sha,
                        }
                    ],
                },
                "recovery-run.json": {
                    "id": int(recovery_run_id),
                    "run_attempt": int(recovery_attempt),
                    "head_sha": recovery_workflow_sha,
                    "head_branch": "dev",
                    "event": "workflow_run",
                    "status": "completed",
                    "conclusion": "success",
                    "name": "Platform production recovery bootstrap build",
                    "path": ".github/workflows/platform-production-recovery-bootstrap-build.yml",
                    "repository": {"full_name": repository},
                },
            }

            def write_fixture(value: dict[str, object]) -> None:
                for name, payload in value.items():
                    (metadata / name).write_text(
                        json.dumps(payload, sort_keys=True, ensure_ascii=False),
                        encoding="utf-8",
                    )

            def validate(
                value: dict[str, object] | None = None,
                *,
                github_ref: str = "refs/heads/dev",
                selected_recovery_run_id: str = recovery_run_id,
                selected_recovery_attempt: str = recovery_attempt,
                selected_recovery_workflow_sha: str = recovery_workflow_sha,
                write: bool = True,
            ) -> dict[str, object]:
                if write:
                    write_fixture(value or valid)
                return recovery.validate_publish_metadata(
                    metadata,
                    repository=repository,
                    source_sha=source_sha,
                    security_run_id=security_run_id,
                    security_run_attempt=security_attempt,
                    security_workflow="Platform security and build",
                    security_workflow_path=".github/workflows/platform-security.yml",
                    security_job=security_job_name,
                    recovery_run_id=selected_recovery_run_id,
                    recovery_run_attempt=selected_recovery_attempt,
                    recovery_workflow_sha=selected_recovery_workflow_sha,
                    recovery_job=recovery_job_name,
                    github_ref=github_ref,
                )

            result = validate()
            self.assertEqual(result["route_artifact_id"], 22)
            self.assertEqual(result["recovery_job_id"], 33)
            provenance_value = result["provenance"]
            self.assertEqual(provenance_value["artifact_sha256"], route_digest)
            self.assertFalse(provenance_value["deployable"])

            # GitHub's run payload may contain non-ASCII descriptive metadata;
            # it must not prevent validation of the exact ASCII provenance
            # fields.  Invalid UTF-8 remains fail-closed with the input label.
            write_fixture(valid)
            (metadata / "run.json").write_bytes(b'{"display_title":"\xff"}')
            with self.assertRaisesRegex(
                recovery.RecoveryBootstrapError, r"^security run is invalid$"
            ):
                validate(write=False)

            # Two completed producer runs may legitimately publish the same
            # security source SHA (for example after a recovery workflow
            # rerun).  The operator's exact run/attempt selection must choose
            # the matching producer, never the latest/unique SHA match.
            rerun = json.loads(json.dumps(valid))
            rerun["recovery-run.json"].update(
                {"id": 67891, "run_attempt": 4}
            )
            rerun["recovery-jobs.json"]["jobs"][0].update(
                {"run_id": 67891, "run_attempt": 4}
            )
            self.assertEqual(
                validate(
                    rerun,
                    selected_recovery_run_id="67891",
                    selected_recovery_attempt="4",
                )["recovery_job_id"],
                33,
            )
            with self.assertRaises(recovery.RecoveryBootstrapError):
                validate(rerun)

            def rejected(
                label: str,
                mutation,
                *,
                github_ref: str = "refs/heads/dev",
            ) -> None:
                with self.subTest(rejection=label):
                    value = json.loads(json.dumps(valid))
                    mutation(value)
                    with self.assertRaises(recovery.RecoveryBootstrapError):
                        validate(value, github_ref=github_ref)

            rejected("duplicate producer job", lambda value: value["recovery-jobs.json"]["jobs"].append(dict(value["recovery-jobs.json"]["jobs"][0])))
            rejected("duplicate security job", lambda value: value["jobs.json"]["jobs"].append(dict(value["jobs.json"]["jobs"][0])))
            rejected("duplicate route artifact", lambda value: value["artifacts.json"]["artifacts"].append(dict(value["artifacts.json"]["artifacts"][0])))
            rejected("over-100 security jobs page", lambda value: value["jobs.json"].__setitem__("total_count", 101))
            rejected("over-100 security artifact page", lambda value: value["artifacts.json"].__setitem__("total_count", 101))
            rejected("negative security jobs page", lambda value: value["jobs.json"].__setitem__("total_count", -1))
            rejected("negative security artifact page", lambda value: value["artifacts.json"].__setitem__("total_count", -1))
            for bad_id in (0, "22"):
                rejected(
                    f"route artifact id {bad_id!r}",
                    lambda value, bad_id=bad_id: value["artifacts.json"]["artifacts"][0].__setitem__("id", bad_id),
                )
            for field, bad_value in (
                ("id", 54321),
                ("run_attempt", 4),
                ("head_sha", "d" * 40),
                ("head_branch", "main"),
                ("event", "push"),
                ("status", "in_progress"),
                ("conclusion", "failure"),
                ("name", "Other recovery workflow"),
                ("path", ".github/workflows/other.yml"),
            ):
                rejected(
                    f"producer run {field}",
                    lambda value, field=field, bad_value=bad_value: value["recovery-run.json"].__setitem__(field, bad_value),
                )
            for field, bad_value in (
                ("run_id", 54321),
                ("run_attempt", 4),
                ("name", "Other producer"),
                ("head_sha", "d" * 40),
                ("status", "in_progress"),
                ("conclusion", "failure"),
            ):
                rejected(
                    f"producer {field}",
                    lambda value, field=field, bad_value=bad_value: value["recovery-jobs.json"]["jobs"][0].__setitem__(field, bad_value),
                )
            for label, mutation in (
                (
                    "run id",
                    lambda value: value["artifacts.json"]["artifacts"][0]["workflow_run"].__setitem__("id", 54321),
                ),
                (
                    "source SHA",
                    lambda value: value["artifacts.json"]["artifacts"][0]["workflow_run"].__setitem__("head_sha", "c" * 40),
                ),
                (
                    "run attempt",
                    lambda value: value["artifacts.json"]["artifacts"][0]["workflow_run"].__setitem__("run_attempt", 4),
                ),
                (
                    "name",
                    lambda value: value["artifacts.json"]["artifacts"][0].__setitem__("name", "platform-ci-route-wrong"),
                ),
                (
                    "digest",
                    lambda value: value["artifacts.json"]["artifacts"][0].__setitem__("digest", "sha256:not-a-digest"),
                ),
            ):
                rejected(f"route artifact {label}", mutation)
            rejected(
                "expired route artifact",
                lambda value: value["artifacts.json"]["artifacts"][0].__setitem__("expired", True),
            )
            rejected(
                "missing route artifact",
                lambda value: value["artifacts.json"].__setitem__("artifacts", []),
            )
            rejected("wrong event", lambda value: value["run.json"].__setitem__("event", "pull_request"))
            rejected("wrong ref", lambda value: value, github_ref="refs/heads/main")

            bundle_name = (
                f"platform-recovery-bootstrap-{source_sha}-{security_run_id}-"
                f"{security_attempt}-{recovery_run_id}-{recovery_attempt}.zip"
            )
            bundle_bytes = b"immutable bundle fixture\n"
            bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
            bundle_metadata = root / "bundle-artifacts.json"
            bundle_metadata.write_text(
                json.dumps(
                    {
                        "total_count": 1,
                        "artifacts": [
                            {
                                "id": 44,
                                "name": bundle_name,
                                "expired": False,
                                "workflow_run": {
                                    "id": int(recovery_run_id),
                                    "head_sha": recovery_workflow_sha,
                                },
                            }
                        ],
                    }
                ),
                encoding="ascii",
            )
            self.assertEqual(
                recovery.validate_publish_artifact_metadata(
                    bundle_metadata,
                    expected_name=bundle_name,
                    expected_run_id=recovery_run_id,
                    expected_run_attempt=recovery_attempt,
                    expected_source_sha=source_sha,
                    expected_workflow_sha=recovery_workflow_sha,
                ),
                44,
            )
            wrong_attempt = json.loads(bundle_metadata.read_text(encoding="ascii"))
            wrong_attempt["artifacts"][0]["workflow_run"]["run_attempt"] = 4
            bundle_metadata.write_text(json.dumps(wrong_attempt), encoding="ascii")
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.validate_publish_artifact_metadata(
                    bundle_metadata,
                    expected_name=bundle_name,
                    expected_run_id=recovery_run_id,
                    expected_run_attempt=recovery_attempt,
                    expected_source_sha=source_sha,
                    expected_workflow_sha=recovery_workflow_sha,
                )
            duplicate_artifact = {
                "artifacts": [
                    {
                        "id": 44,
                        "name": bundle_name,
                        "expired": False,
                        "workflow_run": {
                            "id": int(recovery_run_id),
                            "head_sha": source_sha,
                        },
                    },
                    {
                        "id": 45,
                        "name": bundle_name,
                        "expired": False,
                        "workflow_run": {
                            "id": int(recovery_run_id),
                            "head_sha": source_sha,
                        },
                    },
                ]
            }
            bundle_metadata.write_text(json.dumps(duplicate_artifact), encoding="ascii")
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.validate_publish_artifact_metadata(
                    bundle_metadata,
                    expected_name=bundle_name,
                    expected_run_id=recovery_run_id,
                    expected_run_attempt=recovery_attempt,
                    expected_source_sha=source_sha,
                    expected_workflow_sha=recovery_workflow_sha,
                )
            bundle_metadata.write_text(
                json.dumps(
                    {
                        "artifacts": [
                            {
                                "id": 44,
                                "name": bundle_name,
                                "expired": True,
                                "workflow_run": {
                                    "id": int(recovery_run_id),
                                    "head_sha": source_sha,
                                },
                            }
                        ]
                    }
                ),
                encoding="ascii",
            )
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.validate_publish_artifact_metadata(
                    bundle_metadata,
                    expected_name=bundle_name,
                    expected_run_id=recovery_run_id,
                    expected_run_attempt=recovery_attempt,
                    expected_source_sha=source_sha,
                    expected_workflow_sha=recovery_workflow_sha,
                )

            artifact_zip = root / "artifact.zip"
            info = zipfile.ZipInfo(bundle_name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            with zipfile.ZipFile(artifact_zip, "w") as archive:
                archive.writestr(info, bundle_bytes)
            artifact_archive_sha = hashlib.sha256(artifact_zip.read_bytes()).hexdigest()
            extracted = root / "bundle.zip"
            recovery.extract_publish_bundle(
                artifact_zip,
                extracted,
                expected_name=bundle_name,
                expected_sha=bundle_sha,
                expected_archive_sha=artifact_archive_sha,
            )
            self.assertEqual(extracted.read_bytes(), bundle_bytes)

            # upload-artifact@v6 uses a deflated ZIP by default.  Keep the
            # outer archive policy closed while accepting that real producer
            # format, and bind the extracted bytes to the same expected
            # digest as the stored fixture above.
            def write_member(path: Path, payload: bytes, compression: int) -> None:
                member = zipfile.ZipInfo(bundle_name)
                member.create_system = 3
                member.external_attr = (stat.S_IFREG | 0o600) << 16
                with zipfile.ZipFile(
                    path,
                    "w",
                    compression=compression,
                    allowZip64=False,
                ) as archive:
                    archive.writestr(member, payload, compress_type=compression)

            def mutate_zip_headers(
                source: Path,
                destination: Path,
                *,
                encrypted: bool = False,
                corrupt_crc: bool = False,
            ) -> None:
                raw = bytearray(source.read_bytes())
                cursor = 0
                while True:
                    local = raw.find(b"PK\x03\x04", cursor)
                    central = raw.find(b"PK\x01\x02", cursor)
                    positions = [position for position in (local, central) if position >= 0]
                    if not positions:
                        break
                    position = min(positions)
                    if position == local:
                        if encrypted:
                            flags = int.from_bytes(raw[position + 6 : position + 8], "little")
                            raw[position + 6 : position + 8] = (flags | 0x1).to_bytes(2, "little")
                        if corrupt_crc:
                            crc = int.from_bytes(raw[position + 14 : position + 18], "little")
                            raw[position + 14 : position + 18] = (crc ^ 0x1).to_bytes(4, "little")
                    else:
                        if encrypted:
                            flags = int.from_bytes(raw[position + 8 : position + 10], "little")
                            raw[position + 8 : position + 10] = (flags | 0x1).to_bytes(2, "little")
                        if corrupt_crc:
                            crc = int.from_bytes(raw[position + 16 : position + 20], "little")
                            raw[position + 16 : position + 20] = (crc ^ 0x1).to_bytes(4, "little")
                    cursor = position + 4
                destination.write_bytes(raw)

            deflated_archive = root / "artifact-deflated.zip"
            write_member(deflated_archive, bundle_bytes, zipfile.ZIP_DEFLATED)
            deflated_extracted = root / "bundle-deflated.zip"
            recovery.extract_publish_bundle(
                deflated_archive,
                deflated_extracted,
                expected_name=bundle_name,
                expected_sha=bundle_sha,
                expected_archive_sha=hashlib.sha256(
                    deflated_archive.read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(deflated_extracted.read_bytes(), bundle_bytes)

            encrypted_archive = root / "artifact-encrypted.zip"
            mutate_zip_headers(deflated_archive, encrypted_archive, encrypted=True)
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.extract_publish_bundle(
                    encrypted_archive,
                    root / "bundle-encrypted.zip",
                    expected_name=bundle_name,
                    expected_sha=bundle_sha,
                )

            corrupt_crc_archive = root / "artifact-corrupt-crc.zip"
            mutate_zip_headers(deflated_archive, corrupt_crc_archive, corrupt_crc=True)
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.extract_publish_bundle(
                    corrupt_crc_archive,
                    root / "bundle-corrupt-crc.zip",
                    expected_name=bundle_name,
                    expected_sha=bundle_sha,
                )

            unsafe_path_archive = root / "artifact-unsafe-path.zip"
            unsafe_info = zipfile.ZipInfo("../escape.zip")
            unsafe_info.create_system = 3
            unsafe_info.external_attr = (stat.S_IFREG | 0o600) << 16
            with zipfile.ZipFile(unsafe_path_archive, "w", allowZip64=False) as archive:
                archive.writestr(unsafe_info, bundle_bytes)
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.extract_publish_bundle(
                    unsafe_path_archive,
                    root / "bundle-unsafe-path.zip",
                    expected_name=bundle_name,
                    expected_sha=bundle_sha,
                )

            replacement_archive = root / "artifact-replacement.zip"
            shutil.copy2(deflated_archive, replacement_archive)
            race_archive = root / "artifact-race.zip"
            shutil.copy2(deflated_archive, race_archive)
            original_open = recovery.os.open

            def replace_archive_before_open(path, flags, *arguments):
                if Path(path) == race_archive:
                    race_archive.unlink()
                    race_archive.symlink_to(replacement_archive)
                return original_open(path, flags, *arguments)

            with mock.patch.object(recovery.os, "open", side_effect=replace_archive_before_open):
                with self.assertRaises(recovery.RecoveryBootstrapError):
                    recovery.extract_publish_bundle(
                        race_archive,
                        root / "bundle-race.zip",
                        expected_name=bundle_name,
                        expected_sha=bundle_sha,
                    )

            unsupported_archive = root / "artifact-unsupported.zip"
            write_member(unsupported_archive, bundle_bytes, zipfile.ZIP_LZMA)
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.extract_publish_bundle(
                    unsupported_archive,
                    root / "bundle-unsupported.zip",
                    expected_name=bundle_name,
                    expected_sha=bundle_sha,
                )

            ratio_archive = root / "artifact-ratio.zip"
            ratio_payload = b"A" * (recovery.MAX_MEMBER_COMPRESSION_RATIO * 2_000)
            ratio_sha = hashlib.sha256(ratio_payload).hexdigest()
            write_member(ratio_archive, ratio_payload, zipfile.ZIP_DEFLATED)
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.extract_publish_bundle(
                    ratio_archive,
                    root / "bundle-ratio.zip",
                    expected_name=bundle_name,
                    expected_sha=ratio_sha,
                )

            oversize_archive = root / "artifact-oversize.zip"
            oversize_payload = b"B" * (recovery.MAX_ARCHIVE_BYTES + 1)
            oversize_sha = hashlib.sha256(oversize_payload).hexdigest()
            write_member(oversize_archive, oversize_payload, zipfile.ZIP_DEFLATED)
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.extract_publish_bundle(
                    oversize_archive,
                    root / "bundle-oversize.zip",
                    expected_name=bundle_name,
                    expected_sha=oversize_sha,
                )

            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.extract_publish_bundle(
                    artifact_zip,
                    root / "wrong-bundle.zip",
                    expected_name=bundle_name,
                    expected_sha="c" * 64,
                )
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.extract_publish_bundle(
                    artifact_zip,
                    root / "wrong-artifact-digest.zip",
                    expected_name=bundle_name,
                    expected_sha=bundle_sha,
                    expected_archive_sha="d" * 64,
                )

            evidence = recovery.build_publish_evidence(
                provenance_value,
                bundle_name=bundle_name,
                bundle_sha=bundle_sha,
                recovery_run_id=recovery_run_id,
                recovery_run_attempt=recovery_attempt,
                recovery_job_id="33",
            )
            self.assertEqual(
                set(evidence),
                {
                    "schema",
                    "capability",
                    "capabilities",
                    "deployable",
                    "bundle_name",
                    "bundle_sha256",
                    "recovery_run_id",
                    "recovery_run_attempt",
                    "recovery_job_id",
                    "provenance",
                },
            )
            self.assertFalse(evidence["deployable"])
            publisher_bundle_name = recovery.publisher_bundle_artifact_name(
                source_sha=str(provenance_value["source_sha"]),
                security_run_id=str(provenance_value["run_id"]),
                security_run_attempt=str(provenance_value["run_attempt"]),
                producer_run_id=recovery_run_id,
                producer_run_attempt=recovery_attempt,
                publisher_run_id="44",
                publisher_run_attempt="1",
            )
            publisher_evidence = recovery.build_publish_evidence(
                provenance_value,
                bundle_name=bundle_name,
                bundle_sha=bundle_sha,
                bundle_artifact_sha256=artifact_archive_sha,
                recovery_run_id=recovery_run_id,
                recovery_run_attempt=recovery_attempt,
                recovery_job_id="33",
                publisher_workflow_sha="c" * 40,
                publisher_run_id="44",
                publisher_run_attempt="1",
                publisher_job_id="55",
                publisher_bundle_name=publisher_bundle_name,
                publisher_bundle_artifact_id="66",
                publisher_bundle_artifact_sha256="e" * 64,
            )
            self.assertEqual(publisher_evidence["schema"], 3)
            self.assertEqual(
                publisher_evidence["bundle_artifact_sha256"], artifact_archive_sha
            )
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.build_publish_evidence(
                    provenance_value,
                    bundle_name=bundle_name,
                    bundle_sha=bundle_sha,
                    bundle_artifact_sha256=artifact_archive_sha,
                    recovery_run_id=recovery_run_id,
                    recovery_run_attempt=recovery_attempt,
                    recovery_job_id="33",
                    publisher_workflow_sha="c" * 40,
                    publisher_run_id="44",
                    publisher_run_attempt="1",
                    publisher_job_id="55",
                    publisher_bundle_name=publisher_bundle_name.replace(
                        "-44-1.zip", "-44-2.zip"
                    ),
                    publisher_bundle_artifact_id="66",
                    publisher_bundle_artifact_sha256="e" * 64,
                )
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.build_publish_evidence(
                    provenance_value,
                    bundle_name=bundle_name,
                    bundle_sha=bundle_sha,
                    recovery_run_id=recovery_run_id,
                    recovery_run_attempt=recovery_attempt,
                    recovery_job_id="0",
                )

    def test_provenance_workflows_have_closed_exact_job_identity_predicates(self) -> None:
        workflow_paths = (
            REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-build.yml",
            REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-abort.yml",
            REPO_ROOT / ".github/workflows/platform-production-release-recover.yml",
            REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-publish.yml",
        )
        for path in workflow_paths:
            text_value = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertIn('type(row.get("id")) is int', text_value)
                self.assertIn('row.get("id") > 0', text_value)
                self.assertIn('row.get("run_id") ==', text_value)
                self.assertIn('row.get("run_attempt") ==', text_value)

    def test_publisher_self_job_predicate_is_exact_and_in_progress_only(self) -> None:
        text_value = (
            REPO_ROOT
            / ".github/workflows/platform-production-recovery-bootstrap-publish.yml"
        ).read_text(encoding="utf-8")
        for predicate in (
            'type(row.get("id")) is int',
            'row.get("id") > 0',
            'row.get("run_id") == int(os.environ["PUBLISHER_RUN_ID"])',
            'row.get("run_attempt") == int(os.environ["PUBLISHER_RUN_ATTEMPT"])',
            'row.get("head_sha") == os.environ["PUBLISHER_WORKFLOW_SHA"]',
            'row.get("status") == "in_progress"',
            'row.get("conclusion") is None',
        ):
            with self.subTest(predicate=predicate):
                self.assertIn(predicate, text_value)
        self.assertIn("github.event.workflow_run.status == 'completed'", text_value)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", text_value)
        abort_text = (
            REPO_ROOT
            / ".github/workflows/platform-production-recovery-bootstrap-abort.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'hashlib.sha256((root / "bundle.zip").read_bytes()).hexdigest()',
            abort_text,
        )
        recover_text = (
            REPO_ROOT / ".github/workflows/platform-production-release-recover.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("-o ServerAliveInterval=5", recover_text)
        self.assertIn("-o ServerAliveCountMax=2", recover_text)

    def test_operator_workflows_bound_api_pages_and_use_exact_selected_rows(self) -> None:
        workflow_paths = (
            REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-abort.yml",
            REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-publish.yml",
            REPO_ROOT / ".github/workflows/platform-production-release-recover.yml",
        )
        for path in workflow_paths:
            text_value = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertIn("total_count", text_value)
                self.assertIn("0 <=", text_value)
                self.assertTrue("> 100" in text_value or "<= 100" in text_value)
                self.assertNotIn("next(", text_value)
                self.assertIn("if len(matches) != 1", text_value)
                self.assertIn("matches[0]", text_value)

    def test_security_run_metadata_uses_exact_attempt_endpoint(self) -> None:
        workflow_paths = (
            REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-build.yml",
            REPO_ROOT / ".github/workflows/platform-production-recovery-bootstrap-abort.yml",
            REPO_ROOT / ".github/workflows/platform-production-release-recover.yml",
        )
        for path in workflow_paths:
            text_value = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertIn(
                    "$api/actions/runs/$SECURITY_RUN_ID/attempts/$SECURITY_RUN_ATTEMPT",
                    text_value,
                )
                self.assertIn("0 <=", text_value)
                self.assertTrue("> 100" in text_value or "<= 100" in text_value)

    def test_publisher_outer_artifact_metadata_binds_exact_c_attempt_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "publisher-artifacts.json"
            name = recovery.publisher_bundle_artifact_name(
                source_sha="a" * 40,
                security_run_id="101",
                security_run_attempt="2",
                producer_run_id="202",
                producer_run_attempt="3",
                publisher_run_id="303",
                publisher_run_attempt="4",
            )
            row = {
                "id": 404,
                "name": name,
                "expired": False,
                "digest": "sha256:" + "e" * 64,
                "workflow_run": {
                    "id": 303,
                    "head_sha": "c" * 40,
                    # GitHub's artifact row may omit run_attempt.  The
                    # authoritative publisher run/attempt is supplied to the
                    # validator separately and must still be accepted.
                },
            }

            def write(rows: list[dict[str, object]]) -> None:
                metadata.write_text(
                    json.dumps({"total_count": len(rows), "artifacts": rows}),
                    encoding="ascii",
                )

            write([row])
            selected = recovery.validate_publisher_artifact_metadata(
                metadata,
                expected_name=name,
                expected_run_id="303",
                expected_run_attempt="4",
                expected_workflow_sha="c" * 40,
            )
            self.assertEqual(selected["publisher_bundle_name"], name)
            self.assertEqual(selected["publisher_bundle_artifact_id"], 404)
            self.assertEqual(selected["publisher_bundle_artifact_sha256"], "e" * 64)

            for field, value in (
                ("id", 0),
                ("name", name.replace("-303-4.zip", "-303-5.zip")),
                ("expired", True),
                ("digest", "sha256:invalid"),
            ):
                mutated = json.loads(json.dumps(row))
                mutated[field] = value
                write([mutated])
                with self.subTest(field=field), self.assertRaises(
                    recovery.RecoveryBootstrapError
                ):
                    recovery.validate_publisher_artifact_metadata(
                        metadata,
                        expected_name=name,
                        expected_run_id="303",
                        expected_run_attempt="4",
                        expected_workflow_sha="c" * 40,
                    )

            for field, value in (
                ("workflow_run", {"id": 304, "head_sha": "c" * 40}),
                (
                    "workflow_run",
                    {"id": 303, "head_sha": "d" * 40, "run_attempt": 4},
                ),
                (
                    "workflow_run",
                    {"id": 303, "head_sha": "c" * 40, "run_attempt": 5},
                ),
            ):
                mutated = json.loads(json.dumps(row))
                mutated[field] = value
                write([mutated])
                with self.subTest(workflow_field=repr(value)), self.assertRaises(
                    recovery.RecoveryBootstrapError
                ):
                    recovery.validate_publisher_artifact_metadata(
                        metadata,
                        expected_name=name,
                        expected_run_id="303",
                        expected_run_attempt="4",
                        expected_workflow_sha="c" * 40,
                    )

            write([row, row])
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.validate_publisher_artifact_metadata(
                    metadata,
                    expected_name=name,
                    expected_run_id="303",
                    expected_run_attempt="4",
                    expected_workflow_sha="c" * 40,
                )

    def test_publisher_artifact_run_identity_rejects_boolean_or_noninteger_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "publisher-artifacts.json"

            def name(attempt: str) -> str:
                return recovery.publisher_bundle_artifact_name(
                    source_sha="a" * 40,
                    security_run_id="101",
                    security_run_attempt="2",
                    producer_run_id="202",
                    producer_run_attempt="3",
                    publisher_run_id="1",
                    publisher_run_attempt=attempt,
                )

            def write(rows: list[object]) -> None:
                metadata.write_text(
                    json.dumps({"total_count": len(rows), "artifacts": rows}),
                    encoding="ascii",
                )

            exact_name = name("1")
            exact_row = {
                "id": 404,
                "name": exact_name,
                "expired": False,
                "digest": "sha256:" + "e" * 64,
                "workflow_run": {
                    "id": 1,
                    "head_sha": "c" * 40,
                    "run_attempt": 1,
                },
            }
            write([exact_row])
            selected = recovery.validate_publisher_artifact_metadata(
                metadata,
                expected_name=exact_name,
                expected_run_id="1",
                expected_run_attempt="1",
                expected_workflow_sha="c" * 40,
            )
            self.assertEqual(selected["publisher_bundle_artifact_id"], 404)

            for field, invalid_value in (
                ("id", True),
                ("id", "1"),
                ("id", 1.0),
                ("run_attempt", True),
                ("run_attempt", "1"),
                ("run_attempt", 1.0),
            ):
                with self.subTest(exact_field=field, value=repr(invalid_value)):
                    row = json.loads(json.dumps(exact_row))
                    row["workflow_run"][field] = invalid_value
                    write([row])
                    with self.assertRaises(recovery.RecoveryBootstrapError):
                        recovery.validate_publisher_artifact_metadata(
                            metadata,
                            expected_name=exact_name,
                            expected_run_id="1",
                            expected_run_attempt="1",
                            expected_workflow_sha="c" * 40,
                        )

            stale_name = name("1")
            expected_name = name("2")
            valid_stale_row = {
                "id": 405,
                "name": stale_name,
                "expired": False,
                "digest": "sha256:" + "f" * 64,
                "workflow_run": {
                    "id": 1,
                    "head_sha": "c" * 40,
                    "run_attempt": 1,
                },
            }
            write([valid_stale_row])
            with self.assertRaises(recovery.RecoveryArtifactMetadataPending):
                recovery.validate_publisher_artifact_metadata(
                    metadata,
                    expected_name=expected_name,
                    expected_run_id="1",
                    expected_run_attempt="2",
                    expected_workflow_sha="c" * 40,
                )

            for location, field, invalid_value in (
                ("workflow_run", "id", True),
                ("workflow_run", "id", "1"),
                ("workflow_run", "run_attempt", True),
                ("workflow_run", "run_attempt", "1"),
                ("row", "id", False),
                ("row", "id", "405"),
                ("row", "expired", True),
                ("row", "digest", "sha256:invalid"),
            ):
                with self.subTest(
                    stale_location=location,
                    field=field,
                    value=repr(invalid_value),
                ):
                    stale_row = json.loads(json.dumps(valid_stale_row))
                    target = (
                        stale_row["workflow_run"] if location == "workflow_run" else stale_row
                    )
                    target[field] = invalid_value
                    write([stale_row])
                    try:
                        recovery.validate_publisher_artifact_metadata(
                            metadata,
                            expected_name=expected_name,
                            expected_run_id="1",
                            expected_run_attempt="2",
                            expected_workflow_sha="c" * 40,
                        )
                    except recovery.RecoveryBootstrapError as exc:
                        self.assertIs(type(exc), recovery.RecoveryBootstrapError)
                    else:
                        self.fail("malformed stale-attempt identity was not rejected")

    def test_publisher_artifact_visibility_wait_is_bounded_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "publisher-artifacts.json"
            github_output = root / "github-output.txt"
            name = recovery.publisher_bundle_artifact_name(
                source_sha="a" * 40,
                security_run_id="101",
                security_run_attempt="2",
                producer_run_id="202",
                producer_run_attempt="3",
                publisher_run_id="303",
                publisher_run_attempt="4",
            )
            row = {
                "id": 404,
                "name": name,
                "expired": False,
                "digest": "sha256:" + "e" * 64,
                "workflow_run": {"id": 303, "head_sha": "c" * 40},
            }

            def invoke(payload: object, *, attempt: int) -> tuple[int, str]:
                if isinstance(payload, str):
                    metadata.write_text(payload, encoding="ascii")
                else:
                    metadata.write_text(json.dumps(payload), encoding="ascii")
                github_output.unlink(missing_ok=True)
                stderr = io.StringIO()
                with mock.patch("sys.stderr", new=stderr):
                    status = recovery.main(
                        [
                            "publisher-artifact",
                            "--metadata",
                            str(metadata),
                            "--artifact-name",
                            name,
                            "--publisher-run-id",
                            "303",
                            "--publisher-run-attempt",
                            "4",
                            "--publisher-workflow-sha",
                            "c" * 40,
                            "--github-output",
                            str(github_output),
                            "--metadata-poll-attempt",
                            str(attempt),
                            "--metadata-poll-max-attempts",
                            str(recovery.PUBLISHER_ARTIFACT_METADATA_MAX_ATTEMPTS),
                        ]
                    )
                return status, stderr.getvalue()

            def page(rows: list[object]) -> dict[str, object]:
                return {"total_count": len(rows), "artifacts": rows}

            status, stderr = invoke(page([]), attempt=1)
            self.assertEqual(status, 3)
            self.assertIn("RECOVERY_BOOTSTRAP_WAIT", stderr)
            self.assertFalse(github_output.exists())

            status, stderr = invoke(page([row]), attempt=2)
            self.assertEqual(status, 0)
            self.assertEqual(stderr, "")
            self.assertIn("publisher_bundle_artifact_id=404", github_output.read_text())
            self.assertIn("publisher_bundle_artifact_sha256=" + "e" * 64, github_output.read_text())

            status, stderr = invoke(page([]), attempt=recovery.PUBLISHER_ARTIFACT_METADATA_MAX_ATTEMPTS)
            self.assertEqual(status, 2)
            self.assertIn("metadata_not_visible_within_poll_limit", stderr)
            self.assertNotIn("RECOVERY_BOOTSTRAP_WAIT", stderr)
            self.assertFalse(github_output.exists())

            invalid_rows: list[tuple[str, list[object]]] = []
            wrong_name = json.loads(json.dumps(row))
            wrong_name["name"] = name.replace("-303-4.zip", "-303-5.zip")
            invalid_rows.append(("wrong_name", [wrong_name]))
            for field, value in (
                ("id", 0),
                ("expired", True),
                ("digest", "sha256:invalid"),
                ("workflow_run", {"id": 304, "head_sha": "c" * 40}),
                ("workflow_run", {"id": 303, "head_sha": "d" * 40}),
                (
                    "workflow_run",
                    {"id": 303, "head_sha": "c" * 40, "run_attempt": 5},
                ),
            ):
                invalid = json.loads(json.dumps(row))
                invalid[field] = value
                invalid_rows.append((f"{field}:{value!r}", [invalid]))
            invalid_rows.extend(
                (
                    ("duplicate", [row, row]),
                    ("malformed_row", [None]),
                )
            )
            invalid_pages: list[tuple[str, object]] = [
                (name, page(rows)) for name, rows in invalid_rows
            ]
            invalid_pages.extend(
                (
                    ("incomplete_page", {"total_count": 1, "artifacts": []}),
                    ("invalid_json", "{"),
                )
            )
            for label, invalid_page in invalid_pages:
                with self.subTest(case=label):
                    status, stderr = invoke(invalid_page, attempt=1)
                    self.assertEqual(status, 2)
                    self.assertNotIn("RECOVERY_BOOTSTRAP_WAIT", stderr)

            workflow = (
                REPO_ROOT
                / ".github/workflows/platform-production-recovery-bootstrap-publish.yml"
            ).read_text(encoding="utf-8")
            start = workflow.index("- name: Fetch exact C bundle artifact metadata")
            end = workflow.index("- name: Build closed publisher evidence after C upload", start)
            metadata_step = workflow[start:end]
            self.assertIn("poll_max_attempts=6", metadata_step)
            self.assertIn("sleep 2", metadata_step)
            self.assertIn("--fail-with-body", metadata_step)
            self.assertNotIn("--retry", metadata_step)
            self.assertIn('if [[ "$status" -ne 3 ]]; then', metadata_step)

    def test_publisher_artifact_visibility_workflow_polls_only_valid_empty_pages(self) -> None:
        workflow = (
            REPO_ROOT
            / ".github/workflows/platform-production-recovery-bootstrap-publish.yml"
        ).read_text(encoding="utf-8")
        lines = workflow.splitlines()
        step_start = lines.index("      - name: Fetch exact C bundle artifact metadata")
        run_line = next(
            index
            for index in range(step_start, len(lines))
            if lines[index] == "        run: |"
        )
        script_lines: list[str] = []
        for line in lines[run_line + 1 :]:
            if line and not line.startswith("          "):
                break
            script_lines.append(line[10:] if line else "")
        publisher_step = textwrap.dedent("\n".join(script_lines))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted_tools = root / "trusted-source/platform/tools"
            trusted_tools.mkdir(parents=True)
            for filename in (
                "platform_recovery_bootstrap.py",
                "platform_release_systemd_state.py",
                "platform_release_transaction.py",
            ):
                shutil.copyfile(TOOLS / filename, trusted_tools / filename)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_curl = bin_dir / "curl"
            fake_curl.write_text(
                "#!/bin/bash\n"
                "set -euo pipefail\n"
                "count=0\n"
                "if [[ -f $CURL_COUNT_FILE ]]; then count=$(<\"$CURL_COUNT_FILE\"); fi\n"
                "count=$((count + 1))\n"
                "printf '%s' \"$count\" > \"$CURL_COUNT_FILE\"\n"
                "output=''\n"
                "has_fail_with_body=0\n"
                "while (($#)); do\n"
                "  if [[ $1 == --output ]]; then output=$2; shift 2; continue; fi\n"
                "  if [[ $1 == --fail-with-body ]]; then has_fail_with_body=1; fi\n"
                "  shift\n"
                "done\n"
                "[[ $has_fail_with_body == 1 ]] || exit 64\n"
                "if [[ ${CURL_STATUS:-0} != 0 ]]; then exit \"$CURL_STATUS\"; fi\n"
                "cp \"$CURL_RESPONSE_DIR/$count.json\" \"$output\"\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            fake_sleep = bin_dir / "sleep"
            fake_sleep.write_text(
                "#!/bin/bash\nset -euo pipefail\nprintf '%s\\n' \"$*\" >> \"$SLEEP_LOG\"\n",
                encoding="utf-8",
            )
            fake_sleep.chmod(0o755)

            name = recovery.publisher_bundle_artifact_name(
                source_sha="a" * 40,
                security_run_id="101",
                security_run_attempt="2",
                producer_run_id="202",
                producer_run_attempt="3",
                publisher_run_id="303",
                publisher_run_attempt="4",
            )
            row = {
                "id": 404,
                "name": name,
                "expired": False,
                "digest": "sha256:" + "e" * 64,
                "workflow_run": {"id": 303, "head_sha": "c" * 40},
            }

            def page(rows: list[object]) -> dict[str, object]:
                return {"total_count": len(rows), "artifacts": rows}

            def run_step(
                responses: list[object], *, curl_status: int = 0
            ) -> tuple[subprocess.CompletedProcess[str], int, list[str], str]:
                runner_temp = root / "runner-temp"
                metadata_dir = runner_temp / "platform-recovery-publisher"
                metadata_dir.mkdir(parents=True, exist_ok=True)
                response_dir = root / "responses"
                response_dir.mkdir(exist_ok=True)
                for previous in response_dir.iterdir():
                    previous.unlink()
                for index, response in enumerate(responses, start=1):
                    payload = response if isinstance(response, str) else json.dumps(response)
                    (response_dir / f"{index}.json").write_text(payload, encoding="ascii")
                count_file = root / "curl-count"
                count_file.unlink(missing_ok=True)
                sleep_log = root / "sleep-log"
                sleep_log.unlink(missing_ok=True)
                github_output = root / "github-output"
                github_output.unlink(missing_ok=True)
                github_output.touch(mode=0o600)
                env = os.environ.copy()
                env.update(
                    {
                        "PATH": f"{bin_dir}:/usr/bin:/bin",
                        "RUNNER_TEMP": str(runner_temp),
                        "GITHUB_API_URL": "https://api.example.invalid",
                        "REPOSITORY": "StrayForest/old_sparky",
                        "GH_TOKEN": "test-token",
                        "PUBLISHER_RUN_ID": "303",
                        "PUBLISHER_RUN_ATTEMPT": "4",
                        "PUBLISHER_WORKFLOW_SHA": "c" * 40,
                        "PUBLISHER_BUNDLE_NAME": name,
                        "GITHUB_OUTPUT": str(github_output),
                        "CURL_RESPONSE_DIR": str(response_dir),
                        "CURL_COUNT_FILE": str(count_file),
                        "SLEEP_LOG": str(sleep_log),
                        "CURL_STATUS": str(curl_status),
                    }
                )
                result = subprocess.run(
                    ["/bin/bash", "-c", publisher_step],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                count = int(count_file.read_text(encoding="ascii")) if count_file.exists() else 0
                sleeps = (
                    sleep_log.read_text(encoding="ascii").splitlines()
                    if sleep_log.exists()
                    else []
                )
                return result, count, sleeps, github_output.read_text(encoding="ascii")

            result, calls, sleeps, output = run_step([page([]), page([row])])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((calls, sleeps), (2, ["2"]))
            self.assertIn("publisher_bundle_artifact_id=404", output)
            self.assertIn("publisher_bundle_artifact_sha256=" + "e" * 64, output)

            result, calls, sleeps, output = run_step(
                [page([]) for _ in range(recovery.PUBLISHER_ARTIFACT_METADATA_MAX_ATTEMPTS)]
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(calls, recovery.PUBLISHER_ARTIFACT_METADATA_MAX_ATTEMPTS)
            self.assertEqual(sleeps, ["2"] * (calls - 1))
            self.assertEqual(output, "")
            self.assertIn("metadata_not_visible_within_poll_limit", result.stderr)

            invalid_pages: list[tuple[str, object]] = []
            wrong_name = json.loads(json.dumps(row))
            wrong_name["name"] = name.replace("-303-4.zip", "-303-5.zip")
            invalid_pages.append(("wrong_name", page([wrong_name])))
            wrong_run = json.loads(json.dumps(row))
            wrong_run["workflow_run"]["id"] = 304
            invalid_pages.append(("wrong_run", page([wrong_run])))
            wrong_sha = json.loads(json.dumps(row))
            wrong_sha["workflow_run"]["head_sha"] = "d" * 40
            invalid_pages.append(("wrong_sha", page([wrong_sha])))
            invalid_pages.extend(
                (
                    ("malformed_json", "{"),
                    ("malformed_page", {"total_count": 1, "artifacts": []}),
                    ("duplicate", page([row, row])),
                )
            )
            for label, payload in invalid_pages:
                with self.subTest(immediate_rejection=label):
                    result, calls, sleeps, output = run_step([payload])
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual((calls, sleeps, output), (1, [], ""))
                    self.assertNotIn("RECOVERY_BOOTSTRAP_WAIT", result.stderr)

            for label, curl_status in (("api_or_auth", 22), ("network", 28)):
                with self.subTest(immediate_transport_failure=label):
                    result, calls, sleeps, output = run_step(
                        [], curl_status=curl_status
                    )
                    self.assertEqual(result.returncode, curl_status, result.stderr)
                    self.assertEqual((calls, sleeps, output), (1, [], ""))
                    self.assertNotIn("RECOVERY_BOOTSTRAP_WAIT", result.stderr)

    def test_completed_publisher_event_identity_is_exact_and_rerun_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory)
            repository = "StrayForest/old_sparky"
            source_sha = "a" * 40
            producer_sha = "b" * 40
            publisher_sha = "c" * 40
            route_digest = "d" * 64
            bundle_digest = "e" * 64
            producer_id, producer_attempt = 200, 3
            security_id, security_attempt = 100, 2

            def fixture(producer_run: int, producer_try: int) -> dict[str, object]:
                bundle_name = (
                    f"platform-recovery-bootstrap-{source_sha}-{security_id}-"
                    f"{security_attempt}-{producer_run}-{producer_try}.zip"
                )
                return {
                    "producer-run.json": {
                        "id": producer_run,
                        "run_attempt": producer_try,
                        "head_sha": producer_sha,
                        "head_branch": "dev",
                        "event": "workflow_run",
                        "status": "completed",
                        "conclusion": "success",
                        "name": "Platform production recovery bootstrap build",
                        "path": ".github/workflows/platform-production-recovery-bootstrap-build.yml",
                        "repository": {"full_name": repository},
                    },
                    "producer-jobs.json": {
                        "total_count": 1,
                        "jobs": [{
                            "id": producer_run + 1,
                            "name": "Build retained-release recovery bootstrap evidence",
                            "run_id": producer_run,
                            "run_attempt": producer_try,
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": producer_sha,
                        }],
                    },
                    "producer-artifacts.json": {
                        "total_count": 1,
                        "artifacts": [{
                            "id": producer_run + 2,
                            "name": bundle_name,
                            "expired": False,
                            "digest": f"sha256:{bundle_digest}",
                            "workflow_run": {
                                "id": producer_run,
                                "run_attempt": producer_try,
                                "head_sha": producer_sha,
                                "repository": {"full_name": repository},
                            },
                        }],
                    },
                    "security-run.json": {
                        "id": security_id,
                        "run_attempt": security_attempt,
                        "head_sha": source_sha,
                        "head_branch": "dev",
                        "event": "push",
                        "status": "completed",
                        "conclusion": "success",
                        "name": "Platform security and build",
                        "path": ".github/workflows/platform-security.yml",
                        "repository": {"full_name": repository},
                    },
                    "security-jobs.json": {
                        "total_count": 1,
                        "jobs": [{
                            "id": 110,
                            "name": "Verification contract",
                            "run_id": security_id,
                            "run_attempt": security_attempt,
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": source_sha,
                        }],
                    },
                    "security-artifacts.json": {
                        "total_count": 1,
                        "artifacts": [{
                            "id": 120,
                            "name": f"platform-ci-route-{security_id}-{security_attempt}",
                            "expired": False,
                            "digest": f"sha256:{route_digest}",
                            "workflow_run": {
                                "id": security_id,
                                "run_attempt": security_attempt,
                                "head_sha": source_sha,
                                "repository": {"full_name": repository},
                            },
                        }],
                    },
                }

            def write(value: dict[str, object]) -> None:
                for name, payload in value.items():
                    (metadata / name).write_text(json.dumps(payload), encoding="ascii")

            value = fixture(producer_id, producer_attempt)
            write(value)
            selected = recovery.select_publisher_bundle_metadata(
                metadata,
                repository=repository,
                producer_run_id=str(producer_id),
                producer_run_attempt=str(producer_attempt),
                producer_workflow="Platform production recovery bootstrap build",
                producer_workflow_path=".github/workflows/platform-production-recovery-bootstrap-build.yml",
            )
            self.assertEqual(selected["producer_job_id"], producer_id + 1)
            self.assertEqual(selected["bundle_artifact_id"], producer_id + 2)
            over_100 = json.loads(json.dumps(value))
            producer_jobs = over_100["producer-jobs.json"]["jobs"]
            producer_jobs.extend(
                {
                    "id": producer_id + 100 + index,
                    "name": "unrelated producer job",
                    "run_id": producer_id,
                    "run_attempt": producer_attempt,
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": producer_sha,
                }
                for index in range(100)
            )
            over_100["producer-jobs.json"]["total_count"] = 101
            write(over_100)
            with self.assertRaises(recovery.RecoveryBootstrapError):
                recovery.select_publisher_bundle_metadata(
                    metadata,
                    repository=repository,
                    producer_run_id=str(producer_id),
                    producer_run_attempt=str(producer_attempt),
                    producer_workflow="Platform production recovery bootstrap build",
                    producer_workflow_path=".github/workflows/platform-production-recovery-bootstrap-build.yml",
                )
            write(value)
            result = recovery.validate_publisher_metadata(
                metadata,
                repository=repository,
                producer_run_id=str(producer_id),
                producer_run_attempt=str(producer_attempt),
                producer_workflow="Platform production recovery bootstrap build",
                producer_workflow_path=".github/workflows/platform-production-recovery-bootstrap-build.yml",
                security_workflow="Platform security and build",
                security_workflow_path=".github/workflows/platform-security.yml",
                security_job="Verification contract",
                publisher_workflow_sha=publisher_sha,
                publisher_run_id="300",
                publisher_run_attempt="1",
                publisher_job_id="301",
                github_ref="refs/heads/dev",
            )
            self.assertEqual(result["publisher_workflow_sha"], publisher_sha)
            self.assertEqual(result["producer_workflow_sha"], producer_sha)

            for field, bad_value in (
                ("status", "in_progress"),
                ("event", "push"),
                ("head_sha", source_sha),
                ("conclusion", "failure"),
            ):
                mutated = json.loads(json.dumps(value))
                mutated["producer-run.json"][field] = bad_value
                write(mutated)
                with self.subTest(field=field), self.assertRaises(recovery.RecoveryBootstrapError):
                    recovery.select_publisher_bundle_metadata(
                        metadata,
                        repository=repository,
                        producer_run_id=str(producer_id),
                        producer_run_attempt=str(producer_attempt),
                        producer_workflow="Platform production recovery bootstrap build",
                        producer_workflow_path=".github/workflows/platform-production-recovery-bootstrap-build.yml",
                    )
            rerun = fixture(201, 4)
            write(rerun)
            rerun_result = recovery.validate_publisher_metadata(
                metadata,
                repository=repository,
                producer_run_id="201",
                producer_run_attempt="4",
                producer_workflow="Platform production recovery bootstrap build",
                producer_workflow_path=".github/workflows/platform-production-recovery-bootstrap-build.yml",
                security_workflow="Platform security and build",
                security_workflow_path=".github/workflows/platform-security.yml",
                security_job="Verification contract",
                publisher_workflow_sha=publisher_sha,
                publisher_run_id="302",
                publisher_run_attempt="2",
                publisher_job_id="303",
                github_ref="refs/heads/dev",
            )
            self.assertEqual(rerun_result["producer_run_id"], "201")
            self.assertEqual(rerun_result["producer_run_attempt"], "4")

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
        delta_file_digest = hashlib.sha256(
            "\n".join(sorted(RECOVERY_BOOTSTRAP_CURRENT_DELTA_FILES)).encode()
        ).hexdigest()
        self.assertEqual(
            len(RECOVERY_BOOTSTRAP_CURRENT_DELTA_FILES),
            RECOVERY_BOOTSTRAP_CURRENT_DELTA_FILE_COUNT,
        )
        self.assertEqual(
            delta_file_digest,
            RECOVERY_BOOTSTRAP_CURRENT_DELTA_FILE_DIGEST,
        )
        paths = sorted(RECOVERY_BOOTSTRAP_PATCH_FILES)
        pure_bootstrap_paths = sorted(
            RECOVERY_BOOTSTRAP_PATCH_FILES
            - {
                "platform/tools/platform_live_qa_runtime_install.py",
                "platform/tools/platform_workflow_remote_dispatch.py",
                "platform/tools/platform_production_deploy_supervisor.sh",
            }
        )
        for event, branch in (
            ("pull_request", "feature/recovery-bootstrap"),
            ("push", "dev"),
        ):
            with self.subTest(event=event):
                manifest = classifier.classify(
                    pure_bootstrap_paths,
                    event=event,
                    target_sha="a" * 40,
                    branch=branch,
                )
                self.assertEqual(set(manifest["files"]), set(pure_bootstrap_paths))
                self.assertEqual(manifest["class"], "full")
                self.assertFalse(manifest["deployable"])
                self.assertFalse(manifest["fallback"])
                classifier.validate_manifest(manifest, expected_target_sha="a" * 40)

        candidate_route = classifier.classify(
            paths,
            event="push",
            target_sha="a" * 40,
            branch="dev",
        )
        self.assertEqual(candidate_route["class"], "full")
        self.assertTrue(candidate_route["deployable"])
        self.assertTrue(candidate_route["runtime_sensitive"])
        self.assertFalse(candidate_route["fallback"])
        classifier.validate_manifest(candidate_route, expected_target_sha="a" * 40)

        delta_paths = sorted(RECOVERY_BOOTSTRAP_CURRENT_DELTA_FILES)
        pure_delta_paths = sorted(
            RECOVERY_BOOTSTRAP_CURRENT_DELTA_FILES
            - {
                "platform/tools/platform_workflow_remote_dispatch.py",
                "platform/tools/platform_production_deploy_supervisor.sh",
            }
        )
        for event, branch in (
            ("pull_request", "feature/recovery-bootstrap-delta"),
            ("push", "dev"),
        ):
            with self.subTest(delta_event=event):
                manifest = classifier.classify(
                    pure_delta_paths,
                    event=event,
                    target_sha="a" * 40,
                    branch=branch,
                )
                self.assertEqual(set(manifest["files"]), set(pure_delta_paths))
                self.assertEqual(manifest["class"], "full")
                self.assertFalse(manifest["deployable"])
                self.assertFalse(manifest["fallback"])
                classifier.validate_manifest(manifest, expected_target_sha="a" * 40)

        mixed_delta = classifier.classify(
            delta_paths,
            event="push",
            target_sha="a" * 40,
            branch="dev",
        )
        self.assertEqual(
            set(mixed_delta["files"]), RECOVERY_BOOTSTRAP_CURRENT_DELTA_FILES
        )
        self.assertEqual(mixed_delta["class"], "full")
        self.assertTrue(mixed_delta["deployable"])
        self.assertTrue(mixed_delta["runtime_sensitive"])
        self.assertFalse(mixed_delta["fallback"])
        classifier.validate_manifest(mixed_delta, expected_target_sha="a" * 40)

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
        # Keep the exact historical fixture intact while recording that both
        # candidate-owned runtime changes promote the trusted dev push.
        derived_patch_files = (
            canonical - RECOVERY_BOOTSTRAP_ALLOWLIST_ONLY_FILES
        ) | RECOVERY_BOOTSTRAP_PATCH_DOCS | {
            "platform/tools/platform_production_deploy_supervisor.sh",
            "platform/tools/platform_live_qa_runtime_install.py",
            "platform/tools/platform_workflow_remote_dispatch.py",
        }
        self.assertEqual(derived_patch_files, RECOVERY_BOOTSTRAP_PATCH_FILES)
        classifier_files = self._set_assignment(
            classifier_source, "RECOVERY_BOOTSTRAP_FILES"
        )
        artifact_files = self._set_assignment(
            artifact_source, "RECOVERY_BOOTSTRAP_FILES"
        )
        workflow_files = self._workflow_recovery_set(auto_deploy_source)
        self.assertEqual(canonical, classifier_files)
        self.assertEqual(canonical, artifact_files)
        self.assertEqual(canonical, workflow_files)
        for representation in (
            canonical,
            classifier_files,
            artifact_files,
            workflow_files,
        ):
            self.assertIn(
                "platform/tools/platform_recovery_bootstrap.py", representation
            )


if __name__ == "__main__":
    unittest.main()
