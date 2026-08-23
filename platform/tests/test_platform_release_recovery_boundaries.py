from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = REPO_ROOT / "platform/tools/platform_release_deploy.sh"
ROLLBACK_SCRIPT = REPO_ROOT / "platform/tools/platform_release_rollback.sh"
RUNTIME_RESTORE_SCRIPT = REPO_ROOT / "platform/tools/platform_release_restore_runtime.sh"
TRANSACTION_TOOL = REPO_ROOT / "platform/tools/platform_release_transaction.py"
STATE_NAME = ".release-operation.json"


class PlatformReleaseRecoveryBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.app_dir = self.root / "platform-app"
        self.releases = self.app_dir / "releases"
        self.shared = self.app_dir / "shared"
        self.releases.mkdir(parents=True)
        self.shared.mkdir()
        (self.shared / ".env.platform").write_text("PLATFORM_TESTING=1\n")
        (self.shared / ".env.platform").chmod(0o600)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resume_activation_committed_cleans_receipt(self) -> None:
        current, previous, candidate = self.prepare_install_state()
        self.advance_install_state(candidate, current, phase="activation-committed")
        self.switch_pointer("previous", current)
        self.switch_pointer("current", candidate)

        result = self.run_script(
            DEPLOY_SCRIPT,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)
        self.assertNotEqual(previous, candidate)

    def test_abort_after_candidate_nginx_apply_restores_previous_nginx(self) -> None:
        current, _previous, candidate = self.prepare_install_state(
            with_fake_python=True
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase="services-restarted")
        self.switch_pointer("previous", current)
        self.switch_pointer("current", candidate)
        nginx_state = self.root / "nginx.state"

        interrupted = self.copy_script_with_replacement(
            DEPLOY_SCRIPT,
            "deploy-after-nginx-apply-kill.sh",
            '  set_phase nginx-pending nginx-applied\n',
            '  /bin/kill -KILL "$$"\n  set_phase nginx-pending nginx-applied\n',
        )
        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env={
                "PLATFORM_TEST_NGINX_STATE": str(nginx_state),
                "PLATFORM_TEST_NGINX_LABEL": "candidate",
            },
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(nginx_state.exists(), result.stderr)
        self.assertEqual(nginx_state.read_text(), "candidate\n")
        self.assertEqual(self.state_phase(), "nginx-pending")

        abort = self.copy_abort_script("abort-after-nginx-apply.sh")
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env={
                "PLATFORM_TEST_UNITS_STATE": str(self.root / "units.state"),
                "PLATFORM_TEST_UNITS_LABEL": "previous",
                "PLATFORM_TEST_NGINX_STATE": str(nginx_state),
                "PLATFORM_TEST_NGINX_LABEL": "previous",
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(nginx_state.read_text(), "previous\n")
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)

    def test_rollback_restores_previous_units_and_nginx_before_completion(self) -> None:
        current = self.add_release("current")
        previous = self.add_release("previous")
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        self.prepare_rollback_venv(current)
        units_state = self.root / "units.state"
        nginx_state = self.root / "nginx.state"
        units_state.write_text("current\n")
        nginx_state.write_text("current\n")

        result = self.run_script(
            ROLLBACK_SCRIPT,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            env={
                "PLATFORM_TEST_UNITS_STATE": str(units_state),
                "PLATFORM_TEST_UNITS_LABEL": "previous",
                "PLATFORM_TEST_NGINX_STATE": str(nginx_state),
                "PLATFORM_TEST_NGINX_LABEL": "previous",
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(units_state.read_text(), "previous\n")
        self.assertEqual(nginx_state.read_text(), "previous\n")
        self.assertEqual((self.app_dir / "current").resolve(), previous)
        self.assertEqual((self.app_dir / "previous").resolve(), current)
        self.assertFalse((self.shared / STATE_NAME).exists())

    def test_deploy_faults_after_units_and_restart_resume_to_commit(self) -> None:
        for label, needle in (
            (
                "units",
                '  run_candidate tools/platform_install_systemd_units.sh\n',
            ),
            (
                "restart",
                '  set_phase activation-pending services-restarted\n',
            ),
        ):
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                current, _previous, candidate = self.prepare_deploy_state(
                    "activation-pending"
                )
                systemctl = self.write_fake_systemctl()
                interrupted = self.copy_deploy_script_with_fault(
                    f"deploy-{label}-kill.sh",
                    needle,
                    f'  /bin/kill -KILL "$$"\n{needle}',
                    systemctl,
                )
                result = self.run_script(
                    interrupted,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label="candidate"),
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.state_phase(), "activation-pending")

                resume = self.copy_deploy_script_with_fault(
                    f"deploy-{label}-resume.sh", None, None, systemctl
                )
                result = self.run_script(
                    resume,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label="candidate"),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertEqual((self.app_dir / "current").resolve(), candidate)
                self.assertEqual((self.app_dir / "previous").resolve(), current)

    def test_deploy_fault_after_smoke_resumes_and_commits(self) -> None:
        current, _previous, candidate = self.prepare_deploy_state("nginx-applied")
        systemctl = self.write_fake_systemctl()
        interrupted = self.copy_deploy_script_with_fault(
            "deploy-smoke-kill.sh",
            '  /usr/bin/true\n  set_phase nginx-applied smoke-passed\n',
            '  /bin/kill -KILL "$$"\n  /usr/bin/true\n  set_phase nginx-applied smoke-passed\n',
            systemctl,
        )
        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="candidate"),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "nginx-applied")
        resume = self.copy_deploy_script_with_fault(
            "deploy-smoke-resume.sh", None, None, systemctl
        )
        result = self.run_script(
            resume,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="candidate"),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

    def test_deploy_fault_after_activation_commit_resumes_cleanup(self) -> None:
        current, _previous, candidate = self.prepare_deploy_state("smoke-passed")
        systemctl = self.write_fake_systemctl()
        interrupted = self.copy_deploy_script_with_fault(
            "deploy-activation-commit-kill.sh",
            '  set_phase smoke-passed activation-committed\n',
            '  set_phase smoke-passed activation-committed\n  /bin/kill -KILL "$$"\n',
            systemctl,
        )
        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="candidate"),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "activation-committed")
        result = self.run_script(
            DEPLOY_SCRIPT,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

    def test_fault_after_final_receipt_cleanup_leaves_committed_state(self) -> None:
        current, _previous, candidate = self.prepare_deploy_state("activation-committed")
        systemctl = self.write_fake_systemctl()
        interrupted = self.copy_deploy_script_with_fault(
            "deploy-receipt-cleanup-kill.sh",
            '  /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"\n',
            '  /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"\n'
            '  /bin/kill -KILL "$$"\n',
            systemctl,
        )
        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

    def test_rollback_faults_in_runtime_restore_recover_original_state(self) -> None:
        for label, needle in (
            (
                "units",
                '  PLATFORM_APP_DIR="$APP_DIR" "$UNITS_TOOL"\n',
            ),
            (
                "nginx",
                '    "$NGINX_TOOL" --apply --reload --json\n',
            ),
        ):
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                current, previous = self.prepare_rollback_state()
                runtime = self.copy_runtime_with_fault(
                    f"runtime-{label}-kill.sh",
                    needle,
                    f'{needle}  /bin/kill -KILL "$PPID"\n',
                )
                interrupted = self.copy_rollback_with_runtime(
                    f"rollback-{label}-kill.sh", runtime
                )
                result = self.run_script(
                    interrupted,
                    "--app-dir",
                    str(self.app_dir),
                    "--no-restart",
                    env=self.runtime_env(label="previous"),
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.state_phase(), "rollback-runtime-pending")
                recovery_runtime = self.write_test_runtime_restore()
                recovery = self.copy_rollback_with_runtime(
                    f"rollback-{label}-recover.sh", recovery_runtime
                )
                result = self.run_script(
                    recovery,
                    "--recover-pending",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label="current"),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertEqual((self.app_dir / "current").resolve(), current)
                self.assertEqual((self.app_dir / "previous").resolve(), previous)
                self.assertEqual(
                    (self.shared / "venv" / "deps-version").read_text(), "new\n"
                )

    def prepare_deploy_state(self, phase: str) -> tuple[Path, Path, Path]:
        current, previous, candidate = self.prepare_install_state(
            with_fake_python=True
        )
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase=phase)
        self.switch_pointer("previous", current)
        self.switch_pointer("current", candidate)
        return current, previous, candidate

    def prepare_rollback_state(self) -> tuple[Path, Path]:
        current = self.add_release("rollback-current")
        previous = self.add_release("rollback-previous")
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        self.add_fake_venv(self.shared / "venv", marker="new")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        rollback = current / ".rollback"
        rollback.mkdir()
        snapshot = rollback / "shared-venv-before-install"
        shutil.copytree(self.shared / "venv", snapshot)
        (rollback / "previous-release").write_text(f"{previous}\n")
        (rollback / "previous-release").chmod(0o600)
        (rollback / "venv-transition").write_text("snapshot\n")
        (rollback / "venv-transition").chmod(0o600)
        return current, previous

    def runtime_env(self, *, label: str) -> dict[str, str]:
        return {
            "PLATFORM_TEST_UNITS_STATE": str(self.root / "units.state"),
            "PLATFORM_TEST_UNITS_LABEL": label,
            "PLATFORM_TEST_NGINX_STATE": str(self.root / "nginx.state"),
            "PLATFORM_TEST_NGINX_LABEL": label,
        }

    def write_fake_systemctl(self) -> Path:
        path = self.root / "systemctl"
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n")
        path.chmod(0o755)
        return path

    def copy_deploy_script_with_fault(
        self,
        name: str,
        needle: str | None,
        replacement: str | None,
        systemctl: Path,
    ) -> Path:
        script = self.script_with_physical_tools(DEPLOY_SCRIPT)
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        script = script.replace("/usr/bin/curl", "/usr/bin/true")
        script = script.replace(
            "  release_preflight\n  set_phase nginx-applied smoke-passed\n",
            "  /usr/bin/true\n  set_phase nginx-applied smoke-passed\n",
            1,
        )
        if needle is not None:
            self.assertIn(needle, script)
            assert replacement is not None
            script = script.replace(needle, replacement, 1)
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def copy_runtime_with_fault(
        self, name: str, needle: str, replacement: str
    ) -> Path:
        script = RUNTIME_RESTORE_SCRIPT.read_text()
        self.assertIn(needle, script)
        target = self.root / name
        target.write_text(script.replace(needle, replacement, 1))
        target.chmod(0o755)
        return target

    def copy_rollback_with_runtime(self, name: str, runtime: Path) -> Path:
        script = self.script_with_physical_tools(ROLLBACK_SCRIPT)
        needle = 'RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"'
        self.assertIn(needle, script)
        script = script.replace(needle, f'RUNTIME_RESTORE_TOOL="{runtime}"', 1)
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def write_test_runtime_restore(self) -> Path:
        path = self.root / "runtime-restore.sh"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "APP_DIR=\"\"; RELEASE=\"\"\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  case \"$1\" in\n"
            "    --app-dir) APP_DIR=\"$2\"; shift 2 ;;\n"
            "    --release) RELEASE=\"$2\"; shift 2 ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "PLATFORM_APP_DIR=\"$APP_DIR\" \"$RELEASE/tools/platform_install_systemd_units.sh\"\n"
            "\"$APP_DIR/shared/venv/bin/python\" \"$RELEASE/tools/platform_install_nginx.py\" --apply\n"
        )
        path.chmod(0o755)
        return path

    def prepare_install_state(
        self, *, with_fake_python: bool = False
    ) -> tuple[Path, Path, Path]:
        current = self.add_release("current")
        previous = self.add_release("previous")
        candidate = self.add_release("candidate")
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        (candidate / ".rollback").mkdir()
        freeze = candidate / "requirements-platform.freeze.txt"
        freeze.write_text("pip==test\n")
        freeze.chmod(0o444)
        (candidate / ".rollback" / "previous-release").write_text(f"{current}\n")
        (candidate / ".rollback" / "previous-release").chmod(0o600)
        (candidate / ".rollback" / "venv-transition").write_text("unchanged\n")
        (candidate / ".rollback" / "venv-transition").chmod(0o600)
        digest = hashlib.sha256(freeze.read_bytes()).hexdigest()
        (candidate / ".rollback" / "shared-freeze.sha256").write_text(f"{digest}\n")
        (candidate / ".rollback" / "shared-freeze.sha256").chmod(0o600)
        self.add_fake_venv(self.shared / "venv", marker="shared")
        if with_fake_python:
            self.write_fake_python(self.shared / "venv" / "bin" / "python")
        self.create_transaction(candidate, current, previous)
        return current, previous, candidate

    def advance_install_state(self, candidate: Path, current: Path, *, phase: str) -> None:
        phases = (
            "venv-transitioned",
            "staged",
            "migration-pending",
            "migration-applied",
            "activation-pending",
            "services-restarted",
            "nginx-applied",
            "smoke-passed",
            "activation-committed",
        )
        for next_phase in phases:
            self.run_transaction("phase", "--expected", self.state_phase(), "--phase", next_phase)
            if next_phase == phase:
                break

    def create_transaction(self, candidate: Path, current: Path, previous: Path) -> None:
        self.run_transaction(
            "create",
            "--operation",
            "install",
            "--app-dir",
            str(self.app_dir),
            "--current-before",
            str(current),
            "--previous-before",
            str(previous),
            "--candidate-release",
            str(candidate),
            "--shared-venv",
            str(self.shared / "venv"),
            "--peer",
            str(self.shared / ".venv-install-candidate.0000"),
            "--snapshot",
            str(candidate / ".rollback" / "shared-venv-before-install"),
            "--transition",
            "none",
        )

    def prepare_rollback_venv(self, current: Path) -> None:
        self.add_fake_venv(self.shared / "venv", marker="new")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        rollback = current / ".rollback"
        rollback.mkdir()
        snapshot = rollback / "shared-venv-before-install"
        shutil.copytree(self.shared / "venv", snapshot)
        (rollback / "previous-release").write_text(f"{self.releases / 'previous'}\n")
        (rollback / "previous-release").chmod(0o600)
        (rollback / "venv-transition").write_text("snapshot\n")
        (rollback / "venv-transition").chmod(0o600)

    def add_runtime_stubs(self, release: Path) -> None:
        tools = release / "tools"
        tools.mkdir(exist_ok=True)
        units = tools / "platform_install_systemd_units.sh"
        units.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$PLATFORM_TEST_UNITS_LABEL\" > \"$PLATFORM_TEST_UNITS_STATE\"\n"
        )
        units.chmod(0o755)
        (tools / "platform_install_nginx.py").write_text("# test stub\n")
        (tools / "platform_deploy_smoke.py").write_text("# test stub\n")
    def add_release(self, name: str) -> Path:
        release = self.releases / name
        release.mkdir()
        return release

    def add_fake_venv(self, venv: Path, *, marker: str) -> None:
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin" / "python"
        python.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' {marker!r}\n")
        python.chmod(0o755)
        (venv / "deps-version").write_text(f"{marker}\n")

    def write_fake_python(self, path: Path) -> None:
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "case \"${1:-}\" in\n"
            "  *platform_install_nginx.py)\n"
            "    if [[ \"${2:-}\" == \"--apply\" ]]; then\n"
            "      printf '%s\\n' \"$PLATFORM_TEST_NGINX_LABEL\" > \"$PLATFORM_TEST_NGINX_STATE\"\n"
            "    fi\n"
            "    ;;\n"
            "esac\n"
        )
        path.chmod(0o755)

    def switch_pointer(self, name: str, target: Path) -> None:
        pointer = self.app_dir / name
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(target)

    def state_phase(self) -> str:
        import json

        return json.loads((self.shared / STATE_NAME).read_text())["phase"]

    def run_transaction(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_script(
            TRANSACTION_TOOL,
            *args,
            "--state",
            str(self.shared / STATE_NAME),
        )

    def copy_script_with_replacement(
        self, source: Path, name: str, needle: str, replacement: str
    ) -> Path:
        script = self.script_with_physical_tools(source)
        self.assertIn(needle, script)
        target = self.root / name
        target.write_text(script.replace(needle, replacement, 1))
        target.chmod(0o755)
        return target

    def copy_abort_script(self, name: str) -> Path:
        script = self.script_with_physical_tools(DEPLOY_SCRIPT)
        runtime_restore = self.root / "test-runtime-restore.sh"
        runtime_restore.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "APP_DIR=\"\"\n"
            "RELEASE=\"\"\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  case \"$1\" in\n"
            "    --app-dir) APP_DIR=\"$2\"; shift 2 ;;\n"
            "    --release) RELEASE=\"$2\"; shift 2 ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "PLATFORM_APP_DIR=\"$APP_DIR\" \"$RELEASE/tools/platform_install_systemd_units.sh\"\n"
            "\"$APP_DIR/shared/venv/bin/python\" \"$RELEASE/tools/platform_install_nginx.py\" --apply\n"
        )
        runtime_restore.chmod(0o755)
        script = script.replace(
            'RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"',
            f'RUNTIME_RESTORE_TOOL="{runtime_restore}"',
            1,
        )
        script = script.replace(
            'PLATFORM_APP_DIR="$APP_DIR" "$APP_DIR/current/tools/platform_install_systemd_units.sh"',
            "/usr/bin/true",
        )
        script = script.replace("/usr/bin/systemctl", "/usr/bin/true")
        script = script.replace("/usr/bin/curl", "/usr/bin/true")
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def script_with_physical_tools(self, source: Path) -> str:
        script = source.read_text()
        needle = 'TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"'
        self.assertIn(needle, script)
        return script.replace(needle, f'TOOLS_DIR="{source.parent}"', 1)

    def run_script(
        self,
        script: Path,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command_env = os.environ.copy()
        command_env.update(env or {})
        result = subprocess.run(
            [str(script), *args],
            cwd=REPO_ROOT,
            env=command_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(f"{script} failed: {result.returncode}\n{result.stderr}")
        return result


if __name__ == "__main__":
    unittest.main()
