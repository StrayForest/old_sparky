from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import os
import pwd
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from urllib.parse import urlsplit
from unittest import mock
import uuid
import zipfile

from tests import platform_chromium_sandbox_fixture as chromium_sandbox_fixture
from tests import platform_test_lock_support as lock_support
from tools import platform_release_transaction
from tools import platform_release_systemd_state
from tools import platform_validate_release_artifact
from tools import platform_validate_wheelhouse
from tools import platform_verify_venv_reuse


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "platform" / "tools" / "platform_release_install.sh"
ROLLBACK_SCRIPT = REPO_ROOT / "platform" / "tools" / "platform_release_rollback.sh"
TRANSACTION_TOOL = REPO_ROOT / "platform" / "tools" / "platform_release_transaction.py"
RUNTIME_RESTORE_SCRIPT = REPO_ROOT / "platform" / "tools" / "platform_release_restore_runtime.sh"
RECOVERY_SHIM_SCRIPT = REPO_ROOT / "platform" / "tools" / "platform_release_recovery_shim.sh"
TRANSACTION_STATE_NAME = ".release-operation.json"
BUILT_AT = "20260811T120000Z"
RELEASE_HELPER_NAMES = (
    "platform_install_systemd_units.sh",
    "platform_install_nginx.py",
    "platform_deploy_smoke.py",
    "platform_live_qa_runtime_install.py",
    "platform_install_logging.sh",
    "platform_prepare_service_user.sh",
    "platform_render_service_envs.py",
    "platform_deploy_smoke_impl.py",
    "platform_safe_env_exec.py",
    "platform_release_restore_runtime.sh",
    "platform_release_systemd_state.py",
    "platform_release_transaction.py",
    "platform_release_lock.sh",
)


class PlatformReleaseVenvRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertEqual(
            tuple(f"tools/{name}" for name in RELEASE_HELPER_NAMES),
            platform_release_systemd_state.HELPER_RELATIVE_PATHS,
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self._release_lock = None
        try:
            self._release_lock = lock_support.create_test_lock("venv-rollback")
            self.release_lock_path = self._release_lock.path
            self.tools_dir = self.root / "tools"
            shutil.copytree(REPO_ROOT / "platform" / "tools", self.tools_dir)
            self.install_test_lock_helper(self.tools_dir / "platform_release_lock.sh")
            self.app_dir = self.root / "platform-app"
            self.releases_dir = self.app_dir / "releases"
            self.shared_dir = self.app_dir / "shared"
            self.app_dir.mkdir(mode=0o755)
            self.app_dir.chmod(0o755)
            self.releases_dir.mkdir(mode=0o755)
            self.releases_dir.chmod(0o755)
            self.shared_dir.mkdir(mode=0o755)
            self.shared_dir.chmod(0o755)
            (self.shared_dir / ".env.platform").write_text("PLATFORM_TESTING=1\n")
            (self.shared_dir / ".env.platform").chmod(0o600)
            self.fake_systemctl = self.root / "systemctl"
            self.fake_systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "command=\"${1:-}\"\n"
                "unit=\"${2:-}\"\n"
                "status=64\n"
                "output=\"\"\n"
                "case \"$command\" in\n"
                "  is-active) status=0; output=active ;;\n"
                "  is-enabled) status=0; output=enabled ;;\n"
                "  enable|disable|start|stop|restart|daemon-reload|reload) status=0 ;;\n"
                "  *) status=64 ;;\n"
                "esac\n"
                "if [[ -n \"${unit:-}\" ]]; then\n"
                "  case \"$unit\" in\n"
                "    deadlock-api.service|deadlock-worker.service|deadlock-web.service|"
                "deadlock-maintenance.service|deadlock-maintenance.timer|"
                "deadlock-logrotate.service|deadlock-logrotate.timer|"
                "deadlock-offsite-backup.service|deadlock-offsite-backup.timer|"
                "deadlock-cloudflare-ips.service|deadlock-cloudflare-ips.timer|"
                "deadlock-health-monitor.service|deadlock-health-monitor.timer) ;;\n"
                "    nginx.service)\n"
                "      if [[ \"$command\" == reload ]]; then status=0; else status=65; fi\n"
                "      output=\"\"\n"
                "      ;;\n"
                "    *) status=65 ;;\n"
                "  esac\n"
                "fi\n"
                "if [[ -n \"${PLATFORM_TEST_SYSTEMCTL_TRACE:-}\" ]]; then\n"
                "  umask 077\n"
                "  printf '%s\\t%s\\t%s\\t%s\\n' \"$command\" \"$unit\" \"$status\" \"$output\" >> \"$PLATFORM_TEST_SYSTEMCTL_TRACE\"\n"
                "fi\n"
                "if [[ -n \"$output\" ]]; then printf '%s\\n' \"$output\"; fi\n"
                "exit \"$status\"\n"
            )
            self.fake_systemctl.chmod(0o755)
        except BaseException:
            if self._release_lock is not None:
                self._release_lock.cleanup()
            self.temp_dir.cleanup()
            raise

    def tearDown(self) -> None:
        try:
            if self._release_lock is not None:
                self._release_lock.cleanup()
        finally:
            self.temp_dir.cleanup()

    def test_fresh_offline_venv_is_retained_for_release_rollback(self) -> None:
        previous_release = self.add_installed_release("previous-release")
        # Rollback reads the restored target's historical release identity
        # before it installs the recovery shim.  The old release does not need
        # the new purge API, but it does need its authentic manifest shape.
        self.write_release_manifest(previous_release, "a" * 40)
        self.install_restore_nginx_helper(previous_release)
        protected_release = self.add_installed_release("protected-release")
        (self.app_dir / "current").symlink_to(previous_release)
        (self.app_dir / "previous").symlink_to(protected_release)
        self.add_fake_shared_venv(marker="old")
        artifact = self.build_artifact("new-release", pip_result="new")
        hostile_cwd = self.root / "hostile-cwd"
        hostile_cwd.mkdir()
        hostile_marker = hostile_cwd / "imported-hostile-module"
        (hostile_cwd / "venv.py").write_text(
            f"from pathlib import Path\nPath({str(hostile_marker)!r}).write_text('venv')\n"
            "raise SystemExit(97)\n"
        )
        hostile_pip = hostile_cwd / "pip"
        hostile_pip.mkdir()
        (hostile_pip / "__init__.py").write_text("")
        (hostile_pip / "__main__.py").write_text(
            f"from pathlib import Path\nPath({str(hostile_marker)!r}).write_text('pip')\n"
            "raise SystemExit(98)\n"
        )

        self.run_script(
            INSTALL_SCRIPT,
            str(artifact),
            str(self.app_dir),
            cwd=hostile_cwd,
        )

        current_release = (self.app_dir / "current").resolve()
        snapshot_dir = current_release / ".rollback" / "shared-venv-before-install"
        self.assertEqual(current_release.name, f"new-release-{BUILT_AT}")
        self.assertEqual((self.app_dir / "previous").resolve(), previous_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "new\n"
        )
        installed_venv = self.shared_dir / "venv"
        relocation_script = installed_venv / "bin" / "relocation_probe.py"
        self.assertIn(str(installed_venv), relocation_script.read_text().splitlines()[0])
        relocation_cache = installed_venv / "bin" / "__pycache__" / "relocation_probe.cpython-312.pyc"
        self.assertTrue(relocation_cache.is_file())
        relocation_bytes = relocation_cache.read_bytes()
        import importlib.util
        import marshal

        self.assertEqual(relocation_bytes[:4], importlib.util.MAGIC_NUMBER)
        self.assertEqual(int.from_bytes(relocation_bytes[8:12], "little"), int(relocation_script.stat().st_mtime))
        self.assertEqual(int.from_bytes(relocation_bytes[12:16], "little"), relocation_script.stat().st_size)
        self.assertEqual(stat.S_IMODE(relocation_cache.stat().st_mode), 0o644)
        self.assertEqual(
            marshal.loads(relocation_bytes[16:]),
            compile(relocation_script.read_bytes(), str(relocation_script), "exec", dont_inherit=True),
        )
        unrelated_cache = installed_venv / "bin" / "__pycache__" / "unrelated_probe.cpython-312.pyc"
        self.assertTrue(unrelated_cache.is_file())
        unrelated_script = installed_venv / "bin" / "unrelated_probe.py"
        unrelated_bytes = unrelated_cache.read_bytes()
        self.assertEqual(int.from_bytes(unrelated_bytes[12:16], "little"), unrelated_script.stat().st_size)
        self.assertEqual(stat.S_IMODE((installed_venv / "bin").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((installed_venv / "lib").stat().st_mode), 0o755)
        site_packages = next(installed_venv.glob("lib/python*/site-packages"))
        permission_probe = site_packages / "release_permission_probe.py"
        self.assertEqual(stat.S_IMODE(site_packages.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(permission_probe.stat().st_mode), 0o644)
        self.assertEqual(
            stat.S_IMODE((self.shared_dir / ".env.platform").stat().st_mode),
            0o600,
        )
        self.assertFalse(any(self.shared_dir.glob(".freeze-check-*")))
        self.assertEqual(stat.S_IMODE(self.app_dir.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.shared_dir.stat().st_mode), 0o755)
        # The same promoted interpreter and installed package must be usable
        # by an identity other than root, as the API and worker services do.
        import_command = [
            str(installed_venv / "bin" / "python"),
            "-I",
            "-c",
            "import pip, release_permission_probe; "
            "print(pip.__version__, release_permission_probe.VALUE)",
        ]
        if os.geteuid() == 0:
            runuser = shutil.which("runuser")
            self.assertIsNotNone(runuser)
            self.assertNotEqual(pwd.getpwnam("nobody").pw_uid, 0)
            self.root.chmod(0o755)
            nonroot_import = subprocess.run(
                [runuser, "-u", "nobody", "--", *import_command],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        else:
            nonroot_import = subprocess.run(
                import_command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(nonroot_import.returncode, 0, nonroot_import.stderr)
        self.assertEqual(nonroot_import.stdout.strip(), "26.1.2 readable dependency")
        console = subprocess.run(
            [str(self.shared_dir / "venv" / "bin" / "fake-pip-cli")],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        self.assertEqual(console.stdout, "relocated console script\n")
        self.assertFalse(hostile_marker.exists())
        self.assertEqual((snapshot_dir / "deps-version").read_text(), "old\n")
        self.assertEqual(
            (current_release / ".rollback" / "previous-release").read_text(),
            f"{previous_release}\n",
        )

        purge_v1_keys, preserve_v2_keys = self.prepare_profile_access_purge_runtime(
            current_release, self.shared_dir / "venv"
        )
        self.run_script(ROLLBACK_SCRIPT, "--app-dir", str(self.app_dir), "--no-restart")
        self.assert_profile_access_cache_purged(purge_v1_keys, preserve_v2_keys)

        self.assertEqual((self.app_dir / "current").resolve(), previous_release)
        self.assertEqual((self.app_dir / "previous").resolve(), current_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "old\n"
        )

    def test_failed_offline_dependency_install_keeps_active_runtime_and_pointers(
        self,
    ) -> None:
        current_release = self.add_installed_release("current-release")
        protected_release = self.add_installed_release("protected-release")
        (self.app_dir / "current").symlink_to(current_release)
        (self.app_dir / "previous").symlink_to(protected_release)
        self.add_fake_shared_venv(marker="old")
        artifact = self.build_artifact("broken-release", pip_result="fail")

        result = self.run_script(
            INSTALL_SCRIPT, str(artifact), str(self.app_dir), check=False
        )

        self.assertEqual(result.returncode, 42)
        self.assertEqual((self.app_dir / "current").resolve(), current_release)
        self.assertEqual((self.app_dir / "previous").resolve(), protected_release)
        self.assertFalse((self.releases_dir / f"broken-release-{BUILT_AT}").exists())
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "old\n"
        )

    def test_relocation_cache_cleanup_is_source_bound_and_fail_closed(self) -> None:
        def install_case(case: str, *, expect_success: bool) -> tuple[Path, Path, subprocess.CompletedProcess[str]]:
            app = self.root / f"relocation-{case}" / "platform"
            shared = app / "shared"
            releases = app / "releases"
            releases.mkdir(parents=True)
            shared.mkdir()
            venv = shared / "venv"
            self.add_fake_venv(venv, marker="old")
            artifact = self.build_artifact(f"cache-{case}", pip_result=case)
            result = self.run_script(
                INSTALL_SCRIPT, "--stage-only", str(artifact), str(app), check=False,
                relocation_case=case,
            )
            if expect_success:
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse((app / "current").is_symlink())
                self.assertEqual((venv / "deps-version").read_text(), "old\n")
            return app, shared, result

        for case in (
            "hashed-cache-row",
            "duplicate-cache-row",
            "noncanonical-cache-row",
            "bad-cache-header",
            "bad-cache-code",
        ):
            with self.subTest(case=case):
                install_case(case, expect_success=False)

        _, shared, _ = install_case("no-cache", expect_success=True)
        installed = shared / "venv"
        self.assertTrue((installed / "bin" / "relocation_probe.py").is_file())
        self.assertFalse(
            (installed / "bin" / "__pycache__" / "relocation_probe.cpython-312.pyc").exists()
        )
        self.assertFalse(any(self.shared_dir.glob(".venv-install-*")))

    def test_migration_uncertain_state_refuses_automatic_recovery(self) -> None:
        current_release = self.add_installed_release("current-release")
        protected_release = self.add_installed_release("protected-release")
        (self.app_dir / "current").symlink_to(current_release)
        (self.app_dir / "previous").symlink_to(protected_release)
        self.add_fake_shared_venv(marker="old")
        artifact = self.build_artifact("migration-uncertain", pip_result="new")

        self.run_script(
            INSTALL_SCRIPT,
            "--stage-only",
            str(artifact),
            str(self.app_dir),
        )
        state = self.shared_dir / TRANSACTION_STATE_NAME
        self.assertEqual(json.loads(state.read_text())["phase"], "staged")
        self.run_script(
            TRANSACTION_TOOL,
            "phase",
            "--state",
            str(state),
            "--expected",
            "staged",
            "--phase",
            "migration-pending",
        )

        # A pre-upgrade receipt is not guessed into the new service-state
        # schema. Recovery must fail closed until an operator has a compatible
        # receipt, rather than risking a restart with an unknown pre-state.
        legacy_payload = json.loads(state.read_text())
        legacy_payload["version"] = 1
        state.write_text(json.dumps(legacy_payload) + "\n")
        result = self.run_script(
            TRANSACTION_TOOL,
            "status",
            "--state",
            str(state),
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(state.exists())
        legacy_payload["version"] = 2
        state.write_text(json.dumps(legacy_payload) + "\n")

        result = self.run_script(
            TRANSACTION_TOOL,
            "recover",
            "--state",
            str(state),
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("migration outcome is not safely reversible", result.stderr)
        self.assertTrue(state.exists())

        result = self.run_script(
            TRANSACTION_TOOL,
            "authorize-recovery",
            "--state",
            str(state),
            "--confirm",
            "WRONG_CONFIRMATION",
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(state.exists())
        self.run_script(
            TRANSACTION_TOOL,
            "authorize-recovery",
            "--state",
            str(state),
            "--confirm",
            "MIGRATION_NOT_REVERSED",
        )
        self.assertEqual(json.loads(state.read_text())["phase"], "recovery-authorized")
        self.run_script(TRANSACTION_TOOL, "recover", "--state", str(state))
        self.assertEqual((self.app_dir / "current").resolve(), current_release)
        self.assertEqual((self.app_dir / "previous").resolve(), protected_release)
        self.assertEqual((self.shared_dir / "venv" / "deps-version").read_text(), "old\n")
        self.assertFalse(state.exists())

    def test_skip_python_deps_refuses_an_existing_venv_that_does_not_match_freeze(
        self,
    ) -> None:
        current_release = self.add_installed_release("current-release")
        protected_release = self.add_installed_release("protected-release")
        (self.app_dir / "current").symlink_to(current_release)
        (self.app_dir / "previous").symlink_to(protected_release)
        self.add_fake_shared_venv(marker="old")
        artifact = self.build_artifact("skip-mismatch", pip_result="new")

        result = self.run_script(
            INSTALL_SCRIPT,
            "--skip-python-deps",
            str(artifact),
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app_dir / "current").resolve(), current_release)
        self.assertEqual((self.app_dir / "previous").resolve(), protected_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "old\n"
        )
        self.assertFalse((self.releases_dir / f"skip-mismatch-{BUILT_AT}").exists())

    def test_venv_reuse_accepts_only_the_exact_active_quiesce_receipt(self) -> None:
        current = self.add_installed_release("reuse-current")
        candidate = self.releases_dir / "reuse-candidate"
        previous = self.add_installed_release("reuse-previous")
        origin = self.add_installed_release("reuse-origin")
        (self.app_dir / "current").symlink_to(current)

        def add_proof_release(release: Path) -> None:
            release.mkdir(exist_ok=True)
            metadata = {
                "release_slug": release.name,
                "source_git_commit": "a" * 40,
            }
            release_json = release / "RELEASE.json"
            release_json.write_text(json.dumps(metadata) + "\n")
            release_json.chmod(0o444)
            for relative, content in (
                ("requirements-platform.txt", b"pip==26.1.2\n"),
                ("requirements-platform.lock.txt", b"pip==26.1.2 --hash=sha256:fixture\n"),
                ("requirements-platform.freeze.txt", b"pip==26.1.2\n"),
            ):
                path = release / relative
                path.write_bytes(content)
                path.chmod(0o444)
            (release / "wheelhouse").mkdir()
            (release / "wheelhouse" / "WHEELHOUSE.sha256").write_text("fixture manifest\n")
            (release / "wheelhouse" / "pip-26.1.2-py3-none-any.whl").write_bytes(b"fixture wheel\n")

        def add_rollback(
            release: Path, transition_value: str, previous_release: Path | None,
        ) -> Path:
            rollback_dir = release / ".rollback"
            rollback_dir.mkdir(mode=0o700)
            transition_file = rollback_dir / "venv-transition"
            transition_file.write_text(f"{transition_value}\n")
            transition_file.chmod(0o600)
            if transition_value == "snapshot":
                (rollback_dir / "shared-venv-before-install").mkdir()
            if previous_release is not None:
                previous_file = rollback_dir / "previous-release"
                previous_file.write_text(f"{previous_release}\n")
                previous_file.chmod(0o600)
            if transition_value == "unchanged":
                freeze = release / "requirements-platform.freeze.txt"
                freeze_record = rollback_dir / "shared-freeze.sha256"
                freeze_record.write_text(hashlib.sha256(freeze.read_bytes()).hexdigest() + "\n")
                freeze_record.chmod(0o600)
            return rollback_dir

        add_proof_release(current)
        add_proof_release(previous)
        add_proof_release(origin)
        add_rollback(current, "unchanged", previous)
        add_rollback(previous, "unchanged", origin)
        add_rollback(origin, "snapshot", None)
        (self.shared_dir / "venv").mkdir()
        venv_python = self.shared_dir / "venv" / "bin" / "python"
        venv_python.parent.mkdir()
        self.write_executable(
            venv_python,
            'if [ "$*" = "-B -I -m pip check" ]; then exit 0; fi\n'
            'if [ "$*" = "-B -I -m pip freeze --all" ]; then '
            "printf '%s\\n' 'pip==26.1.2'; exit 0; fi\n"
            "exit 1\n",
        )
        activation = venv_python.parent / "activate"
        activation.write_text(
            f"VIRTUAL_ENV_PROMPT='(.venv-install-{origin.name}.ABC123) '\n",
            encoding="utf-8",
        )
        quiesce_state = self.shared_dir / ".release-quiesce.json"
        platform_release_transaction.prepare_quiesce(
            quiesce_state,
            app_dir=self.app_dir,
            candidate_release=candidate,
            service_states=[
                "deadlock-api=active", "deadlock-worker=active", "deadlock-web=active"
            ],
            timer_active_before="inactive",
            service_enabled=[
                "deadlock-api=enabled", "deadlock-worker=enabled", "deadlock-web=enabled"
            ],
            timer_enabled_before="enabled",
            candidate_may_exist=False,
        )
        add_proof_release(candidate)
        activation_scripts = platform_verify_venv_reuse._expected_activation_scripts(
            self.shared_dir / "venv", origin.name,
        )
        activation_sha = platform_verify_venv_reuse._activation_scripts_digest(
            activation_scripts,
        )
        with mock.patch.object(platform_verify_venv_reuse, "_runtime") as runtime_check, \
                mock.patch.object(
                    platform_verify_venv_reuse, "_venv_integrity",
                    return_value=activation_sha,
                ) as integrity_check:
            proof = platform_verify_venv_reuse.prove(
                self.app_dir, current, candidate, self.shared_dir / "venv",
                Path("/usr/bin/python3.12"), self.shared_dir / ".release-operation.json",
                quiesce_state, "",
            )
            runtime_check.assert_called_once()
            integrity_check.assert_called_once()
            integrity_check.assert_called_with(
                self.shared_dir / "venv", current / "wheelhouse", origin.name,
            )
            self.assertEqual(proof["origin_release_slug"], origin.name)
            self.assertEqual(proof["origin_source_sha"], "a" * 40)
            self.assertEqual(proof["activation_sha256"], activation_sha)

            # A missing/cyclic legacy chain is never inferred from the prompt.
            middle_previous = previous / ".rollback" / "previous-release"
            middle_previous.write_text(f"{current}\n")
            middle_previous.chmod(0o600)
            with self.assertRaises(platform_verify_venv_reuse.ReuseRefused):
                platform_verify_venv_reuse._derive_venv_origin(
                    current, self.app_dir, self.shared_dir / "venv",
                )
            middle_previous.write_text(f"{origin}\n")
            middle_previous.chmod(0o600)
            origin_snapshot = origin / ".rollback" / "shared-venv-before-install"
            origin_snapshot.rmdir()
            with self.assertRaises(platform_verify_venv_reuse.ReuseRefused):
                platform_verify_venv_reuse._derive_venv_origin(
                    current, self.app_dir, self.shared_dir / "venv",
                )
            origin_snapshot.mkdir()

            # Exercise the same O_EXCL receipt writer used by the installer.
            installer = INSTALL_SCRIPT.read_text(encoding="utf-8")
            writer_start = installer.index("persist_venv_origin_receipt() {")
            writer_end = installer.index("\nLOCK_HELPER=", writer_start)
            writer_harness = installer[writer_start:writer_end] + \
                '\npersist_venv_origin_receipt "$VENV_REUSE_PROOF"\n'
            candidate_rollback = add_rollback(candidate, "unchanged", current)
            saved = subprocess.run(
                ["/bin/bash", "-c", writer_harness],
                env={
                    "PATH": "/usr/bin:/bin", "HOME": str(self.root),
                    "VENV_REUSE_PROOF": json.dumps(proof, sort_keys=True, separators=(",", ":")),
                    "RELEASE_DIR": str(candidate),
                    "SHARED_VENV_DIR": str(self.shared_dir / "venv"),
                    "VENV_ROLLBACK_DIR": str(candidate_rollback),
                },
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(saved.returncode, 0, saved.stderr)
            origin_receipt = candidate_rollback / "venv-origin.json"
            self.assertEqual(origin_receipt.stat().st_uid, 0)
            self.assertEqual(origin_receipt.stat().st_gid, 0)
            self.assertEqual(origin_receipt.stat().st_nlink, 1)
            self.assertEqual(stat.S_IMODE(origin_receipt.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(origin_receipt.read_text()),
                proof,
            )
            # The anchor survives pruning intermediate releases; no unbounded
            # historical release retention is needed for the next reuse.
            shutil.rmtree(previous)
            shutil.rmtree(origin)
            anchored = platform_verify_venv_reuse._derive_venv_origin(
                candidate, self.app_dir, self.shared_dir / "venv",
            )
            self.assertEqual(anchored[0:2], (origin.name, "a" * 40))
            self.assertIsNotNone(anchored[3])
            self.assertEqual(
                platform_verify_venv_reuse._activation_scripts_digest(
                    platform_verify_venv_reuse._expected_activation_scripts(
                        self.shared_dir / "venv", anchored[0],
                    ),
                ),
                activation_sha,
            )
            bad_anchor = dict(proof)
            bad_anchor["venv_ino"] = int(proof["venv_ino"]) + 1
            origin_receipt.write_text(
                json.dumps(bad_anchor, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            origin_receipt.chmod(0o600)
            with self.assertRaises(platform_verify_venv_reuse.ReuseRefused):
                platform_verify_venv_reuse._derive_venv_origin(
                    candidate, self.app_dir, self.shared_dir / "venv",
                )
            origin_receipt.write_text(
                json.dumps(proof, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            origin_receipt.chmod(0o600)
            # A second strict reuse validates the carried anchor after its
            # immediate predecessor's origin chain has been pruned.
            (self.app_dir / "current").unlink()
            (self.app_dir / "current").symlink_to(candidate)
            (self.app_dir / "previous").symlink_to(current)
            next_candidate = self.releases_dir / "reuse-next"
            add_proof_release(next_candidate)
            quiesce_state.unlink()
            platform_release_transaction.prepare_quiesce(
                quiesce_state,
                app_dir=self.app_dir,
                candidate_release=next_candidate,
                service_states=[
                    "deadlock-api=active", "deadlock-worker=active", "deadlock-web=active"
                ],
                timer_active_before="inactive",
                service_enabled=[
                    "deadlock-api=enabled", "deadlock-worker=enabled", "deadlock-web=enabled"
                ],
                timer_enabled_before="enabled",
                candidate_may_exist=True,
            )
            with mock.patch.object(platform_verify_venv_reuse, "_runtime") as runtime_check, \
                    mock.patch.object(
                        platform_verify_venv_reuse, "_venv_integrity",
                        return_value=activation_sha,
                    ) as integrity_check:
                second_proof = platform_verify_venv_reuse.prove(
                    self.app_dir, candidate, next_candidate, self.shared_dir / "venv",
                    Path("/usr/bin/python3.12"),
                    self.shared_dir / ".release-operation.json", quiesce_state,
                    str(current),
                )
                runtime_check.assert_called_once()
                integrity_check.assert_called_with(
                    self.shared_dir / "venv", candidate / "wheelhouse", origin.name,
                )
                self.assertEqual(second_proof["origin_release_slug"], origin.name)
                self.assertEqual(second_proof["activation_sha256"], activation_sha)
                bad_activation = dict(proof)
                bad_activation["activation_sha256"] = "0" * 64
                origin_receipt.write_text(
                    json.dumps(bad_activation, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                origin_receipt.chmod(0o600)
                with mock.patch.object(platform_verify_venv_reuse, "_runtime"), \
                        mock.patch.object(
                            platform_verify_venv_reuse, "_venv_integrity",
                            return_value=activation_sha,
                        ):
                    with self.assertRaises(platform_verify_venv_reuse.ReuseRefused):
                        platform_verify_venv_reuse.prove(
                            self.app_dir, candidate, next_candidate,
                            self.shared_dir / "venv", Path("/usr/bin/python3.12"),
                            self.shared_dir / ".release-operation.json", quiesce_state,
                            str(current),
                        )
            origin_receipt.write_text(
                json.dumps(proof, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            origin_receipt.chmod(0o600)
            with self.assertRaises(platform_verify_venv_reuse.ReuseRefused):
                platform_verify_venv_reuse._expected_activation_scripts(
                    self.shared_dir / "venv", "wrong-origin",
                )
            # Existing receipts are never overwritten or accepted from an
            # artifact-provided preexisting path.
            overwritten = subprocess.run(
                ["/bin/bash", "-c", writer_harness],
                env={
                    "PATH": "/usr/bin:/bin", "HOME": str(self.root),
                    "VENV_REUSE_PROOF": json.dumps(proof, sort_keys=True, separators=(",", ":")),
                    "RELEASE_DIR": str(candidate),
                    "SHARED_VENV_DIR": str(self.shared_dir / "venv"),
                    "VENV_ROLLBACK_DIR": str(candidate_rollback),
                },
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertNotEqual(overwritten.returncode, 0)
            foreign_candidate = self.releases_dir / "foreign-candidate"
            foreign_candidate.mkdir()
            foreign_metadata = foreign_candidate / "RELEASE.json"
            foreign_metadata.write_text(json.dumps({
                "release_slug": foreign_candidate.name,
                "source_git_commit": "b" * 40,
            }) + "\n")
            foreign_metadata.chmod(0o444)
            with self.assertRaises(platform_verify_venv_reuse.ReuseRefused):
                platform_verify_venv_reuse.prove(
                    self.app_dir, current, foreign_candidate, self.shared_dir / "venv",
                    Path("/usr/bin/python3.12"), self.shared_dir / ".release-operation.json",
                    quiesce_state, "",
                )

    def test_venv_reuse_compile_proof_does_not_inherit_future_flags(self) -> None:
        import __future__

        code = platform_verify_venv_reuse._compile_source(b"annotation: object\n", "proof.py")
        self.assertEqual(code.co_flags & __future__.annotations.compiler_flag, 0)

    def test_venv_reuse_rejects_source_tamper_even_with_rewritten_record(self) -> None:
        venv = self.root / "proof-venv"
        site = venv / "lib" / "python3.12" / "site-packages"
        package_dir = site / "proof_pkg"
        dist_info = site / "proof_pkg-1.0.dist-info"
        package_dir.mkdir(parents=True)
        dist_info.mkdir()
        (venv / "bin").mkdir(parents=True)
        wheelhouse = self.root / "proof-wheelhouse"
        wheelhouse.mkdir()
        payloads = {
            "proof_pkg/__init__.py": b"VALUE = 'trusted'\n",
            "proof_pkg-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: proof-pkg\nVersion: 1.0\n\n",
            "proof_pkg-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
            "proof_hook.pth": b"# trusted inert fixture\n",
        }

        def record_bytes(files: dict[str, bytes], extra_rows: tuple[tuple[str, str, str], ...] = ()) -> bytes:
            import base64
            import csv
            import io

            output = io.StringIO(newline="")
            writer = csv.writer(output, lineterminator="\n")
            for name, content in sorted(files.items()):
                digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
                writer.writerow((name, f"sha256={digest}", str(len(content))))
            for row in extra_rows:
                writer.writerow(row)
            writer.writerow(("proof_pkg-1.0.dist-info/RECORD", "", ""))
            return output.getvalue().encode()

        data_script_name = "proof_pkg-1.0.data/scripts/proof.py"
        data_script_source = b"#!python\nprint('trusted')\n"
        wheel_payloads = {**payloads, data_script_name: data_script_source}
        wheel_files = {
            **wheel_payloads,
            "proof_pkg-1.0.dist-info/RECORD": record_bytes(wheel_payloads),
        }
        wheel = wheelhouse / "proof_pkg-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, content in wheel_files.items():
                archive.writestr(name, content)
        for name, content in payloads.items():
            target = site / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o644)
        transformed_script = b"#!" + str(venv / "bin/python").encode() + b"\nprint('trusted')\n"
        script_target = venv / "bin" / "proof.py"
        script_target.write_bytes(transformed_script)
        script_target.chmod(0o755)
        intermediate_script = b"#!/tmp/.venv-install-proof.XYZ123/bin/python\nprint('trusted')\n"
        installed_extra = (
            "../../../bin/proof.py",
            "sha256=" + __import__("base64").urlsafe_b64encode(
                hashlib.sha256(intermediate_script).digest()
            ).rstrip(b"=").decode(),
            str(len(intermediate_script)),
        )
        (dist_info / "RECORD").write_bytes(record_bytes(payloads, (installed_extra,)))
        (venv / "bin").chmod(0o755)
        temporary_name = ".venv-install-proof-current.A1b2C3"
        trusted_python = Path("/usr/bin/python3.12")
        self.assertTrue(trusted_python.is_file())
        trusted_fixture_script = """
import importlib.util
import importlib.machinery
import marshal
from pathlib import Path
import stat
import sys

module_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("platform_verify_venv_reuse", module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
operation = sys.argv[2]
if operation == "render":
    venv = Path(sys.argv[3])
    activation = module._render_activation_scripts(venv, sys.argv[4])
    for path, (content, mode) in activation.items():
        path.write_bytes(content)
        path.chmod(mode)
elif operation in {"verify", "verify-no-pip"}:
    if operation == "verify-no-pip":
        module._expected_console_scripts = lambda *_args: (_ for _ in ()).throw(
            AssertionError("unverified pip must not execute")
        )
    else:
        module._expected_console_scripts = lambda *_args: {}
    try:
        module._venv_integrity(Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5])
    except module.ReuseRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(42)
elif operation in {"timestamp-cache", "hash-cache"}:
    source = Path(sys.argv[3])
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir(parents=True, exist_ok=True)
    source_bytes = source.read_bytes()
    code = marshal.dumps(module._compile_source(source_bytes, str(source)))
    if operation == "timestamp-cache":
        header = (importlib.util.MAGIC_NUMBER + (0).to_bytes(4, "little")
                  + int(source.stat().st_mtime).to_bytes(4, "little")
                  + len(source_bytes).to_bytes(4, "little"))
    else:
        header = (importlib.util.MAGIC_NUMBER + (3).to_bytes(4, "little")
                  + importlib.util.source_hash(source_bytes))
    cache.write_bytes(header + code)
    cache.chmod(0o644)
else:
    raise SystemExit(f"unknown fixture operation: {operation}")
"""
        verifier_path = Path(platform_verify_venv_reuse.__file__)

        def run_trusted_fixture(operation: str, *arguments: object) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    str(trusted_python),
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    trusted_fixture_script,
                    str(verifier_path),
                    operation,
                    *(str(argument) for argument in arguments),
                ],
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": "/nonexistent",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        def assert_reuse_refused(*, no_pip: bool = False) -> None:
            operation = "verify-no-pip" if no_pip else "verify"
            result = run_trusted_fixture(operation, venv, wheelhouse, "proof-current")
            self.assertEqual(result.returncode, 42, result.stderr)

        rendered = run_trusted_fixture("render", venv, temporary_name)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        for name in ("python", "python3", "python3.12"):
            (venv / "bin" / name).symlink_to("/usr/bin/python3.12")
        verified = run_trusted_fixture("verify", venv, wheelhouse, "proof-current")
        self.assertEqual(verified.returncode, 0, verified.stderr)

        import importlib.util

        cache = Path(importlib.util.cache_from_source(str(script_target)))
        cache_source = script_target.read_bytes()
        timestamp_cache = run_trusted_fixture("timestamp-cache", script_target)
        self.assertEqual(timestamp_cache.returncode, 0, timestamp_cache.stderr)
        verified = run_trusted_fixture("verify", venv, wheelhouse, "proof-current")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        cache_bytes = cache.read_bytes()
        hash_cache = run_trusted_fixture("hash-cache", script_target)
        self.assertEqual(hash_cache.returncode, 0, hash_cache.stderr)
        verified = run_trusted_fixture("verify", venv, wheelhouse, "proof-current")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        cache.write_bytes(cache_bytes)
        cache.write_bytes(
            cache_bytes[:12]
            + (len(cache_source) + 1).to_bytes(4, "little")
            + cache_bytes[16:]
        )
        assert_reuse_refused()
        cache.unlink()
        cache.parent.rmdir()

        activation_file = venv / "bin" / "activate"
        activation_file.write_bytes(activation_file.read_bytes() + b"# changed\n")
        assert_reuse_refused()
        restored_activation = run_trusted_fixture("render", venv, temporary_name)
        self.assertEqual(restored_activation.returncode, 0, restored_activation.stderr)

        rogue_script = venv / "bin" / "unrecorded-script"
        rogue_script.write_text("#!/bin/sh\nexit 0\n")
        rogue_script.chmod(0o755)
        assert_reuse_refused()
        rogue_script.unlink()

        package_dir.chmod(0o700)
        assert_reuse_refused()
        package_dir.chmod(0o755)
        source = package_dir / "__init__.py"
        source.chmod(0o600)
        assert_reuse_refused()
        source.chmod(0o644)
        script_target.chmod(0o700)
        assert_reuse_refused()
        script_target.chmod(0o755)

        alternate_spelling = (
            "lib/python3.12/site-packages/../../../bin/proof.py",
            installed_extra[1],
            installed_extra[2],
        )
        (dist_info / "RECORD").write_bytes(record_bytes(payloads, (alternate_spelling,)))
        assert_reuse_refused()
        duplicate_rows = (installed_extra, alternate_spelling)
        (dist_info / "RECORD").write_bytes(record_bytes(payloads, duplicate_rows))
        assert_reuse_refused()
        (dist_info / "RECORD").write_bytes(record_bytes(payloads, (installed_extra,)))

        script_target.write_bytes(b"#!" + str(venv / "bin/python").encode() + b"\nprint('changed')\n")
        assert_reuse_refused()
        script_target.write_bytes(transformed_script)

        changed = b"VALUE = 'changed'\n"
        source.write_bytes(changed)
        record_path = dist_info / "RECORD"
        changed_files = {**payloads, "proof_pkg/__init__.py": changed}
        record_path.write_bytes(record_bytes(changed_files))
        assert_reuse_refused()

        source.write_bytes(payloads["proof_pkg/__init__.py"])
        marker = self.root / "pth-side-effect"
        malicious_pth = (
            "import pathlib; pathlib.Path(" + repr(str(marker)) + ").write_text('executed')\n"
        ).encode()
        (site / "proof_hook.pth").write_bytes(malicious_pth)
        changed_files = {**payloads, "proof_hook.pth": malicious_pth}
        record_path.write_bytes(record_bytes(changed_files))
        assert_reuse_refused(no_pip=True)
        self.assertFalse(marker.exists())

        (site / "proof_hook.pth").write_bytes(payloads["proof_hook.pth"])
        record_path.write_bytes(record_bytes(payloads))
        source.unlink()
        source.symlink_to(self.root / "trusted-source.py")
        (self.root / "trusted-source.py").write_bytes(payloads["proof_pkg/__init__.py"])
        assert_reuse_refused()

        source.unlink()
        source.write_bytes(payloads["proof_pkg/__init__.py"])
        source.chmod(0o4755)
        assert_reuse_refused()

    def test_skip_python_deps_publishes_receipt_and_default_rollback_preserves_venv(
        self,
    ) -> None:
        original_current = self.add_installed_release("current-release")
        self.write_release_manifest(original_current, "a" * 40)
        self.install_restore_nginx_helper(original_current)
        self.install_restore_runtime_reconcile_helper(original_current, self.app_dir)
        protected_release = self.add_installed_release("protected-release")
        (self.app_dir / "current").symlink_to(original_current)
        (self.app_dir / "previous").symlink_to(protected_release)
        self.add_fake_shared_venv(marker="unchanged", matching_freeze=True)
        shared_identity = (self.shared_dir / "venv").stat()
        artifact = self.build_artifact("skip-match", pip_result="unused")

        self.run_script(
            INSTALL_SCRIPT,
            "--skip-python-deps",
            str(artifact),
            str(self.app_dir),
        )

        candidate = (self.app_dir / "current").resolve()
        rollback_dir = candidate / ".rollback"
        freeze = candidate / "requirements-platform.freeze.txt"
        self.assertEqual((self.app_dir / "previous").resolve(), original_current)
        self.assertEqual(
            (rollback_dir / "previous-release").read_text(),
            f"{original_current}\n",
        )
        self.assertEqual((rollback_dir / "venv-transition").read_text(), "unchanged\n")
        self.assertEqual(
            (rollback_dir / "shared-freeze.sha256").read_text(),
            f"{hashlib.sha256(freeze.read_bytes()).hexdigest()}\n",
        )
        for receipt in (
            rollback_dir / "previous-release",
            rollback_dir / "venv-transition",
            rollback_dir / "shared-freeze.sha256",
        ):
            self.assertEqual(receipt.stat().st_uid, 0)
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        self.assertFalse((rollback_dir / "shared-venv-before-install").exists())

        purge_v1_keys, preserve_v2_keys = self.prepare_profile_access_purge_runtime(
            candidate, self.shared_dir / "venv"
        )
        self.install_restore_nginx_helper(candidate)
        self.refresh_unchanged_venv_freeze_receipt(
            candidate, self.shared_dir / "venv"
        )
        self.run_script(
            ROLLBACK_SCRIPT,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
        )
        self.assert_profile_access_cache_purged(purge_v1_keys, preserve_v2_keys)

        restored_identity = (self.shared_dir / "venv").stat()
        self.assertEqual((self.app_dir / "current").resolve(), original_current)
        self.assertEqual((self.app_dir / "previous").resolve(), candidate)
        self.assertEqual(
            (restored_identity.st_dev, restored_identity.st_ino),
            (shared_identity.st_dev, shared_identity.st_ino),
        )
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(),
            "unchanged\n",
        )

    def test_unchanged_venv_rollback_refuses_missing_or_tampered_receipt(self) -> None:
        cases = ("missing-transition", "invalid-transition", "invalid-digest", "drift")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                if index:
                    self.reset_app_tree()
                original_current = self.add_installed_release(f"current-{index}")
                protected_release = self.add_installed_release(f"protected-{index}")
                (self.app_dir / "current").symlink_to(original_current)
                (self.app_dir / "previous").symlink_to(protected_release)
                self.add_fake_shared_venv(marker="unchanged", matching_freeze=True)
                artifact = self.build_artifact(
                    f"skip-receipt-{index}", pip_result="unused"
                )
                self.run_script(
                    INSTALL_SCRIPT,
                    "--skip-python-deps",
                    str(artifact),
                    str(self.app_dir),
                )
                candidate = (self.app_dir / "current").resolve()
                rollback_dir = candidate / ".rollback"
                if case == "missing-transition":
                    (rollback_dir / "venv-transition").unlink()
                elif case == "invalid-transition":
                    (rollback_dir / "venv-transition").write_text("invalid\n")
                elif case == "invalid-digest":
                    (rollback_dir / "shared-freeze.sha256").write_text(f"{'0' * 64}\n")
                else:
                    self.write_executable(
                        self.shared_dir / "venv" / "bin" / "python",
                        'if [ "$*" = "-I -B -m pip freeze --all" ] || '
                        '[ "$*" = "-I -m pip freeze --all" ]; then\n'
                        "  printf '%s\\n' 'pip==0.0.0'\n"
                        "fi\n",
                    )

                result = self.run_script(
                    ROLLBACK_SCRIPT,
                    "--app-dir",
                    str(self.app_dir),
                    "--no-restart",
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.app_dir / "current").resolve(), candidate)
                self.assertEqual(
                    (self.app_dir / "previous").resolve(), original_current
                )
                self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_transaction_refuses_tampered_skip_receipt_before_state_publish(
        self,
    ) -> None:
        for index, field in enumerate(("transition", "freeze")):
            with self.subTest(field=field):
                if index:
                    self.reset_app_tree()
                current = self.add_installed_release(f"receipt-current-{index}")
                protected = self.add_installed_release(f"receipt-protected-{index}")
                candidate = self.add_installed_release(f"receipt-candidate-{index}")
                self.add_fake_shared_venv(marker="unchanged", matching_freeze=True)
                freeze = candidate / "requirements-platform.freeze.txt"
                freeze.write_text("pip==26.1.2\n")
                freeze.chmod(0o444)
                rollback_dir = candidate / ".rollback"
                rollback_dir.mkdir(mode=0o700)
                records = {
                    "previous-release": f"{current}\n",
                    "venv-transition": "unchanged\n",
                    "shared-freeze.sha256": (
                        f"{hashlib.sha256(freeze.read_bytes()).hexdigest()}\n"
                    ),
                }
                if field == "transition":
                    records["venv-transition"] = "snapshot\n"
                else:
                    records["shared-freeze.sha256"] = f"{'0' * 64}\n"
                for name, value in records.items():
                    record = rollback_dir / name
                    record.write_text(value)
                    record.chmod(0o600)

                result = self.run_script(
                    TRANSACTION_TOOL,
                    "create",
                    "--state",
                    str(self.shared_dir / TRANSACTION_STATE_NAME),
                    "--operation",
                    "install",
                    "--app-dir",
                    str(self.app_dir),
                    "--current-before",
                    str(current),
                    "--previous-before",
                    str(protected),
                    "--candidate-release",
                    str(candidate),
                    "--shared-venv",
                    str(self.shared_dir / "venv"),
                    "--peer",
                    str(self.shared_dir / f".venv-install-{candidate.name}.none"),
                    "--snapshot",
                    str(rollback_dir / "shared-venv-before-install"),
                    "--transition",
                    "none",
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_failure_between_pointer_updates_restores_exact_preinstall_state(
        self,
    ) -> None:
        current_release = self.add_installed_release("current-release")
        protected_release = self.add_installed_release("protected-release")
        (self.app_dir / "current").symlink_to(current_release)
        (self.app_dir / "previous").symlink_to(protected_release)
        self.add_fake_shared_venv(marker="old")
        artifact = self.build_artifact("pointer-failure", pip_result="new")
        injected_installer = self.write_injected_script(
            INSTALL_SCRIPT,
            "platform_release_install_fail_current.sh",
            "    --phase previous-switched\n",
            "false # test-injected current-pointer failure\n",
        )

        result = self.run_script(
            injected_installer,
            str(artifact),
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app_dir / "current").resolve(), current_release)
        self.assertEqual((self.app_dir / "previous").resolve(), protected_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "old\n"
        )
        self.assertFalse((self.releases_dir / f"pointer-failure-{BUILT_AT}").exists())
        self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_release_lock_contention_causes_no_install_or_rollback_mutation(
        self,
    ) -> None:
        artifact = self.root / "lock-test.tar.gz"
        artifact.write_bytes(b"unused while the lock is held")
        Path(f"{artifact}.sha256").write_text(f"{'0' * 64}  {artifact.name}\n")
        assert self._release_lock is not None
        try:
            self._release_lock.acquire(nonblocking=True)
            install = self.run_script(
                INSTALL_SCRIPT,
                str(artifact),
                str(self.app_dir),
                check=False,
            )
            rollback = self.run_script(
                ROLLBACK_SCRIPT,
                "--app-dir",
                str(self.app_dir),
                "--no-restart",
                check=False,
            )
            bootstrap_app = self.root / "first-bootstrap-app"
            bootstrap_install = self.run_script(
                INSTALL_SCRIPT,
                str(artifact),
                str(bootstrap_app),
                check=False,
            )
        finally:
            self._release_lock.release()

        self.assertEqual(install.returncode, 3)
        self.assertEqual(rollback.returncode, 3)
        self.assertEqual(bootstrap_install.returncode, 3)
        self.assertFalse(bootstrap_app.exists())
        self.assertFalse((self.releases_dir / "lock-test").exists())
        self.assertFalse((self.shared_dir / "venv").exists())
        self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_interrupted_installs_are_exactly_recoverable(self) -> None:
        cases = (
            (
                "after-exchange",
                '    /usr/bin/python3 -I "$TRANSACTION_TOOL" exchange '
                '--state "$TRANSACTION_STATE"\n',
                True,
            ),
            (
                "after-snapshot-move",
                "      --mode place-snapshot\n",
                True,
            ),
            (
                "after-previous-pointer",
                "    --phase previous-switched\n",
                False,
            ),
        )
        for index, (label, needle, previous_present) in enumerate(cases):
            with self.subTest(label=label):
                if index:
                    self.reset_app_tree()
                current_release = self.add_installed_release(f"current-{index}")
                protected_release = self.add_installed_release(f"protected-{index}")
                (self.app_dir / "current").symlink_to(current_release)
                if previous_present:
                    (self.app_dir / "previous").symlink_to(protected_release)
                self.add_fake_shared_venv(marker=f"old-{index}")
                release_ref = f"kill-{index}"
                artifact = self.build_artifact(release_ref, pip_result=f"new-{index}")
                injected = self.write_injected_script(
                    INSTALL_SCRIPT,
                    f"platform_release_install_{label}.sh",
                    needle,
                    '/bin/kill -KILL "$$" # test abrupt interruption\n',
                )

                result = self.run_script(
                    injected,
                    str(artifact),
                    str(self.app_dir),
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertTrue((self.shared_dir / "venv").is_dir())
                self.assertTrue((self.shared_dir / TRANSACTION_STATE_NAME).is_file())
                self.run_script(
                    ROLLBACK_SCRIPT,
                    "--recover-pending",
                    "--app-dir",
                    str(self.app_dir),
                )
                self.assertEqual((self.app_dir / "current").resolve(), current_release)
                if previous_present:
                    self.assertEqual(
                        (self.app_dir / "previous").resolve(), protected_release
                    )
                else:
                    self.assertFalse(os.path.lexists(self.app_dir / "previous"))
                self.assertEqual(
                    (self.shared_dir / "venv" / "deps-version").read_text(),
                    f"old-{index}\n",
                )
                self.assertFalse(
                    (self.releases_dir / f"{release_ref}-{BUILT_AT}").exists()
                )
                self.assertFalse(any(self.shared_dir.glob(".venv-install-*")))
                self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_first_install_interruption_uses_install_recovery_fallback(self) -> None:
        artifact = self.build_artifact("first-install", pip_result="new")
        injected = self.write_injected_script(
            INSTALL_SCRIPT,
            "platform_release_install_first_bootstrap.sh",
            "    /usr/bin/python3 -I \"$TRANSACTION_TOOL\" rename \\\n"
            "      --state \"$TRANSACTION_STATE\" \\\n"
            "      --mode activate-created\n",
            '/bin/kill -KILL "$$" # test first-install interruption\n',
        )

        result = self.run_script(
            injected,
            str(artifact),
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        state = self.shared_dir / TRANSACTION_STATE_NAME
        self.assertTrue(state.is_file())
        record = json.loads(state.read_text(encoding="ascii"))
        self.assertEqual(record["operation"], "install")
        self.assertEqual(len(record["operation_id"]), 32)
        self.assertIsNone(record["current_before"])
        self.assertIsNone(record["previous_before"])
        self.assertTrue((self.shared_dir / "venv").is_dir())

        systemd_state = self.shared_dir / ".release-systemd-state.json"
        systemd_state.write_text("unexpected\n", encoding="ascii")
        systemd_state.chmod(0o600)
        blocked = self.run_script(
            ROLLBACK_SCRIPT,
            "--recover-pending",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertTrue(state.exists())
        self.assertTrue((self.shared_dir / "venv").is_dir())
        systemd_state.unlink()

        recovered = self.run_script(
            ROLLBACK_SCRIPT,
            "--recover-pending",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )

        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse(os.path.lexists(self.app_dir / "current"))
        self.assertFalse(os.path.lexists(self.app_dir / "previous"))
        self.assertFalse((self.shared_dir / "venv").exists())
        self.assertFalse(state.exists())
        self.assertFalse(
            (self.releases_dir / f"first-install-{BUILT_AT}").exists()
        )
        self.assertFalse(any(self.shared_dir.glob(".venv-install-*")))

    def test_rollback_interruptions_restore_exact_original_state(self) -> None:
        current_release, previous_release, snapshot = self.prepare_rollback_fixture()
        cases = (
            (
                "after-exchange",
                '  /usr/bin/python3 -I "$TRANSACTION_TOOL" exchange '
                '--state "$TRANSACTION_STATE"\n',
            ),
            ("between-pointers", "  --phase current-switched\n"),
        )
        for label, needle in cases:
            with self.subTest(label=label):
                injected = self.write_injected_script(
                    ROLLBACK_SCRIPT,
                    f"platform_release_rollback_{label}.sh",
                    needle,
                    '/bin/kill -KILL "$$" # test abrupt interruption\n',
                )
                result = self.run_script(
                    injected,
                    "--app-dir",
                    str(self.app_dir),
                    "--no-restart",
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertTrue((self.shared_dir / "venv").is_dir())
                self.assertTrue((self.shared_dir / TRANSACTION_STATE_NAME).is_file())
                self.run_script(
                    ROLLBACK_SCRIPT,
                    "--recover-pending",
                    "--app-dir",
                    str(self.app_dir),
                )
                self.assertEqual((self.app_dir / "current").resolve(), current_release)
                self.assertEqual(
                    (self.app_dir / "previous").resolve(), previous_release
                )
                self.assertEqual(
                    (self.shared_dir / "venv" / "deps-version").read_text(), "new\n"
                )
                self.assertEqual((snapshot / "deps-version").read_text(), "old\n")
                self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_catchable_rollback_failure_after_exchange_recovers_automatically(
        self,
    ) -> None:
        current_release, previous_release, snapshot = self.prepare_rollback_fixture()
        injected = self.write_injected_script(
            ROLLBACK_SCRIPT,
            "platform_release_rollback_false_after_exchange.sh",
            '  /usr/bin/python3 -I "$TRANSACTION_TOOL" exchange '
            '--state "$TRANSACTION_STATE"\n',
            "false # test catchable failure\n",
        )

        result = self.run_script(
            injected,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app_dir / "current").resolve(), current_release)
        self.assertEqual((self.app_dir / "previous").resolve(), previous_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "new\n"
        )
        self.assertEqual((snapshot / "deps-version").read_text(), "old\n")
        self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_rollback_cache_purge_failure_recovers_before_older_activation(self) -> None:
        current_release, previous_release, snapshot = self.prepare_rollback_fixture()

        result = self.run_script(
            ROLLBACK_SCRIPT,
            "--app-dir",
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        state = self.shared_dir / TRANSACTION_STATE_NAME
        self.assertFalse(state.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current_release)
        self.assertEqual((self.app_dir / "previous").resolve(), previous_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "new\n"
        )
        self.assertEqual((snapshot / "deps-version").read_text(), "old\n")

    def test_rollback_refuses_unsafe_pointer_snapshot_and_expected_record(self) -> None:
        outside = self.root / "outside-release"
        outside.mkdir()
        previous = self.add_installed_release("previous")
        (self.app_dir / "current").symlink_to(outside)
        (self.app_dir / "previous").symlink_to(previous)
        result = self.run_script(
            ROLLBACK_SCRIPT,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

        self.reset_app_tree()
        current, previous, snapshot = self.prepare_rollback_fixture()
        moved_snapshot = current / ".rollback" / "unsafe-snapshot-target"
        snapshot.rename(moved_snapshot)
        snapshot.symlink_to(moved_snapshot, target_is_directory=True)
        result = self.run_script(
            ROLLBACK_SCRIPT,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

        snapshot.unlink()
        moved_snapshot.rename(snapshot)
        expected = current / ".rollback" / "previous-release"
        os.link(expected, current / ".rollback" / "unexpected-hardlink")
        result = self.run_script(
            ROLLBACK_SCRIPT,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "new\n"
        )
        self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_rollback_helper_path_survives_current_symlink_switch(self) -> None:
        current_release, previous_release, _snapshot = self.prepare_rollback_fixture()
        self.write_release_manifest(current_release, "b" * 40)
        self.write_release_manifest(previous_release, "c" * 40)
        self.install_restore_nginx_helper(previous_release)
        release_tools = current_release / "tools"
        release_tools.mkdir(exist_ok=True)
        shutil.copy2(ROLLBACK_SCRIPT, release_tools / ROLLBACK_SCRIPT.name)
        shutil.copy2(TRANSACTION_TOOL, release_tools / TRANSACTION_TOOL.name)
        shutil.copy2(RUNTIME_RESTORE_SCRIPT, release_tools / RUNTIME_RESTORE_SCRIPT.name)
        systemd_state_tool = (
            REPO_ROOT / "platform" / "tools" / "platform_release_systemd_state.py"
        )
        shutil.copy2(systemd_state_tool, release_tools / systemd_state_tool.name)
        (release_tools / systemd_state_tool.name).chmod(0o755)
        self.install_test_lock_helper(release_tools / "platform_release_lock.sh")
        shutil.copy2(RECOVERY_SHIM_SCRIPT, release_tools / RECOVERY_SHIM_SCRIPT.name)
        invoked_through_current = (
            self.app_dir / "current" / "tools" / ROLLBACK_SCRIPT.name
        )
        transaction_diagnostic = self.root / "transaction-diagnostic.txt"
        self.install_bounded_transaction_diagnostic(
            current_release, transaction_diagnostic
        )
        purge_v1_keys, preserve_v2_keys = self.prepare_profile_access_purge_runtime(
            current_release, self.shared_dir / "venv"
        )

        result = self.run_script(
            invoked_through_current,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            check=False,
        )
        if result.returncode != 0:
            # The shell intentionally keeps transaction internals out of its
            # public marker and its EXIT recovery may remove the active receipt.
            # This test-only copy records only a bounded, path-free validator
            # reason before that recovery runs.
            self.assertTrue(transaction_diagnostic.is_file())
            metadata = transaction_diagnostic.lstat()
            self.assertFalse(stat.S_ISLNK(metadata.st_mode))
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
            self.fail(
                "bounded transaction diagnostic: "
                + transaction_diagnostic.read_text(encoding="ascii").strip()
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_profile_access_cache_purged(purge_v1_keys, preserve_v2_keys)

        self.assertEqual((self.app_dir / "current").resolve(), previous_release)
        self.assertEqual((self.app_dir / "previous").resolve(), current_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "old\n"
        )

    def reset_app_tree(self) -> None:
        shutil.rmtree(self.app_dir)
        self.releases_dir.mkdir(parents=True)
        self.shared_dir.mkdir()
        (self.shared_dir / ".env.platform").write_text("PLATFORM_TESTING=1\n")
        (self.shared_dir / ".env.platform").chmod(0o600)

    def prepare_rollback_fixture(self) -> tuple[Path, Path, Path]:
        current_release = self.add_installed_release("rollback-current")
        previous_release = self.add_installed_release("rollback-previous")
        (self.app_dir / "current").symlink_to(current_release)
        (self.app_dir / "previous").symlink_to(previous_release)
        self.add_fake_shared_venv(marker="new")
        rollback_dir = current_release / ".rollback"
        rollback_dir.mkdir(mode=0o700)
        snapshot = rollback_dir / "shared-venv-before-install"
        self.add_fake_venv(snapshot, marker="old")
        expected = rollback_dir / "previous-release"
        expected.write_text(f"{previous_release}\n")
        expected.chmod(0o600)
        return current_release, previous_release, snapshot

    def prepare_profile_access_purge_runtime(
        self, source_release: Path, venv: Path
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Give a positive rollback case the real bounded purge child closure."""

        import sys
        import sysconfig

        from redis.asyncio import from_url

        from tools.platform_test_runner import validate_test_resource_configuration

        resources = validate_test_resource_configuration()
        self.assertEqual(resources.database_name, "platformdb_test")
        self.assertEqual(resources.database_schema, "platform")
        self.assertEqual(resources.redis_database, "15")
        self.assertEqual(resources.redis_host, "127.0.0.1")
        parsed_redis_url = urlsplit(resources.redis_url)
        self.assertEqual(
            (parsed_redis_url.scheme, parsed_redis_url.hostname, parsed_redis_url.port, parsed_redis_url.path),
            ("redis", "127.0.0.1", 6379, "/15"),
        )
        self.assertIsNone(parsed_redis_url.username)
        self.assertIsNone(parsed_redis_url.password)
        redis_url = "redis://127.0.0.1:6379/15"

        env_file = self.shared_dir / ".env.platform"
        env_file.write_text(f"PLATFORM_REDIS_URL={redis_url}\n", encoding="ascii")
        env_file.chmod(0o600)

        platform_root = REPO_ROOT / "platform"
        safe_env_tool = source_release / "tools" / "platform_safe_env_exec.py"
        shutil.copyfile(platform_root / "tools" / "platform_safe_env_exec.py", safe_env_tool)
        safe_env_tool.chmod(0o555)

        api_source = platform_root / "apps" / "platform_api" / "app"
        api_destination = source_release / "apps" / "platform_api" / "app"
        api_destination.mkdir(parents=True, exist_ok=True)
        for package in ("__init__.py",):
            shutil.copyfile(api_source / package, api_destination / package)
        service_source = api_source / "services"
        service_destination = api_destination / "services"
        service_destination.mkdir()
        shutil.copyfile(service_source / "__init__.py", service_destination / "__init__.py")
        shutil.copyfile(
            service_source / "tournament_profile_access.py",
            service_destination / "tournament_profile_access.py",
        )
        packages_source = platform_root / "python_packages"
        packages_destination = source_release / "python_packages"
        packages_destination.mkdir()
        shutil.copyfile(packages_source / "__init__.py", packages_destination / "__init__.py")
        shutil.copytree(
            packages_source / "platform_infra",
            packages_destination / "platform_infra",
        )

        self.assertEqual(sys.version_info[:2], (3, 12))
        fixed_python = Path("/usr/bin/python3.12")
        self.assertTrue(fixed_python.is_file())
        fixed_python_identity = fixed_python.resolve(strict=True)
        venv_bin = venv / "bin"
        python = venv_bin / "python"
        try:
            python_metadata = python.lstat()
        except FileNotFoundError:
            python_metadata = None
        if python_metadata is not None:
            if stat.S_ISLNK(python_metadata.st_mode):
                self.assertEqual(
                    python.resolve(strict=True),
                    fixed_python_identity,
                    "existing venv launcher is not the pinned interpreter",
                )
            elif stat.S_ISREG(python_metadata.st_mode):
                # The just-built fixture venv may already contain a launcher.
                # Replace only that regular file with the exact pinned runtime.
                python.unlink()
            else:
                self.fail("venv launcher has an unsupported file type")
        if not python.is_symlink():
            self.assertFalse(os.path.lexists(python))
            python.symlink_to(fixed_python)
        self.assertEqual(python.resolve(strict=True), fixed_python_identity)
        purelib = Path(sysconfig.get_path("purelib")).resolve(strict=True)
        try:
            purelib.relative_to(Path(sys.prefix).resolve(strict=True))
        except ValueError as exc:
            raise AssertionError("test child packages are outside the pinned venv") from exc
        for dependency in ("redis", "sqlalchemy", "pydantic_settings"):
            self.assertTrue((purelib / dependency).exists(), dependency)
        (venv / "pyvenv.cfg").write_text(
            "home = /usr/bin\n"
            "include-system-site-packages = false\n"
            f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n",
            encoding="ascii",
        )
        site_packages = (
            venv
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        site_packages.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(site_packages.parent.is_dir())
        self.assertFalse(site_packages.parent.is_symlink())
        try:
            site_packages_metadata = site_packages.lstat()
        except FileNotFoundError:
            site_packages_metadata = None
        if site_packages_metadata is not None and stat.S_ISLNK(
            site_packages_metadata.st_mode
        ):
            self.assertEqual(
                site_packages.resolve(strict=True),
                purelib,
                "existing venv site-packages link is not the pinned package set",
            )
        elif site_packages_metadata is None:
            site_packages.symlink_to(purelib, target_is_directory=True)
        else:
            self.assertTrue(stat.S_ISDIR(site_packages_metadata.st_mode))
            bridge = site_packages / "platform_test_pinned_packages.pth"
            bridge_contents = f"{purelib}\n"
            if os.path.lexists(bridge):
                bridge_metadata = bridge.lstat()
                self.assertTrue(stat.S_ISREG(bridge_metadata.st_mode))
                self.assertEqual(bridge.read_text(encoding="ascii"), bridge_contents)
            else:
                descriptor = os.open(
                    bridge,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o644,
                )
                with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                    stream.write(bridge_contents)

        nonce = uuid.uuid4().hex
        v1_keys = tuple(
            f"platform:tournament:{namespace}:v1:rollback-fixture-{nonce}"
            for namespace in ("profile-access", "profile-viewers", "profile-roster")
        )
        v2_keys = tuple(
            f"platform:tournament:{namespace}:v2:rollback-fixture-{nonce}"
            for namespace in ("profile-access", "profile-viewers", "profile-roster")
        )

        async def seed() -> None:
            client = from_url(redis_url, decode_responses=False)
            try:
                self.assertTrue(await client.ping())
                self.assertEqual(await client.exists(*v1_keys, *v2_keys), 0)
                await client.mset(
                    {
                        **{key: b"legacy-v1" for key in v1_keys},
                        **{key: b"preserve-v2" for key in v2_keys},
                    }
                )
            finally:
                await client.aclose()

        asyncio.run(seed())
        self.addCleanup(self.delete_profile_access_fixture_keys, redis_url, v1_keys, v2_keys)
        return v1_keys, v2_keys

    def assert_profile_access_cache_purged(
        self, v1_keys: tuple[str, ...], v2_keys: tuple[str, ...]
    ) -> None:
        from redis.asyncio import from_url

        async def check() -> None:
            client = from_url("redis://127.0.0.1:6379/15", decode_responses=False)
            try:
                self.assertTrue(await client.ping())
                self.assertEqual(await client.exists(*v1_keys), 0)
                self.assertEqual(
                    await client.mget(v2_keys),
                    [b"preserve-v2"] * len(v2_keys),
                )
            finally:
                await client.aclose()

        asyncio.run(check())

    def refresh_unchanged_venv_freeze_receipt(
        self, release: Path, venv: Path
    ) -> None:
        """Bind skip-deps rollback metadata to the real pinned test venv."""

        env = {
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PIP_CONFIG_FILE": "/dev/null",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
        }
        result = subprocess.run(
            [str(venv / "bin/python"), "-I", "-m", "pip", "freeze", "--all"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
            close_fds=True,
            env=env,
            text=True,
        )
        self.assertEqual(result.returncode, 0, "pinned venv freeze failed")
        frozen = "".join(f"{line}\n" for line in sorted(result.stdout.splitlines()))
        freeze = release / "requirements-platform.freeze.txt"
        freeze.write_text(frozen, encoding="utf-8")
        freeze.chmod(0o444)
        digest = hashlib.sha256(freeze.read_bytes()).hexdigest()
        receipt = release / ".rollback" / "shared-freeze.sha256"
        receipt.write_text(f"{digest}\n", encoding="ascii")
        receipt.chmod(0o600)

    def delete_profile_access_fixture_keys(
        self,
        redis_url: str,
        v1_keys: tuple[str, ...],
        v2_keys: tuple[str, ...],
    ) -> None:
        from redis.asyncio import from_url

        async def delete() -> None:
            client = from_url(redis_url, decode_responses=False)
            try:
                await client.delete(*v1_keys, *v2_keys)
            finally:
                await client.aclose()

        asyncio.run(delete())

    def write_release_manifest(self, release: Path, source_sha: str) -> None:
        manifest = release / "RELEASE.json"
        manifest.write_text(
            json.dumps({"source_git_commit": source_sha}, sort_keys=True) + "\n",
            encoding="ascii",
        )
        manifest.chmod(0o444)

    def install_restore_nginx_helper(self, release: Path) -> None:
        """Make the restored release's Python-invoked helper executable code."""

        helper = release / "tools" / "platform_install_nginx.py"
        source = (
            "import sys\n"
            "valid_args = sys.argv[1:] == ['--apply', '--reload', '--json']\n"
            "if not valid_args:\n"
            "    raise SystemExit(64)\n"
        )
        compile(source, str(helper), "exec")
        helper.write_text(source, encoding="ascii")
        helper.chmod(0o755)

    def install_restore_runtime_reconcile_helper(
        self, release: Path, app_dir: Path
    ) -> None:
        """Model the restored release's exact Python reconcile entrypoint."""

        helper = release / "tools" / "platform_live_qa_runtime_install.py"
        expected_app_dir = str(app_dir)
        source = (
            "import sys\n"
            "from pathlib import Path\n"
            f"expected = ['reconcile', '--app-dir', {expected_app_dir!r}]\n"
            "if sys.argv[1:] != expected:\n"
            "    raise SystemExit(64)\n"
            "app_dir = Path(sys.argv[3])\n"
            "current = app_dir / 'current'\n"
            "release = Path(__file__).resolve().parent.parent\n"
            "if not app_dir.is_absolute() or not current.is_symlink():\n"
            "    raise SystemExit(65)\n"
            "if current.resolve(strict=True) != release:\n"
            "    raise SystemExit(65)\n"
        )
        compile(source, str(helper), "exec")
        helper.write_text(source, encoding="ascii")
        helper.chmod(0o755)

    def install_bounded_transaction_diagnostic(
        self, release: Path, diagnostic_path: Path
    ) -> None:
        """Instrument only the copied CLI with a private, bounded failure code."""

        helper = release / "tools" / "platform_release_transaction.py"
        source = helper.read_text(encoding="utf-8")
        needle = "    except TransactionError as exc:\n"
        self.assertEqual(source.count(needle), 1)
        diagnostic = (
            "        try:\n"
            f"            _diag_fd = os.open({str(diagnostic_path)!r}, "
            "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)\n"
            "            _diag_message = str(exc)\n"
            "            _diag_safe = (\n"
            "                len(_diag_message) <= 160\n"
            "                and all(ch.isascii() and (ch.isalnum() or ch in ' _-') for ch in _diag_message)\n"
            "            )\n"
            "            _diag_text = (\n"
            "                type(exc).__name__ + '\\t' + (_diag_message if _diag_safe else 'unclassified') + '\\n'\n"
            "            ).encode('ascii')\n"
            "            with os.fdopen(_diag_fd, 'wb') as _diag_stream:\n"
            "                _diag_stream.write(_diag_text)\n"
            "                _diag_stream.flush()\n"
            "                os.fsync(_diag_stream.fileno())\n"
            "        except OSError:\n"
            "            pass\n"
        )
        helper.write_text(source.replace(needle, needle + diagnostic, 1), encoding="utf-8")
        helper.chmod(0o755)

    def _script_with_physical_tools(self, source: Path) -> str:
        lines = source.read_text().splitlines(keepends=True)
        tools_lines = [
            index for index, line in enumerate(lines) if line.startswith('TOOLS_DIR="')
        ]
        self.assertEqual(len(tools_lines), 1)
        lines[tools_lines[0]] = f'TOOLS_DIR="{self.tools_dir}"\n'
        return "".join(lines)

    def install_test_lock_helper(self, destination: Path) -> None:
        """Install a test-local helper while retaining production validation."""

        helper = (REPO_ROOT / "platform" / "tools" / "platform_release_lock.sh").read_text(
            encoding="utf-8"
        )
        helper = helper.replace(
            "/run/lock/oldsparky-platform-release.lock",
            str(self.release_lock_path),
        )
        destination.write_text(helper, encoding="utf-8")
        destination.chmod(0o755)

    def write_injected_script(
        self,
        source: Path,
        name: str,
        needle: str,
        insertion: str,
    ) -> Path:
        script = self._script_with_physical_tools(source)
        self.assertIn(needle, script)
        script = script.replace(needle, needle + insertion, 1)
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def write_replaced_script(
        self,
        source: Path,
        name: str,
        needle: str,
        replacement: str,
    ) -> Path:
        script = self._script_with_physical_tools(source)
        self.assertIn(needle, script)
        target = self.root / name
        target.write_text(script.replace(needle, replacement))
        target.chmod(0o755)
        return target

    def add_installed_release(self, name: str) -> Path:
        release = self.releases_dir / name
        release.mkdir()
        tools = release / "tools"
        tools.mkdir()
        for tool_name in RELEASE_HELPER_NAMES:
            tool = tools / tool_name
            tool.write_text("#!/usr/bin/env sh\nexit 0\n")
            tool.chmod(0o755)
        return release

    def build_artifact(self, release_ref: str, *, pip_result: str) -> Path:
        slug = f"{release_ref}-{BUILT_AT}"
        release = self.root / "artifact-src" / slug
        static_dir = (
            release
            / "apps"
            / "platform_web"
            / ".next"
            / "standalone"
            / ".next"
            / "static"
        )
        static_dir.mkdir(parents=True)
        (
            release / "apps" / "platform_web" / ".next" / "standalone" / "server.js"
        ).write_text("console.log('ok');\n")
        (release / "apps" / "platform_web" / "package-lock.json").write_text("{}\n")
        (release / ".env.platform.example").write_text("PLATFORM_TESTING=1\n")
        (release / "requirements-platform.txt").write_text("pip==26.1.2\n")
        lock = release / "requirements-platform.lock.txt"
        freeze = release / "requirements-platform.freeze.txt"
        freeze.write_text("pip==26.1.2\n")
        freeze.chmod(0o444)
        wheelhouse = release / "wheelhouse"
        wheelhouse.mkdir()
        for relative in platform_validate_release_artifact.REQUIRED_RUNTIME_DIAGNOSTIC_HELPERS:
            helper = release / relative
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text("# aggregate-only runtime helper fixture\n")
        self.add_liveqa_runtime(release)
        for tool_name in RELEASE_HELPER_NAMES:
            helper = release / "tools" / tool_name
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text("#!/usr/bin/env sh\nexit 0\n")
            helper.chmod(0o755)
        runtime_installer = release / "tools" / "platform_live_qa_runtime_install.py"
        runtime_installer.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n")
        runtime_installer.chmod(0o755)
        pip_wheel = self.add_fake_pip_wheel(wheelhouse, result=pip_result)
        lock.write_text(
            "pip==26.1.2 --hash=sha256:"
            f"{hashlib.sha256(pip_wheel.read_bytes()).hexdigest()}\n"
        )
        lock.chmod(0o644)
        platform_validate_wheelhouse.create_manifest(
            wheelhouse,
            release / "requirements-platform.txt",
            lock,
            freeze,
        )
        payload = {
            "artifact_format_version": 1,
            "release_slug": slug,
            "built_at_utc": BUILT_AT,
            "release_ref": release_ref,
            "source_git_commit": "a" * 40,
            "python_requirements_file": "requirements-platform.txt",
            "python_lock_file": "requirements-platform.lock.txt",
            "python_freeze_file": "requirements-platform.freeze.txt",
            "python_wheelhouse_dir": "wheelhouse",
            "python_wheelhouse_manifest_file": "wheelhouse/WHEELHOUSE.sha256",
            "web_package_lock_file": "apps/platform_web/package-lock.json",
            "web_build_id": "test-build-id",
            "node_version": "26.3.1",
            "npm_version": "11.16.0",
        }
        release_json = release / "RELEASE.json"
        release_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        release_json.chmod(0o444)

        artifact = self.root / f"{slug}.tar.gz"

        def archive_filter(member: tarfile.TarInfo) -> tarfile.TarInfo:
            if member.name == (
                f"{slug}/{platform_validate_release_artifact.LIVE_QA_SANDBOX_RELATIVE}"
            ):
                member.uid = member.gid = 0
                member.mode = 0o4755
            return member

        with tarfile.open(artifact, "w:gz") as archive:
            archive.add(release, arcname=slug, filter=archive_filter)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        Path(f"{artifact}.sha256").write_text(f"{digest}  {artifact.name}\n")
        return artifact

    def add_liveqa_runtime(self, release: Path) -> None:
        """Build the smallest immutable runtime accepted by the artifact validator."""

        runtime = release / platform_validate_release_artifact.LIVE_QA_RUNTIME_ROOT
        directories = (
            runtime,
            runtime / "node",
            runtime / "node" / "bin",
            runtime / "web",
            runtime / "web" / "tests",
            runtime / "web" / "tests" / "smoke",
            runtime / "web" / "tests" / "support",
            runtime / "web" / "node_modules",
            runtime / "web" / "node_modules" / "@playwright",
            runtime / "web" / "node_modules" / "@playwright" / "test",
            runtime / "web" / "node_modules" / "playwright",
            runtime / "web" / "node_modules" / "playwright-core",
            runtime / "browsers",
            runtime / "browsers" / "chromium-1228",
            runtime / "browsers" / "chromium-1228" / "chrome-linux64",
            runtime / "browsers" / "chromium-1228" / "chrome-linux64" / "resources",
            runtime
            / "browsers"
            / "chromium-1228"
            / "chrome-linux64"
            / "resources"
            / "accessibility",
            runtime / "browsers" / "chromium_headless_shell-1228",
            runtime / "browsers" / "webkit-2311",
            runtime / "browsers" / "ffmpeg-1011",
        )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o555)

        files: dict[str, bytes] = {
            "node/bin/node": b"node\n",
            "web/package-lock.json": b"{}\n",
            "web/playwright.live.config.ts": b"export default {};\n",
            "web/tests/smoke/live-launch.spec.ts": b"test('live launch', () => {});\n",
            "web/tests/smoke/live-user-journey.spec.ts": b"test('live', () => {});\n",
            "web/tests/support/live-qa-origin.ts": b"export {};\n",
            "web/tests/support/live-qa-sandbox.ts": b"export {};\n",
            "web/tests/support/live-count-reporter.cjs": (
                REPO_ROOT
                / "platform/apps/platform_web/tests/support/live-count-reporter.cjs"
            ).read_bytes(),
            "web/node_modules/@playwright/test/package.json": b'{"name":"@playwright/test"}\n',
            "web/node_modules/playwright/package.json": b'{"name":"playwright"}\n',
            "web/node_modules/playwright-core/package.json": b'{"name":"playwright-core"}\n',
            "browsers/chromium-1228/chrome-linux64/resources.pak": b"pak\n",
            "browsers/chromium-1228/chrome-linux64/resources/accessibility/ax": b"ax\n",
        }
        for relative, content in files.items():
            path = runtime / relative
            path.write_bytes(content)
            path.chmod(0o555 if relative == "node/bin/node" else 0o444)

        sandbox = runtime / "browsers" / "chromium-1228" / "chrome-linux64" / "chrome_sandbox"
        sandbox.write_bytes(chromium_sandbox_fixture.read_bytes())
        sandbox.chmod(0o444)
        self.assertEqual(stat.S_IMODE(sandbox.stat().st_mode), 0o444)
        self.assertEqual(
            platform_validate_release_artifact.LIVE_QA_SANDBOX_SIZE,
            chromium_sandbox_fixture.EXPECTED_SIZE,
        )
        self.assertEqual(
            platform_validate_release_artifact.LIVE_QA_SANDBOX_SHA256,
            chromium_sandbox_fixture.EXPECTED_SHA256,
        )

        digest = hashlib.sha256()
        manifest_files: dict[str, str] = {}
        runtime_members = sorted(
            runtime.rglob("*"),
            key=lambda path: PurePosixPath(
                path.relative_to(runtime).as_posix()
            ).parts,
        )
        for path in runtime_members:
            relative = path.relative_to(runtime).as_posix()
            if relative == "runtime-manifest.json":
                continue
            digest.update(relative.encode("utf-8") + b"\0")
            if path.is_dir():
                digest.update(b"d\0")
                continue
            file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest_files[relative] = file_digest
            digest.update(b"f\0" + bytes.fromhex(file_digest))
        manifest = {
            "version": 1,
            "node_version": platform_validate_release_artifact.PINNED_NODE_VERSION,
            "package_lock_sha256": hashlib.sha256(b"{}\n").hexdigest(),
            "tree_sha256": digest.hexdigest(),
            "files": manifest_files,
        }
        manifest_path = runtime / "runtime-manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
        manifest_path.chmod(0o444)

    def add_fake_pip_wheel(self, wheelhouse: Path, *, result: str) -> Path:
        wheel = wheelhouse / "pip-26.1.2-py3-none-any.whl"
        module = f"""\
from pathlib import Path
import base64
import csv
import hashlib
import io
import os
import py_compile
import sys
import zipfile

arguments = sys.argv[1:]
install_result = {result!r}
if arguments and arguments[0] == "install":
    Path(sys.prefix, "deps-version").write_text({result!r} + "\\n")
    import sysconfig
    site_packages = Path(sysconfig.get_paths()["purelib"])
    site_packages.mkdir(parents=True, exist_ok=True)
    (site_packages / "release_permission_probe.py").write_text(
        "VALUE = 'readable dependency'\\n"
    )
    wheel_arg = Path(arguments[-1])
    if wheel_arg.suffix == ".whl":
        with zipfile.ZipFile(wheel_arg) as archive:
            script_members = [
                name for name in archive.namelist()
                if ".data/scripts/" in name and name.endswith(".py")
            ]
            script_payloads = [(Path(name).name, archive.read(name)) for name in script_members]
        installed_payloads = []
        for script_name, script_body in script_payloads:
            script_target = Path(sys.prefix, "bin", script_name)
            if script_body.startswith(b"#!python"):
                installed_script = b"#!" + os.fsencode(sys.executable) + script_body[len(b"#!python"):]
            else:
                installed_script = script_body
            script_target.write_bytes(installed_script)
            script_target.chmod(0o755)
            cache_target = Path(py_compile.cache_from_source(str(script_target)))
            if install_result != "no-cache":
                py_compile.compile(str(script_target), doraise=True)
            installed_payloads.append((script_target, cache_target, installed_script))
        record = site_packages / "pip-26.1.2.dist-info" / "RECORD"
        if wheel_arg.suffix == ".whl":
            record.parent.mkdir(parents=True, exist_ok=True)
            output = io.StringIO(newline="")
            writer = csv.writer(output, lineterminator="\\n")
            for script_target, cache_target, installed_script in installed_payloads:
                script_hash = base64.urlsafe_b64encode(hashlib.sha256(installed_script).digest()).rstrip(b"=").decode()
                writer.writerow((os.path.relpath(script_target, site_packages), "sha256=" + script_hash, str(len(installed_script))))
                cache_relative = os.path.relpath(cache_target, site_packages)
                writer.writerow((cache_relative, "", ""))
            writer.writerow(("pip-26.1.2.dist-info/RECORD", "", ""))
            record.write_text(output.getvalue())
    raise SystemExit({42 if result == "fail" else 0})
if arguments and arguments[0] == "check":
    print("No broken requirements found.")
    raise SystemExit(0)
if arguments[:2] == ["freeze", "--all"]:
    print("pip==26.1.2")
    raise SystemExit(0)
raise SystemExit("unsupported fake pip invocation: " + repr(arguments))
"""
        dist_info = "pip-26.1.2.dist-info"
        wheel_payloads = {
            "pip/__init__.py": b'__version__ = "26.1.2"\n',
            "pip/__main__.py": module.encode(),
            "pip/cli.py": b'def main():\n    print("relocated console script")\n',
            f"{dist_info}/METADATA": b"Metadata-Version: 2.1\nName: pip\nVersion: 26.1.2\n\n",
            f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"{dist_info}/entry_points.txt": b"[console_scripts]\nfake-pip-cli = pip.cli:main\n",
            "pip-26.1.2.data/scripts/relocation_probe.py": b"#!python\nprint('relocated probe')\n",
            "pip-26.1.2.data/scripts/unrelated_probe.py": b"#!/usr/bin/env python3\nprint('unrelated probe')\n",
        }
        record = io.StringIO(newline="")
        writer = csv.writer(record, lineterminator="\n")
        for member_name, content in sorted(wheel_payloads.items()):
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
            writer.writerow((member_name, "sha256=" + digest, str(len(content))))
        writer.writerow((f"{dist_info}/RECORD", "", ""))
        with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for member_name, content in wheel_payloads.items():
                archive.writestr(member_name, content)
            archive.writestr(f"{dist_info}/RECORD", record.getvalue())
        return wheel

    def add_fake_shared_venv(
        self, *, marker: str, matching_freeze: bool = False
    ) -> None:
        self.add_fake_venv(self.shared_dir / "venv", marker=marker)
        if matching_freeze:
            self.write_executable(
                self.shared_dir / "venv" / "bin" / "python",
                'if [ "$*" = "-I -B -m pip freeze --all" ] || '
                '[ "$*" = "-I -m pip freeze --all" ]; then\n'
                "  printf '%s\\n' 'pip==26.1.2'\n"
                "fi\n",
            )

    def add_fake_venv(self, venv: Path, *, marker: str) -> None:
        bin_dir = venv / "bin"
        bin_dir.mkdir(parents=True)
        (venv / "deps-version").write_text(f"{marker}\n")
        self.write_executable(bin_dir / "python", "exit 0\n")

    def write_executable(self, path: Path, body: str) -> None:
        path.write_text(f"#!/usr/bin/env sh\nset -eu\n{body}")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def run_script(
        self,
        script: Path,
        *args: str,
        check: bool = True,
        cwd: Path | None = None,
        relocation_case: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command_env = os.environ.copy()
        command_env["PLATFORM_ENVIRONMENT"] = "test"
        command_env["PLATFORM_TESTING"] = "1"
        command_env["PLATFORM_TEST_SYSTEMCTL_TRACE"] = str(
            self.root / "systemctl-calls.log"
        )
        if relocation_case is not None:
            if relocation_case not in {
                "hashed-cache-row", "duplicate-cache-row", "noncanonical-cache-row",
                "bad-cache-header", "bad-cache-code", "no-cache",
            }:
                raise AssertionError("unknown relocation test case")
            command_env["PLATFORM_TEST_RELOCATION_CASE"] = relocation_case
        command_script = script
        if script in (INSTALL_SCRIPT, ROLLBACK_SCRIPT) or "platform_release_rollback" in script.name:
            command_script = self.root / f".{script.stem}.systemctl.sh"
            script_text = script.read_text()
            tools_needle = 'TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"'
            if tools_needle in script_text:
                tools_dir = self.tools_dir
                if script not in (INSTALL_SCRIPT, ROLLBACK_SCRIPT) and script.parent != self.root:
                    tools_dir = script.parent.resolve()
                script_text = script_text.replace(
                    tools_needle, f'TOOLS_DIR="{tools_dir}"', 1
                )
            if relocation_case is not None and script == INSTALL_SCRIPT:
                relocation_call = '  relocate_venv_paths "$NEW_VENV_DIR" "$SHARED_VENV_DIR" "$RELEASE_DIR/wheelhouse" >/dev/null 2>/dev/null'
                test_mutation = '''  if [[ -n "${PLATFORM_TEST_RELOCATION_CASE:-}" ]]; then
    /usr/bin/python3 -I -S -B - "$NEW_VENV_DIR" "$PLATFORM_TEST_RELOCATION_CASE" <<'PYTEST_RELOCATION_CACHE'
import csv
import importlib.util
import marshal
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
case = sys.argv[2]
site = next(root.glob("lib/python*/site-packages"))
script = root / "bin" / "relocation_probe.py"
cache = Path(importlib.util.cache_from_source(str(script)))
if case == "no-cache":
    cache.unlink(missing_ok=True)
else:
    record = site / "pip-26.1.2.dist-info" / "RECORD"
    rows = list(csv.reader(record.read_text().splitlines()))
    relative = os.path.relpath(cache, site).replace(os.sep, "/")
    if case == "hashed-cache-row":
        import base64
        import hashlib
        content = cache.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
        rows = [(row[0], "sha256=" + digest, str(len(content))) if row[0] == relative else row for row in rows]
    elif case == "duplicate-cache-row":
        rows.append((relative, "", ""))
    elif case == "noncanonical-cache-row":
        rows = [("missing/../" + row[0], row[1], row[2]) if row[0] == relative else row for row in rows]
    elif case == "bad-cache-header":
        content = bytearray(cache.read_bytes())
        content[12] ^= 1
        cache.write_bytes(content)
    elif case == "bad-cache-code":
        content = cache.read_bytes()
        cache.write_bytes(content[:16] + marshal.dumps(compile("pass\\n", str(script), "exec", dont_inherit=True)))
    output = []
    for row in rows:
        line = []
        for value in row:
            line.append('"' + value.replace('"', '""') + '"' if any(c in value for c in ',"\\n') else value)
        output.append(",".join(line))
    record.write_text("\\n".join(output) + "\\n")
PYTEST_RELOCATION_CACHE
  fi
'''
                if relocation_call not in script_text:
                    raise AssertionError("installer relocation call changed; update the focused test hook")
                script_text = script_text.replace(
                    relocation_call, test_mutation + relocation_call, 1
                )
            command_script.write_text(
                script_text.replace("/usr/bin/systemctl", str(self.fake_systemctl))
            )
            command_script.chmod(0o755)
        result = subprocess.run(
            [str(command_script), *args],
            cwd=cwd or REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=command_env,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                f"{command_script} failed: {result.returncode}\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

if __name__ == "__main__":
    unittest.main()
