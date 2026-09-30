from __future__ import annotations

import contextlib
import ast
import io
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from tests import platform_test_lock_support as lock_support


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = PLATFORM_ROOT / "tools"
SYSTEMD_DIR = PLATFORM_ROOT / "deploy" / "systemd"
WORKFLOW_DIR = PLATFORM_ROOT.parent / ".github" / "workflows"

sys.path.insert(0, str(TOOLS_DIR))
import platform_safe_env_exec as safe_env  # noqa: E402
from tools import platform_nginx_error_summary  # noqa: E402
from tools import platform_media_migration_diagnostics_summary  # noqa: E402
from tools import platform_web_runtime_diagnostics_summary  # noqa: E402
from tools import platform_install_nginx  # noqa: E402


class SafeEnvironmentTests(unittest.TestCase):
    def test_shell_syntax_is_data_not_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_path = Path(temporary) / "runtime.env"
            marker = Path(temporary) / "executed"
            literal = f"$(touch {marker})"
            env_path.write_text(
                "PLATFORM_ENVIRONMENT=production\n"
                f"PLATFORM_SECRET_KEY='{literal}'\n",
                encoding="utf-8",
            )
            env_path.chmod(0o600)

            values = safe_env.load_env_file(env_path)

            self.assertEqual(values["PLATFORM_SECRET_KEY"], literal)
            self.assertFalse(marker.exists())

    def test_duplicate_platform_key_is_rejected(self) -> None:
        with self.assertRaises(safe_env.SafeEnvError):
            safe_env.parse_dotenv(
                b"PLATFORM_ENVIRONMENT=test\nPLATFORM_ENVIRONMENT=production\n"
            )


class ReleaseHardeningContractTests(unittest.TestCase):
    def read_tool(self, name: str) -> str:
        return (TOOLS_DIR / name).read_text(encoding="utf-8")

    @contextlib.contextmanager
    def isolated_release_lock(self, root: Path):
        """Yield a unique root-owned test lock, never the production pathname."""

        test_lock = lock_support.create_test_lock("audit")
        lock_path = test_lock.path
        try:
            tools = root / "release-tools"
            tools.mkdir()
            helper = tools / "platform_release_lock.sh"
            helper_text = self.read_tool("platform_release_lock.sh").replace(
                "/run/lock/oldsparky-platform-release.lock", str(lock_path)
            )
            helper.write_text(helper_text, encoding="utf-8")
            helper.chmod(0o755)
            guard = tools / "platform_release_lock_exec.sh"
            guard_text = self.read_tool(guard.name).replace(
                "/run/lock/oldsparky-platform-release.lock", str(lock_path)
            )
            guard.write_text(guard_text, encoding="utf-8")
            guard.chmod(0o755)
            yield lock_path, helper, guard
        finally:
            test_lock.cleanup()

    def test_runtime_loader_never_sources_env_file(self) -> None:
        runtime_common = self.read_tool("platform_runtime_common.sh")
        self.assertNotIn('source "$PLATFORM_ENV_FILE"', runtime_common)
        self.assertIn("platform_safe_env_exec.py", runtime_common)
        self.assertIn("export-b64", runtime_common)

    def test_deploy_smoke_uses_strict_parser_and_clears_ambient_platform_env(self) -> None:
        smoke = self.read_tool("platform_deploy_smoke.py")
        self.assertIn("platform_safe_env_exec.py", smoke)
        self.assertIn("_SAFE_ENV.load_env_file", smoke)
        self.assertIn("_clear_ambient_platform_environment", smoke)
        self.assertIn('key.startswith(("PLATFORM_", "NEXT_PUBLIC_PLATFORM_"))', smoke)
        self.assertTrue((TOOLS_DIR / "platform_deploy_smoke_impl.py").is_file())

    def test_production_migration_requires_release_transaction_and_quiesces(self) -> None:
        alembic = self.read_tool("platform_run_alembic.sh")
        self.assertIn('"$1" == "upgrade"', alembic)
        self.assertIn('"$2" == "head"', alembic)
        self.assertIn("platform_release_lock.sh", alembic)
        self.assertIn("migration-pending", alembic)
        stop_at = alembic.index("systemctl stop deadlock-api deadlock-worker deadlock-web")
        exec_at = alembic.index('"$PLATFORM_PYTHON_BIN" -m alembic')
        self.assertLess(stop_at, exec_at)
        self.assertIn('SYSTEMCTL_TIMEOUT_BIN="/usr/bin/timeout"', alembic)
        self.assertIn('30s "$SYSTEMCTL_BIN"', alembic)
        self.assertIn('[[ "$status" -eq 3 && "$output" == "inactive" ]]', alembic)
        self.assertIn('if ! read_inactive_state "$service"', alembic)
        self.assertIn('stat -c \'%F:%u:%g:%h:%a\'', alembic)
        self.assertIn("systemctl path metadata is unsafe", alembic)
        self.assertIn('ALEMBIC_TIMEOUT_BIN="/usr/bin/timeout"', alembic)
        self.assertIn('"${ALEMBIC_OPERATION_TIMEOUT_SECONDS}s"', alembic)

    def test_production_alembic_stop_and_state_failures_are_before_python(self) -> None:
        alembic = self.read_tool("platform_run_alembic.sh")
        stop = alembic.index("run_systemctl stop deadlock-api deadlock-worker deadlock-web")
        inactive = alembic.index("read_inactive_state", stop)
        python = alembic.index('"$PLATFORM_PYTHON_BIN" -m alembic')
        self.assertLess(stop, inactive)
        self.assertLess(inactive, python)
        self.assertIn('[[ "$status" -eq 3 && "$output" == "inactive" ]]', alembic)
        self.assertIn('SYSTEMCTL_TIMEOUT_BIN="/usr/bin/timeout"', alembic)
        self.assertIn('"$SYSTEMCTL_TIMEOUT_BIN" --signal=TERM --kill-after=5s 30s', alembic)
        self.assertIn('ALEMBIC_TIMEOUT_BIN="/usr/bin/timeout"', alembic)
        self.assertIn('"${ALEMBIC_OPERATION_TIMEOUT_SECONDS}s"', alembic)
        # rc=1/124 from stop and rc=1/4/124 or non-canonical output from
        # is-active all remain failures; none may reach Alembic.
        self.assertIn("run_systemctl stop", alembic)
        self.assertNotIn("\n  systemctl stop deadlock-api deadlock-worker deadlock-web", alembic)

    def test_current_only_preflight_rejects_stale_systemd_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "platform-app"
            release = app / "releases" / "current-release"
            shared = app / "shared"
            release.mkdir(parents=True)
            shared.mkdir()
            (app / "current").symlink_to(release)
            (shared / ".release-systemd-state.json").write_text("stale\n", encoding="ascii")
            marker = Path(temporary) / "preflight-failure.txt"
            preflight = Path(temporary) / "platform_release_preflight.sh"
            source = (TOOLS_DIR / "platform_release_preflight.sh").read_text(encoding="utf-8")
            self.assertIn("fail() {\n  exit 1\n}", source)
            preflight.write_text(
                source.replace(
                    "fail() {\n  exit 1\n}",
                    f"fail() {{\n  printf '%s\\n' \"$*\" > {str(marker)!r}\n  exit 1\n}}",
                    1,
                ),
                encoding="utf-8",
            )
            preflight.chmod(0o755)
            result = subprocess.run(
                [
                    str(preflight),
                    "--app-dir",
                    str(app),
                    "--allow-no-previous",
                ],
                env={**os.environ, "PLATFORM_APP_DIR": str(app)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                marker.read_text(encoding="utf-8").strip(),
                "--allow-no-previous requires no systemd receipt.",
            )

    def test_preflight_bad_current_pointer_emits_canonical_failure(self) -> None:
        source = (TOOLS_DIR / "platform_release_preflight.sh").read_text(
            encoding="utf-8"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for kind in ("regular", "symlink-to-regular"):
                with self.subTest(kind=kind):
                    app = root / kind / "platform-app"
                    app.mkdir(parents=True)
                    (app / "shared").mkdir()
                    if kind == "regular":
                        (app / "current").write_text("not a pointer\n", encoding="ascii")
                    else:
                        target = app / "not-a-release"
                        target.write_text("not a release\n", encoding="ascii")
                        (app / "current").symlink_to(target)
                    preflight = app / "platform_release_preflight.sh"
                    preflight.write_text(source, encoding="utf-8")
                    preflight.chmod(0o755)
                    result = subprocess.run(
                        [
                            str(preflight),
                            "--app-dir",
                            str(app),
                            "--allow-no-previous",
                        ],
                        env={**os.environ, "PLATFORM_APP_DIR": str(app)},
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 127)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(
                        "RELEASE_PREFLIGHT schema=1 status=failed class=preflight",
                        result.stderr,
                    )

    def test_production_alembic_rejects_adversarial_commands_before_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            for name in (
                "platform_run_alembic.sh",
                "platform_runtime_common.sh",
                "platform_safe_env_exec.py",
            ):
                shutil.copy2(TOOLS_DIR / name, tools / name)
            script = tools / "platform_run_alembic.sh"
            script.chmod(0o755)
            env_file = root / "production.env"
            env_file.write_text("PLATFORM_ENVIRONMENT=production\n", encoding="utf-8")
            env_file.chmod(0o600)
            marker = root / "python-invoked"
            fake_python = root / "python"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                f"touch {marker}\n"
                "exit 99\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            command_env = {
                **os.environ,
                "PLATFORM_ENV_FILE": str(env_file),
                "PLATFORM_PYTHON_BIN": str(fake_python),
            }
            adversarial = (
                ("downgrade", "-1"),
                ("downgrade", "base"),
                ("current",),
                ("history",),
                ("upgrade", "head", "--sql"),
                ("--sql", "upgrade", "head"),
                ("upgrade", "HEAD"),
                ("upgrade", "head\n"),
                ("--", "upgrade", "head"),
            )
            for args in adversarial:
                with self.subTest(args=args):
                    result = subprocess.run(
                        [str(script), *args],
                        cwd=root,
                        env=command_env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("exact command: upgrade head", result.stderr)
                    self.assertFalse(marker.exists())

            test_env_file = root / "test.env"
            test_env_file.write_text("PLATFORM_ENVIRONMENT=test\n", encoding="utf-8")
            test_env_file.chmod(0o600)
            command_env["PLATFORM_ENV_FILE"] = str(test_env_file)
            result = subprocess.run(
                [str(script), "current"],
                cwd=root,
                env=command_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 99, result.stderr)
            self.assertTrue(marker.exists())

    def test_deploy_quiesces_before_stage_and_migration(self) -> None:
        deploy = self.read_tool("platform_release_deploy.sh")
        installer = self.read_tool("platform_release_install.sh")
        abort_workflow = (
            WORKFLOW_DIR / "platform-production-release-abort.yml"
        ).read_text(encoding="utf-8")
        quiesce_call = deploy.index(
            "quiesce_runtime_writers", deploy.index('if [[ "$RESUME" -eq 0 ]]')
        )
        stage = deploy.index('"$INSTALL_TOOL" --stage-only')
        migration = deploy.index("tools/platform_run_alembic.sh upgrade head")
        lock_call = deploy.rindex("acquire_release_lock\n")
        preflight = deploy.index("  release_preflight\n")
        self.assertLess(lock_call, preflight)
        self.assertLess(quiesce_call, stage)
        self.assertLess(stage, migration)
        self.assertIn("release_preflight", deploy[stage:migration])
        self.assertIn("PLATFORM_ENABLE_SYSTEMD_UNITS=0", deploy)
        self.assertNotIn("PLATFORM_RELEASE_LOCK_FD=\"$RELEASE_LOCK_FD\"", deploy)
        self.assertIn("same pathname-form flock", deploy)
        self.assertNotIn("PLATFORM_RELEASE_LOCK_FD", installer)
        self.assertIn("no numeric descriptor is inherited or accepted", installer)
        release_lock_helper = self.read_tool("platform_release_lock.sh")
        self.assertIn(
            "/run/lock/oldsparky-platform-release.lock", release_lock_helper
        )
        for script in (
            deploy,
            installer,
            self.read_tool("platform_release_rollback.sh"),
            self.read_tool("platform_release_restore_runtime.sh"),
        ):
            self.assertIn("platform_release_lock.sh", script)
        self.assertLess(
            deploy.index("platform_release_lock_open"),
            deploy.index('APP_DIR="$(readlink -f'),
        )
        self.assertLess(
            installer.index("platform_release_lock_open"),
            installer.index('if [[ -e "$APP_DIR" || -L "$APP_DIR" ]]'),
        )
        recover_workflow = (
            WORKFLOW_DIR / "platform-production-release-recover.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("Transfer exact attested recovery bundle", recover_workflow)
        self.assertIn('"$bootstrap_tool" install', recover_workflow)
        self.assertIn("--capability recover_pending", recover_workflow)
        self.assertIn("generation_name=\"$bundle_sha\"", recover_workflow)
        self.assertIn("trusted_generation=\"$runtime/shared/.release-recovery/generations/$generation_name\"", recover_workflow)
        self.assertIn("platform_recover_pending.sh", recover_workflow)
        self.assertIn("validate-generation", recover_workflow)
        self.assertIn("set +e", recover_workflow)
        self.assertNotIn("$runtime/current/tools", recover_workflow)
        self.assertNotIn("platform_release_rollback.sh", recover_workflow)
        self.assertNotIn("exec 9<", recover_workflow)
        self.assertNotIn("flock -n 9", recover_workflow)
        self.assertNotIn("PLATFORM_RELEASE_LOCK_FD=9", recover_workflow)
        self.assertIn("ABORT-LEGACY-RELEASE", abort_workflow)
        self.assertIn("platform_recovery_bootstrap.py", abort_workflow)
        self.assertIn("abort_retained_only", abort_workflow)
        self.assertIn("legacy bridge accepts one exact v2 receipt schema", abort_workflow)
        self.assertIn("current_before", abort_workflow)
        self.assertNotIn("platform_release_deploy", abort_workflow)
        self.assertNotIn(
            'systemctl restart deadlock-api deadlock-worker deadlock-web',
            abort_workflow,
        )
        self.assertNotIn(
            'for service in deadlock-api deadlock-worker deadlock-web; do\n'
            '            systemctl is-active --quiet "$service"',
            abort_workflow,
        )

    def test_test_environment_cannot_bypass_canonical_release_lock(self) -> None:
        """A test-looking environment must not redirect a production guard."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.isolated_release_lock(root) as (lock_path, _helper, guard):
                app_dir = root / "platform-app"
                (app_dir / "shared").mkdir(parents=True, mode=0o700)
                (app_dir / "releases").mkdir(mode=0o700)
                release = app_dir / "releases" / "release-under-test"
                release.mkdir(mode=0o700)
                (release / "RELEASE.json").write_text("{}\n", encoding="utf-8")
                (app_dir / "current").symlink_to("releases/release-under-test")
                marker = root / "production-body-ran"
                attacker_lock = app_dir / "attacker.lock"
                runner = root / "run-bypass-regression.sh"
                runner.write_text(
                "#!/usr/bin/env bash\n"
                "set -Eeuo pipefail\n"
                "app_dir=$1\nguard=$2\n"
                "marker=$3\nattacker_lock=$4\nlock_path=$5\n"
                "export PLATFORM_ENVIRONMENT=test PLATFORM_TESTING=1\n"
                "export PLATFORM_TEST_RELEASE_LOCK_PATH=\"$attacker_lock\"\n"
                "set +e\n"
                "/usr/bin/flock -n --close \"$lock_path\" "
                "/bin/bash -c '\n"
                "  set +e\n"
                "  \"$1\" --app-dir \"$2\" -- /usr/bin/touch \"$3\"\n"
                "  status=$?\n"
                "  [[ ! -e \"$3\" ]] || exit 42\n"
                "  exit \"$status\"\n"
                "' bash \"$guard\" \"$app_dir\" \"$marker\"\n"
                "status=$?\n"
                "[[ ! -e \"$marker\" ]] || exit 42\n"
                "exit \"$status\"\n",
                encoding="utf-8",
                )
                runner.chmod(0o755)
                result = subprocess.run(
                    [
                        str(runner),
                        str(app_dir),
                        str(guard),
                        str(marker),
                        str(attacker_lock),
                        str(lock_path),
                    ],
                cwd=root,
                env={
                    **os.environ,
                    "PLATFORM_ENVIRONMENT": "test",
                    "PLATFORM_TESTING": "1",
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                )
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertFalse(marker.exists(), result.stderr)
                self.assertFalse((root / "9").exists(), result.stderr)

    def test_shared_flock_is_not_accepted_as_release_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.isolated_release_lock(root) as (lock_path, helper, _guard):
                runner = root / "run-shared-lock-regression.sh"
                runner.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -Eeuo pipefail\n"
                    "helper=$1\nlock_path=$2\n"
                    "/usr/bin/flock -n -s --close \"$lock_path\" "
                    "/bin/bash -c '\n"
                    "  set -Eeuo pipefail\n"
                    "  source \"$1\"\n"
                    "  if platform_release_lock_supervisor_holds; then exit 42; fi\n"
                    "' bash \"$helper\"\n",
                    encoding="utf-8",
                )
                runner.chmod(0o755)
                result = subprocess.run(
                    [str(runner), str(helper), str(lock_path)],
                    cwd=root,
                    env={
                        **os.environ,
                        "PLATFORM_ENVIRONMENT": "test",
                        "PLATFORM_TESTING": "1",
                    },
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((root / "9").exists(), result.stderr)

    def test_pathname_supervisor_body_has_no_release_fd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_scope = self.isolated_release_lock(root)
            lock_path, helper, _guard = lock_scope.__enter__()
            self.addCleanup(lock_scope.__exit__, None, None, None)
            runner = root / "run-fd-regression.sh"
            runner.write_text(
                "#!/usr/bin/env bash\n"
                "set -Eeuo pipefail\n"
                "helper=$1\n"
                "/usr/bin/env -u PLATFORM_RELEASE_LOCK_FD \\\n"
                "  \"$helper\" --run /bin/bash -c '\n"
                "    set -Eeuo pipefail\n"
                "    for fd in /proc/$$/fd/*; do\n"
                "      target=$(readlink \"$fd\" 2>/dev/null || true)\n"
                "      [[ \"$target\" == /run/lock/oldsparky-platform-release.lock ]] && exit 43\n"
                "    done\n"
                "    source \"$1\"\n"
                "    platform_release_lock_open\n"
                "    platform_release_lock_supervisor_holds\n"
                "  ' bash \"$helper\"\n",
                encoding="utf-8",
            )
            runner.write_text(
                runner.read_text(encoding="utf-8").replace(
                    "/run/lock/oldsparky-platform-release.lock", str(lock_path)
                ),
                encoding="utf-8",
            )
            runner.chmod(0o755)
            result = subprocess.run(
                [
                    str(runner),
                    str(helper),
                ],
                cwd=root,
                env={**os.environ, "PLATFORM_ENVIRONMENT": "test", "PLATFORM_TESTING": "1"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "9").exists(), result.stderr)

    def test_cloudflare_timer_joins_release_lock(self) -> None:
        unit = (SYSTEMD_DIR / "deadlock-cloudflare-ips.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("platform_release_lock_exec.sh", unit)
        self.assertIn("platform_update_cloudflare_ips.py --apply --reload", unit)

    def test_cloudflare_refresh_has_bounded_validation_and_service_timeout(self) -> None:
        updater = self.read_tool("platform_update_cloudflare_ips.py")
        self.assertIn("FETCH_TIMEOUT_MAX_SECONDS = 30.0", updater)
        self.assertIn("SUBPROCESS_TIMEOUT_SECONDS = 30.0", updater)
        self.assertIn("MAX_SUBPROCESS_CALLS = 4", updater)
        self.assertIn("OPERATION_BUDGET_SECONDS", updater)
        self.assertIn("SERVICE_TIMEOUT_SECONDS", updater)
        self.assertIn("timeout=timeout", updater)
        self.assertIn('SYSTEMCTL_BIN = "/usr/bin/systemctl"', updater)
        unit = (SYSTEMD_DIR / "deadlock-cloudflare-ips.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("TimeoutStartSec=210s", unit)

    def test_active_path_subprocesses_and_deploy_marker_are_bounded(self) -> None:
        smoke = self.read_tool("platform_deploy_smoke_impl.py")
        self.assertIn("SYSTEMCTL_TIMEOUT_SECONDS = 30.0", smoke)
        self.assertIn("RUNUSER_TIMEOUT_SECONDS = 30.0", smoke)
        self.assertIn("timeout=SYSTEMCTL_TIMEOUT_SECONDS", smoke)
        self.assertIn("timeout=RUNUSER_TIMEOUT_SECONDS", smoke)
        self.assertIn("DATABASE_CONNECT_TIMEOUT_SECONDS = 30.0", smoke)
        self.assertIn("DATABASE_COMMAND_TIMEOUT_SECONDS = 30.0", smoke)
        self.assertIn("timeout=DATABASE_COMMAND_TIMEOUT_SECONDS", smoke)
        self.assertIn("await asyncio.wait_for", smoke)
        self.assertIn("connection.close()", smoke)
        edge = self.read_tool("platform_validate_edge_policy.py")
        self.assertIn("UFW_COMMAND_TIMEOUT_SECONDS = 30.0", edge)
        self.assertIn("timeout=UFW_COMMAND_TIMEOUT_SECONDS", edge)
        recovery = self.read_tool("platform_recovery_bootstrap.py")
        self.assertIn("RECOVERY_SUBPROCESS_TIMEOUT_SECONDS = 120.0", recovery)
        self.assertIn("start_new_session=True", recovery)
        self.assertIn("os.killpg", recovery)
        dispatcher = self.read_tool("platform_workflow_remote_dispatch.py")
        for value in (
            "DEPLOY_OPERATION_TIMEOUT_SECONDS = 900.0",
            "CLEANUP_OPERATION_TIMEOUT_SECONDS = 300.0",
            "ARTIFACT_PREP_OPERATION_TIMEOUT_SECONDS = 120.0",
            "LIVE_USER_QA_OPERATION_TIMEOUT_SECONDS = 300.0",
            "LIVE_LAUNCH_OPERATION_TIMEOUT_SECONDS = 300.0",
            "start_new_session=True",
            "os.killpg",
            "timeout_seconds=ARTIFACT_PREP_OPERATION_TIMEOUT_SECONDS",
        ):
            self.assertIn(value, dispatcher)
        health = self.read_tool("platform_health_monitor.py")
        self.assertIn("HEALTH_OPERATION_BUDGET_SECONDS", health)
        self.assertIn(
            "HEALTH_SERVICE_TIMEOUT_SECONDS = HEALTH_OPERATION_BUDGET_SECONDS + 35.0",
            health,
        )
        workflow = (WORKFLOW_DIR / "platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("timeout --foreground 300s ssh", workflow)
        self.assertIn("timeout --foreground 300s scp", workflow)
        self.assertIn("timeout --foreground 900s ssh", workflow)
        self.assertIn("ServerAliveInterval=15", workflow)
        self.assertIn("ServerAliveCountMax=3", workflow)
        self.assertIn("/usr/bin/timeout --foreground --signal=TERM --kill-after=5s 60s", workflow)
        self.assertIn("timeout --foreground --signal=TERM --kill-after=5s 30s", workflow)
        self.assertIn("if [[ -z \"$public_line\" ]]; then", workflow)
        self.assertIn(
            "exit 1\n          fi\n          printf '%s\\n' \"$public_line\"",
            workflow,
        )
        for recovery_workflow in (
            WORKFLOW_DIR / "platform-production-recovery-bootstrap-abort.yml",
            WORKFLOW_DIR / "platform-production-release-recover.yml",
        ):
            recovery_source = recovery_workflow.read_text(encoding="utf-8")
            self.assertIn("timeout --foreground 300s ssh", recovery_source)
            self.assertIn("timeout --foreground 300s scp", recovery_source)

        alembic = self.read_tool("platform_run_alembic.sh")
        self.assertIn("PLATFORM_ALEMBIC_OPERATION_GUARDED", alembic)
        self.assertIn('"${ALEMBIC_OPERATION_TIMEOUT_SECONDS}s"', alembic)
        recovery_db = self.read_tool("platform_tournament_list_read_model_recovery.py")
        for value in (
            "RECOVERY_DB_CONNECT_TIMEOUT_SECONDS = 30.0",
            "RECOVERY_DB_COMMAND_TIMEOUT_SECONDS = 30.0",
            '"statement_timeout": RECOVERY_DB_STATEMENT_TIMEOUT_MS',
            '"lock_timeout": RECOVERY_DB_LOCK_TIMEOUT_MS',
        ):
            self.assertIn(value, recovery_db)
        preflight = self.read_tool("platform_release_preflight.sh")
        self.assertIn('DB_OPERATION_TIMEOUT_SECONDS="30"', preflight)
        self.assertIn("PLATFORM_DB_STATEMENT_TIMEOUT_MS=\"30000\"", preflight)
        self.assertIn('"${DB_OPERATION_TIMEOUT_SECONDS}s"', preflight)
        alembic_env = (PLATFORM_ROOT / "alembic/env.py").read_text(encoding="utf-8")
        for value in (
            "ALEMBIC_DB_CONNECT_TIMEOUT_SECONDS = 30.0",
            "ALEMBIC_DB_COMMAND_TIMEOUT_SECONDS = 30.0",
            "ALEMBIC_DB_STATEMENT_TIMEOUT_MS = 30_000",
            "ALEMBIC_DB_LOCK_TIMEOUT_MS = 30_000",
            '"statement_timeout": _bounded_milliseconds(',
            '"lock_timeout": _bounded_milliseconds(',
            "connect_args=alembic_asyncpg_connect_args()",
        ):
            self.assertIn(value, alembic_env)
        self.assertIn("ALEMBIC_DB_TIMEOUT_MAX_SECONDS = 30.0", alembic_env)
        self.assertIn("ALEMBIC_DB_TIMEOUT_MAX_MS = 30_000", alembic_env)
        tree = ast.parse(alembic_env)
        timeout_functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"_bounded_seconds", "_bounded_milliseconds"}
        ]
        namespace = {
            "os": os,
            "math": __import__("math"),
            "ALEMBIC_DB_TIMEOUT_MAX_SECONDS": 30.0,
            "ALEMBIC_DB_TIMEOUT_MAX_MS": 30_000,
        }
        exec(compile(ast.Module(body=timeout_functions, type_ignores=[]), str(PLATFORM_ROOT / "alembic/env.py"), "exec"), namespace)
        with mock.patch.dict(os.environ, {"PLATFORM_ALEMBIC_DB_CONNECT_TIMEOUT_SECONDS": "30.001"}, clear=True):
            with self.assertRaises(RuntimeError):
                namespace["_bounded_seconds"](
                    "PLATFORM_ALEMBIC_DB_CONNECT_TIMEOUT_SECONDS", default=30.0
                )
        with mock.patch.dict(os.environ, {"PLATFORM_ALEMBIC_DB_CONNECT_TIMEOUT_SECONDS": "30"}, clear=True):
            self.assertEqual(
                namespace["_bounded_seconds"](
                    "PLATFORM_ALEMBIC_DB_CONNECT_TIMEOUT_SECONDS", default=30.0
                ),
                30.0,
            )
        with mock.patch.dict(os.environ, {"PLATFORM_ALEMBIC_DB_STATEMENT_TIMEOUT_MS": "30001"}, clear=True):
            with self.assertRaises(RuntimeError):
                namespace["_bounded_milliseconds"](
                    "PLATFORM_ALEMBIC_DB_STATEMENT_TIMEOUT_MS", default=30_000
                )
        with mock.patch.dict(os.environ, {"PLATFORM_ALEMBIC_DB_STATEMENT_TIMEOUT_MS": "30000"}, clear=True):
            self.assertEqual(
                namespace["_bounded_milliseconds"](
                    "PLATFORM_ALEMBIC_DB_STATEMENT_TIMEOUT_MS", default=30_000
                ),
                "30000ms",
            )

    def test_production_supervisor_nginx_validation_is_bounded_and_absolute(self) -> None:
        supervisor = self.read_tool("platform_production_deploy_supervisor.sh")
        self.assertIn('NGINX_BIN="/usr/sbin/nginx"', supervisor)
        self.assertIn('NGINX_TIMEOUT_BIN="/usr/bin/timeout"', supervisor)
        self.assertIn('"${NGINX_CONFIG_TIMEOUT_SECONDS}s"', supervisor)
        self.assertIn("run_nginx_config_test", supervisor)
        self.assertNotIn("nginx -t >/dev/null", supervisor)

    def test_production_supervisor_systemctl_calls_are_bounded_and_strict(self) -> None:
        supervisor = self.read_tool("platform_production_deploy_supervisor.sh")
        self.assertIn('SYSTEMCTL_BIN="/usr/bin/systemctl"', supervisor)
        self.assertIn('SYSTEMCTL_TIMEOUT_BIN="/usr/bin/timeout"', supervisor)
        self.assertIn('"${SYSTEMCTL_TIMEOUT_SECONDS}s" "$SYSTEMCTL_BIN"', supervisor)
        self.assertIn("service_is_active", supervisor)
        self.assertIn("0:active", supervisor)
        self.assertIn("3:inactive", supervisor)
        self.assertNotRegex(supervisor, r"(?m)^\s+systemctl\s+(restart|is-active|show)\b")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_systemctl = root / "systemctl"
            fake_systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "sleep 5\n",
                encoding="ascii",
            )
            fake_systemctl.chmod(0o755)
            start = supervisor.index("run_systemctl()")
            end = supervisor.index("\n}\n", start) + 3
            active_start = supervisor.index("service_is_active()")
            active_end = supervisor.index("\n}\n", active_start) + 3
            probe = (
                "set -u\n"
                f'SYSTEMCTL_BIN={str(fake_systemctl)!r}\n'
                'SYSTEMCTL_TIMEOUT_BIN="/usr/bin/timeout"\n'
                "SYSTEMCTL_TIMEOUT_SECONDS=0.1\n"
                f"{supervisor[start:end]}\n"
                f"{supervisor[active_start:active_end]}\n"
                "service_is_active deadlock-api\n"
                "printf '%s\\n' \"$?\"\n"
            )
            result = subprocess.run(
                ["bash", "-c", probe],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "4")

    def test_platform_logging_systemctl_calls_are_bounded_and_strict(self) -> None:
        logging = self.read_tool("platform_install_logging.sh")
        self.assertIn('SYSTEMCTL_TIMEOUT_BIN="/usr/bin/timeout"', logging)
        self.assertIn('"${SYSTEMCTL_TIMEOUT_SECONDS}s" "$SYSTEMCTL_BIN"', logging)
        self.assertIn("reload_if_active", logging)
        self.assertIn("0:active", logging)
        self.assertIn("3:inactive", logging)
        self.assertNotRegex(logging, r"(?m)^\s+systemctl\s+(is-active|try-reload-or-restart)\b")

    def test_runtime_node_is_exactly_pinned(self) -> None:
        web_unit = (SYSTEMD_DIR / "deadlock-web.service").read_text(
            encoding="utf-8"
        )
        node_helper = self.read_tool("platform_node.sh")
        security_workflow = (WORKFLOW_DIR / "platform-security.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("node-v26.3.1/bin/node", web_unit)
        self.assertIn('REQUIRED_NODE_VERSION="26.3.1"', node_helper)
        self.assertIn('node-version: "26.3.1"', security_workflow)

    def test_systemd_installer_reconciles_retired_units(self) -> None:
        installer = self.read_tool("platform_install_systemd_units.sh")
        self.assertIn("RETIRED_UNITS", installer)
        self.assertIn('rm -f -- "$unit_path"', installer)
        self.assertIn("run_systemctl disable", installer)
        self.assertIn("read_active_state", installer)
        self.assertIn("read_enabled_state", installer)
        self.assertIn("0:active", installer)
        self.assertIn("3:inactive", installer)
        self.assertIn("0:enabled", installer)
        self.assertIn("1:disabled", installer)
        self.assertIn("schema=2", installer)
        self.assertIn("phase=cleanup-pending", installer)
        self.assertIn("RETIRED_SOURCE_DIGEST", installer)
        self.assertIn("RETIRED_BACKUP_IDENTITY", installer)
        self.assertIn("retired_sync_path", installer)
        self.assertIn("os.fsync", installer)
        self.assertIn("retired_reject_orphans", installer)
        self.assertIn("retired_validate_backup_entries", installer)
        self.assertIn("retired_cleanup_pending", installer)
        self.assertIn("retired_reconcile_status_temps", installer)
        self.assertIn("RETIRED_PHASE_INTENDED", installer)
        self.assertIn("RETIRED_DURABLE_PHASE", installer)
        self.assertIn("RETIRED_PHASE_WRITE_FAILED", installer)
        self.assertIn("phase-status-write", installer)
        self.assertIn("phase-status-fsync", installer)
        mutation_start = installer.index(
            'if [[ "${RETIRED_ACTIVE_BEFORE[$unit_name]}" == "active" ]]'
        )
        self.assertLess(installer.index("prepare_retired_transaction"), mutation_start)
        self.assertLess(installer.index("write_retired_rollback_status"), mutation_start)
        self.assertNotIn("is-active --quiet", installer)
        self.assertNotIn("is-enabled --quiet", installer)

    def test_systemd_installer_retired_cleanup_is_bounded_and_transactional(self) -> None:
        installer_source = self.read_tool("platform_install_systemd_units.sh")
        # Keep the production guard tied to /etc/systemd/system; this isolated
        # subprocess replaces only that literal so the fake destination can
        # exercise the same cleanup transaction without widening production
        # path authority.
        installer_source = installer_source.replace(
            'if [[ "$SYSTEMD_DEST_DIR" == "/etc/systemd/system" && "$ENABLE_SYSTEMD_UNITS" == "1" ]]; then',
            'if [[ "$ENABLE_SYSTEMD_UNITS" == "1" ]]; then',
            1,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "platform" / "tools"
            units = root / "platform" / "deploy" / "systemd"
            destination = root / "etc" / "systemd" / "system"
            app_dir = root / "app"
            tools.mkdir(parents=True)
            units.mkdir(parents=True)
            destination.mkdir(parents=True)
            app_dir.mkdir()
            timeout_wrapper = root / "timeout-wrapper"
            timeout_wrapper.write_text(
                "#!/usr/bin/env bash\n"
                "set -u\n"
                "log=${FAKE_TIMEOUT_LOG:-}\n"
                "args=(\"$@\")\n"
                "if [[ \"${FAKE_STOP_TIMEOUT:-0}\" == 1 || \"${FAKE_DISABLE_TIMEOUT:-0}\" == 1 ]]; then\n"
                "  args[2]=\"${FAKE_TEST_TIMEOUT_SECONDS:-1}s\"\n"
                "fi\n"
                "if [[ -n \"$log\" ]]; then\n"
                "  { printf 'start command='; printf '%q ' \"${args[@]}\"; printf '\\n'; } >> \"$log\"\n"
                "fi\n"
                "/usr/bin/timeout \"${args[@]}\"\n"
                "status=$?\n"
                "if [[ -n \"$log\" ]]; then\n"
                "  { printf 'exit=%s command=' \"$status\"; printf '%q ' \"${args[@]}\"; printf '\\n'; } >> \"$log\"\n"
                "fi\n"
                "exit \"$status\"\n",
                encoding="ascii",
            )
            timeout_wrapper.chmod(0o755)
            installer_source = installer_source.replace(
                'SYSTEMCTL_TIMEOUT_BIN="/usr/bin/timeout"',
                f'SYSTEMCTL_TIMEOUT_BIN="{timeout_wrapper}"',
                1,
            )
            installer = tools / "platform_install_systemd_units.sh"
            installer.write_text(installer_source, encoding="utf-8")
            installer.chmod(0o755)
            for unit in SYSTEMD_DIR.glob("deadlock-*.service"):
                shutil.copy2(unit, units / unit.name)
            for unit in SYSTEMD_DIR.glob("deadlock-*.timer"):
                shutil.copy2(unit, units / unit.name)
            for name in ("platform_install_logging.sh", "platform_prepare_service_user.sh"):
                stub = tools / name
                stub.write_text("#!/usr/bin/env bash\nset -eu\nexit 0\n", encoding="ascii")
                stub.chmod(0o755)
            fake_systemctl = root / "systemctl"
            reload_count = root / "daemon-reload-count"
            fake_systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "action=${1:-}\n"
                "printf '%s\\n' \"$action\" >> \"${FAKE_SYSTEMCTL_LOG}\"\n"
                "case \"$action\" in\n"
                "  is-active)\n"
                "    if [[ -n \"${FAKE_ACTIVE_RC:-}\" ]]; then printf '%s\\n' inactive; exit \"$FAKE_ACTIVE_RC\"; fi\n"
                "    state=inactive; test ! -e \"$FAKE_ACTIVE_STATE\" || state=$(cat \"$FAKE_ACTIVE_STATE\"); printf '%s\\n' \"$state\"; [[ \"$state\" == active ]] && exit 0 || exit 3\n"
                "    ;;\n"
                "  is-enabled)\n"
                "    if [[ -n \"${FAKE_ENABLED_RC:-}\" ]]; then printf '%s\\n' disabled; exit \"$FAKE_ENABLED_RC\"; fi\n"
                "    state=disabled; test ! -e \"$FAKE_ENABLED_STATE\" || state=$(cat \"$FAKE_ENABLED_STATE\"); printf '%s\\n' \"$state\"; [[ \"$state\" == enabled ]] && exit 0 || exit 1\n"
                "    ;;\n"
                "  daemon-reload) count=0; test ! -e \"$FAKE_RELOAD_COUNT\" || count=$(cat \"$FAKE_RELOAD_COUNT\"); count=$((count + 1)); printf '%s' \"$count\" > \"$FAKE_RELOAD_COUNT\"; if [[ \"${FAKE_PAUSE_RELOAD_COUNT:-0}\" == \"$count\" ]]; then touch \"$FAKE_PAUSE_MARKER\"; sleep 5; fi; test \"$count\" != \"${FAKE_FAIL_RELOAD_AT:-0}\" ;;\n"
                "  stop)\n"
                "    [[ \"${FAKE_STOP_TIMEOUT:-0}\" != 1 ]] || sleep 5\n"
                "    [[ \"${FAKE_STOP_RC:-0}\" -eq 0 ]] || exit \"$FAKE_STOP_RC\"\n"
                "    [[ \"${FAKE_PAUSE_BEFORE_ACTION:-}\" != stop ]] || { touch \"$FAKE_PAUSE_MARKER\"; sleep 5; }\n"
                "    printf '%s' inactive > \"$FAKE_ACTIVE_STATE\"; if [[ \"${FAKE_PAUSE_ACTION:-}\" == stop ]]; then touch \"$FAKE_PAUSE_MARKER\"; sleep 5; fi; exit 0\n"
                "    ;;\n"
                "  disable)\n"
                "    [[ \"${FAKE_DISABLE_TIMEOUT:-0}\" != 1 ]] || sleep 5\n"
                "    [[ \"${FAKE_DISABLE_RC:-0}\" -eq 0 ]] || exit \"$FAKE_DISABLE_RC\"\n"
                "    printf '%s' disabled > \"$FAKE_ENABLED_STATE\"; if [[ \"${FAKE_PAUSE_ACTION:-}\" == disable ]]; then touch \"$FAKE_PAUSE_MARKER\"; sleep 5; fi; exit 0\n"
                "    ;;\n"
                "  start) [[ \"${FAKE_RESTORE_FAIL_ACTION:-}\" != start || -e \"${FAKE_RESTORE_FAIL_MARKER:-/nonexistent}\" ]] || { touch \"$FAKE_RESTORE_FAIL_MARKER\"; exit 4; }; printf '%s' active > \"$FAKE_ACTIVE_STATE\"; exit 0 ;;\n"
                "  enable) [[ \"${FAKE_RESTORE_FAIL_ACTION:-}\" != enable || -e \"${FAKE_RESTORE_FAIL_MARKER:-/nonexistent}\" ]] || { touch \"$FAKE_RESTORE_FAIL_MARKER\"; exit 4; }; printf '%s' enabled > \"$FAKE_ENABLED_STATE\"; exit 0 ;;\n"
                "  restart) exit 0 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n",
                encoding="ascii",
            )
            fake_systemctl.chmod(0o755)

            def run_case(
                *, reset_state: bool = True, create_retired: bool = True,
                kill_point: str | None = None, **overrides: str
            ) -> tuple[subprocess.CompletedProcess[str], Path]:
                retired = destination / "deadlock-retired.service"
                if create_retired:
                    retired.write_text("[Unit]\nDescription=retired\n", encoding="ascii")
                    retired.chmod(0o644)
                environment = {
                    **os.environ,
                    "PLATFORM_SYSTEMD_DIR": str(destination),
                    "PLATFORM_APP_DIR": str(app_dir),
                    "PLATFORM_SYSTEMCTL_BIN": str(fake_systemctl),
                    "PLATFORM_ENABLE_SYSTEMD_UNITS": "1",
                    "FAKE_RELOAD_COUNT": str(reload_count),
                    "FAKE_ACTIVE_STATE": str(root / "active-state"),
                    "FAKE_ENABLED_STATE": str(root / "enabled-state"),
                    "FAKE_SYSTEMCTL_LOG": str(root / "systemctl.log"),
                    "FAKE_TIMEOUT_LOG": str(root / "timeout.log"),
                    **overrides,
                }
                pause_marker = root / "pause-marker"
                pause_marker.unlink(missing_ok=True)
                if kill_point == "record":
                    environment["FAKE_PAUSE_BEFORE_ACTION"] = "stop"
                    environment["FAKE_PAUSE_MARKER"] = str(pause_marker)
                elif kill_point in {"stop", "disable"}:
                    environment["FAKE_PAUSE_ACTION"] = kill_point
                    environment["FAKE_PAUSE_MARKER"] = str(pause_marker)
                elif kill_point == "remove":
                    environment["FAKE_PAUSE_RELOAD_COUNT"] = "2"
                    environment["FAKE_PAUSE_MARKER"] = str(pause_marker)
                elif kill_point == "new-reload":
                    environment["FAKE_PAUSE_RELOAD_COUNT"] = "1"
                    environment["FAKE_PAUSE_MARKER"] = str(pause_marker)
                elif kill_point in {
                    "cleanup-phase-record",
                    "backup-unlink",
                    "backup-rmdir",
                    "status-clear",
                    "status-temp-write",
                    "status-temp-rename",
                    "restore-temp-write",
                    "restore-temp-rename",
                }:
                    environment["PLATFORM_TEST_RETIRED_PAUSE_POINT"] = kill_point
                    environment["PLATFORM_TEST_RETIRED_PAUSE_MARKER"] = str(pause_marker)
                if reset_state:
                    reload_count.unlink(missing_ok=True)
                    for state_path in (
                        Path(environment["FAKE_ACTIVE_STATE"]),
                        Path(environment["FAKE_ENABLED_STATE"]),
                        Path(environment["FAKE_SYSTEMCTL_LOG"]),
                    ):
                        state_path.unlink(missing_ok=True)
                    if "FAKE_INITIAL_ACTIVE" in overrides:
                        Path(environment["FAKE_ACTIVE_STATE"]).write_text(
                            overrides["FAKE_INITIAL_ACTIVE"], encoding="ascii"
                        )
                    if "FAKE_INITIAL_ENABLED" in overrides:
                        Path(environment["FAKE_ENABLED_STATE"]).write_text(
                            overrides["FAKE_INITIAL_ENABLED"], encoding="ascii"
                        )
                if kill_point is None:
                    result = subprocess.run(
                        [str(installer)],
                        cwd=root,
                        env=environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        timeout=20,
                    )
                else:
                    process = subprocess.Popen(
                        [str(installer)],
                        cwd=root,
                        env=environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        start_new_session=True,
                    )

                    def kill_process_group() -> None:
                        # The installer, timeout wrapper and fake systemctl
                        # share this session.  Kill only that exact group so
                        # a deliberately interrupted transaction cannot leak
                        # a sleeping child into the retry.
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass

                    def reap_killed_process() -> tuple[str, str]:
                        try:
                            return process.communicate(timeout=10)
                        except subprocess.TimeoutExpired:
                            kill_process_group()
                            return process.communicate(timeout=10)

                    deadline = time.monotonic() + 10
                    while not pause_marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if not pause_marker.exists():
                        kill_process_group()
                        stdout, stderr = reap_killed_process()
                        self.fail(f"installer did not reach kill point {kill_point}: {stderr}")
                    kill_process_group()
                    stdout, stderr = reap_killed_process()
                    result = subprocess.CompletedProcess(
                        [str(installer)], -9, stdout, stderr
                    )
                return result, retired

            for variable, value in (
                ("FAKE_ACTIVE_RC", "4"),
                ("FAKE_ACTIVE_RC", "124"),
                ("FAKE_ENABLED_RC", "4"),
                ("FAKE_ENABLED_RC", "124"),
            ):
                with self.subTest(variable=variable, value=value):
                    result, retired = run_case(**{variable: value})
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(retired.exists(), result.stderr)

            result, retired = run_case(
                FAKE_FAIL_RELOAD_AT="1",
                FAKE_INITIAL_ACTIVE="active",
                FAKE_INITIAL_ENABLED="enabled",
            )
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertTrue(retired.exists(), result.stderr)
            self.assertEqual((root / "active-state").read_text(encoding="ascii"), "active")
            self.assertEqual((root / "enabled-state").read_text(encoding="ascii"), "enabled")

            result, retired = run_case(
                FAKE_FAIL_RELOAD_AT="2",
                FAKE_INITIAL_ACTIVE="active",
                FAKE_INITIAL_ENABLED="enabled",
            )
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertTrue(retired.exists(), result.stderr)
            self.assertEqual((root / "active-state").read_text(encoding="ascii"), "active")
            self.assertEqual((root / "enabled-state").read_text(encoding="ascii"), "enabled")

            result, retired = run_case(
                FAKE_INITIAL_ACTIVE="active", FAKE_INITIAL_ENABLED="enabled"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(retired.exists(), result.stderr)
            log = (root / "systemctl.log").read_text(encoding="ascii").splitlines()
            self.assertIn("stop", log)
            self.assertIn("disable", log)
            self.assertGreaterEqual(log.count("is-active"), 2)
            self.assertGreaterEqual(log.count("is-enabled"), 2)

            for failure in (
                {"FAKE_INITIAL_ACTIVE": "active", "FAKE_STOP_RC": "4"},
                {"FAKE_INITIAL_ACTIVE": "active", "FAKE_STOP_TIMEOUT": "1"},
                {"FAKE_INITIAL_ENABLED": "enabled", "FAKE_DISABLE_RC": "4"},
                {"FAKE_INITIAL_ENABLED": "enabled", "FAKE_DISABLE_TIMEOUT": "1"},
            ):
                with self.subTest(retired_failure=failure):
                    result, retired = run_case(**failure)
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(retired.exists(), result.stderr)

            result, retired = run_case()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(retired.exists(), result.stderr)
            durable_status = destination / ".oldsparky-retired-rollback-status"

            # A phase receipt is only authoritative after its replacement has
            # been atomically renamed and fsynced.  Inject failure on each
            # side of that boundary: the EXIT rollback must restore the
            # retired file and exact active/enabled states, and must retain
            # the active receipt plus backup for a retry.
            for phase_failure in ("phase-status-write", "phase-status-fsync"):
                with self.subTest(phase_failure=phase_failure):
                    phase_failure_marker = root / f"{phase_failure}.marker"
                    phase_failure_marker.unlink(missing_ok=True)
                    result, retired = run_case(
                        FAKE_INITIAL_ACTIVE="active",
                        FAKE_INITIAL_ENABLED="enabled",
                        PLATFORM_TEST_RETIRED_FAIL_POINT=phase_failure,
                        PLATFORM_TEST_RETIRED_FAIL_MARKER=str(phase_failure_marker),
                    )
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(retired.exists(), result.stderr)
                    self.assertTrue(durable_status.exists(), result.stderr)
                    self.assertIn(
                        "phase=active\n",
                        durable_status.read_text(encoding="ascii"),
                    )
                    self.assertTrue(
                        list(destination.glob(".oldsparky-retired.*")),
                        result.stderr,
                    )
                    self.assertEqual(
                        (root / "active-state").read_text(encoding="ascii"),
                        "active",
                    )
                    self.assertEqual(
                        (root / "enabled-state").read_text(encoding="ascii"),
                        "enabled",
                    )
                    retry, retired = run_case(
                        reset_state=False,
                        create_retired=False,
                    )
                    self.assertEqual(retry.returncode, 0, retry.stderr)
                    self.assertFalse(retired.exists(), retry.stderr)
                    self.assertFalse(durable_status.exists(), retry.stderr)
                    self.assertFalse(
                        list(destination.glob(".oldsparky-retired.*")),
                        retry.stderr,
                    )

            rollback_marker = root / "retired-rollback-failed"
            result, retired = run_case(
                FAKE_FAIL_RELOAD_AT="1",
                FAKE_INITIAL_ACTIVE="active",
                FAKE_INITIAL_ENABLED="enabled",
                FAKE_RESTORE_FAIL_ACTION="start",
                FAKE_RESTORE_FAIL_MARKER=str(rollback_marker),
            )
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertTrue(retired.exists(), result.stderr)
            self.assertTrue(durable_status.exists(), result.stderr)
            result, retired = run_case(
                reset_state=False,
                create_retired=False,
                FAKE_FAIL_RELOAD_AT="0",
                FAKE_RESTORE_FAIL_ACTION="start",
                FAKE_RESTORE_FAIL_MARKER=str(rollback_marker),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(retired.exists(), result.stderr)
            self.assertFalse(durable_status.exists(), result.stderr)

            # A SIGKILL is deliberately injected at every durable transaction
            # boundary.  Retry must use the exact record/backup to restore the
            # retired file and both systemd states before beginning a fresh
            # install; no EXIT trap is available to help the killed process.
            for kill_point in ("record", "stop", "disable", "remove", "new-reload"):
                with self.subTest(kill_point=kill_point):
                    result, retired = run_case(
                        kill_point=kill_point,
                        FAKE_INITIAL_ACTIVE="active",
                        FAKE_INITIAL_ENABLED="enabled",
                    )
                    self.assertEqual(result.returncode, -9, result.stderr)
                    self.assertTrue(durable_status.exists(), result.stderr)
                    retry, retired = run_case(
                        reset_state=False,
                        create_retired=False,
                    )
                    self.assertEqual(retry.returncode, 0, retry.stderr)
                    self.assertFalse(retired.exists(), retry.stderr)
                    self.assertFalse(durable_status.exists(), retry.stderr)

            for kill_point in (
                "cleanup-phase-record",
                "backup-unlink",
                "backup-rmdir",
                "status-clear",
                "status-temp-write",
                "status-temp-rename",
            ):
                with self.subTest(cleanup_kill_point=kill_point):
                    result, retired = run_case(
                        kill_point=kill_point,
                        FAKE_INITIAL_ACTIVE="active",
                        FAKE_INITIAL_ENABLED="enabled",
                    )
                    self.assertEqual(result.returncode, -9, result.stderr)
                    retry, retired = run_case(
                        reset_state=False,
                        create_retired=False,
                    )
                    self.assertEqual(retry.returncode, 0, retry.stderr)
                    self.assertFalse(retired.exists(), retry.stderr)
                    self.assertFalse(durable_status.exists(), retry.stderr)

            for kill_point in ("restore-temp-write", "restore-temp-rename"):
                with self.subTest(restore_kill_point=kill_point):
                    result, retired = run_case(
                        kill_point=kill_point,
                        FAKE_FAIL_RELOAD_AT="1",
                        FAKE_INITIAL_ACTIVE="active",
                        FAKE_INITIAL_ENABLED="enabled",
                    )
                    self.assertEqual(result.returncode, -9, result.stderr)
                    self.assertTrue(durable_status.exists(), result.stderr)
                    retry, retired = run_case(
                        reset_state=False,
                        create_retired=False,
                    )
                    self.assertEqual(retry.returncode, 0, retry.stderr)
                    self.assertFalse(retired.exists(), retry.stderr)
                    self.assertFalse(durable_status.exists(), retry.stderr)

            timeout_log = (root / "timeout.log").read_text(encoding="ascii")
            timeout_lines = timeout_log.splitlines()
            self.assertTrue(
                any(line.startswith("start command=") for line in timeout_lines),
                timeout_log,
            )
            self.assertTrue(
                any(
                    line.startswith("exit=0 command=") and "30s" in line
                    for line in timeout_lines
                ),
                timeout_log,
            )
            self.assertTrue(
                any(
                    line.startswith("exit=124 command=") and "1s" in line
                    for line in timeout_lines
                ),
                timeout_log,
            )

    def test_release_systemctl_mutations_use_trusted_bounded_wrapper(self) -> None:
        for name in (
            "platform_install_systemd_units.sh",
            "platform_release_restore_runtime.sh",
            "platform_recover_pending.sh",
        ):
            text = self.read_tool(name)
            with self.subTest(tool=name):
                self.assertIn('SYSTEMCTL_TIMEOUT_BIN="/usr/bin/timeout"', text)
                self.assertIn(
                    '"$SYSTEMCTL_TIMEOUT_BIN" --signal=TERM --kill-after=5s 30s',
                    text,
                )
                self.assertIn("run_systemctl", text)
        recovery = self.read_tool("platform_recover_pending.sh")
        self.assertIn("inactive:3:inactive", recovery)
        self.assertNotIn("inactive) ! run_systemctl", recovery)

    def test_nginx_reload_uses_bounded_timeout_and_fails_closed(self) -> None:
        source = self.read_tool("platform_install_nginx.py")
        self.assertIn("SYSTEMCTL_RELOAD_TIMEOUT_SECONDS = 30.0", source)
        self.assertIn("NGINX_CONFIG_TIMEOUT_SECONDS = 30.0", source)
        self.assertEqual(source.count("timeout=OPENSSL_TIMEOUT_SECONDS"), 4)
        self.assertEqual(source.count("timeout=NGINX_CONFIG_TIMEOUT_SECONDS"), 2)
        self.assertEqual(source.count("timeout=SYSTEMCTL_RELOAD_TIMEOUT_SECONDS"), 2)
        captured: dict[str, object] = {}

        def timed_out(command: list[str], **kwargs: object) -> object:
            captured["command"] = command
            captured.update(kwargs)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with mock.patch.object(platform_install_nginx.subprocess, "run", side_effect=timed_out):
            with self.assertRaisesRegex(RuntimeError, "Command timed out safely after 30s"):
                platform_install_nginx.run_checked(
                    ["systemctl", "reload", "nginx.service"],
                    timeout=platform_install_nginx.SYSTEMCTL_RELOAD_TIMEOUT_SECONDS,
                )
        self.assertEqual(captured["command"], ["systemctl", "reload", "nginx.service"])
        self.assertEqual(captured["timeout"], 30.0)

    def test_recovery_nginx_config_checks_are_bounded_and_fail_closed(self) -> None:
        for name in ("platform_recover_pending.sh", "platform_release_restore_runtime.sh"):
            source = self.read_tool(name)
            with self.subTest(tool=name):
                self.assertIn('NGINX_TIMEOUT_BIN="/usr/bin/timeout"', source)
                self.assertIn('NGINX_CONFIG_TIMEOUT_SECONDS=30', source)
                self.assertIn("run_nginx_config_test", source)
                self.assertIn('"${NGINX_CONFIG_TIMEOUT_SECONDS}s"', source)

    def test_production_logging_avoids_duplicate_access_and_worker_info_streams(self) -> None:
        api_runner = self.read_tool("platform_run_api.sh")
        worker_runner = self.read_tool("platform_run_worker.sh")
        self.assertIn("PLATFORM_GUNICORN_ACCESS_LOG", api_runner)
        self.assertIn("PLATFORM_WORKER_LOG_LEVEL", worker_runner)

    def test_standalone_cache_path_is_prepared_and_smoked_without_old_path(self) -> None:
        preparer = self.read_tool("platform_prepare_service_user.sh")
        build = self.read_tool("platform_build_release.sh")
        smoke = self.read_tool("platform_deploy_smoke_impl.py")
        standalone_path = (
            "apps/platform_web/.next/standalone/.next/cache"
        )
        old_path = "apps/platform_web/.next/cache"

        self.assertIn(f'WEB_CACHE_DIR="$APP_DIR/current/{standalone_path}"', preparer)
        self.assertIn('runuser -u oldsparky-web -- test -w "$WEB_CACHE_DIR"', preparer)
        self.assertIn('if [[ -L "$WEB_CACHE_DIR" ]]', preparer)
        self.assertLess(
            preparer.index('if [[ -L "$WEB_CACHE_DIR" ]]'),
            preparer.index('install -d -o oldsparky-web -g oldsparky-web -m 0750 "$WEB_CACHE_DIR"'),
        )
        self.assertIn("rm -rf .next/standalone/.next/cache", build)
        self.assertIn(
            'git -C "$REPO_ROOT" archive --format=tar "$SOURCE_GIT_COMMIT" platform',
            build,
        )
        self.assertIn("WEB_RUNTIME_CACHE_RELATIVE", smoke)
        self.assertIn("release_standalone_cache_sandbox", smoke)
        self.assertIn("mode != 0o750", smoke)
        self.assertIn('"systemctl",', smoke)
        self.assertIn('"runuser",', smoke)
        self.assertNotIn(f'WEB_CACHE_DIR="$APP_DIR/current/{old_path}"', preparer)

    def test_nginx_outage_diagnostics_are_bounded_and_summary_only(self) -> None:
        workflow = (
            WORKFLOW_DIR / "platform-production-web-runtime-diagnostics.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("nginx_properties", workflow)
        self.assertIn("nginx_error_summary", workflow)
        self.assertIn("web_journal_summary", workflow)
        self.assertIn("journalctl -u deadlock-web", workflow)
        self.assertIn("-n 600", workflow)
        self.assertIn("nginx_error_log=/var/log/nginx/error.log", workflow)
        self.assertIn('tail -n 300 -- "$nginx_error_log"', workflow)
        self.assertIn("platform_nginx_error_summary.py", workflow)
        self.assertIn("platform_web_runtime_diagnostics_summary.py", workflow)
        self.assertIn('"kind":"web_runtime_diagnostics"', workflow)
        keyscan_start = workflow.index(
            "      - name: Configure pinned production SSH"
        )
        collection_start = workflow.index(
            "      - name: Collect exact-window read-only evidence"
        )
        cleanup_start = workflow.index("      - name: Remove production SSH material")
        upload_start = workflow.index("      - name: Upload aggregate diagnostic evidence")
        self.assertNotIn('"status":"unavailable"', workflow)
        self.assertIn("collect_command_output", workflow)
        self.assertIn("producer_status", workflow)
        self.assertIn("producer failed with status", workflow)
        self.assertNotIn("|| true", workflow[collection_start:cleanup_start])
        self.assertIn('test -f "$nginx_error_log"', workflow)
        self.assertNotIn('cat "$nginx_error_log"', workflow)

        collect_start = workflow.index("  collect:")
        collect_end = workflow.index("\n  ", collect_start + 3)
        collect_job = workflow[collect_start:collect_end]
        self.assertNotRegex(collect_job, r"secrets\.PROD_SSH_(?:HOST|USER|KEY)")

        keyscan = workflow[keyscan_start:collection_start]
        self.assertIn("PROD_SSH_HOST:", keyscan)
        self.assertIn("PROD_SSH_KEY:", keyscan)
        self.assertNotIn("PROD_SSH_USER:", keyscan)

        collection = workflow[collection_start:cleanup_start]
        cleanup = workflow[cleanup_start:upload_start]
        self.assertIn("PROD_SSH_HOST:", collection)
        self.assertIn("PROD_SSH_USER:", collection)
        self.assertNotIn("PROD_SSH_KEY:", collection)
        self.assertIn('"$ssh_dir/id_ed25519"', cleanup)
        self.assertIn('"$ssh_dir/control.sock"', cleanup)
        self.assertIn('test ! -e "$ssh_dir/control.sock"', cleanup)
        self.assertIn('test ! -L "$ssh_dir"', cleanup)
        self.assertIn("SSH_DIR=", cleanup)
        upload = workflow[upload_start:]
        self.assertIn("id: cleanup_ssh", cleanup)
        self.assertIn(
            "if: ${{ always() && steps.cleanup_ssh.outcome == 'success' }}",
            upload,
        )
        self.assertIn("if-no-files-found: error", upload)
        self.assertNotIn("PROD_SSH_", upload)
        self.assertNotIn("secrets.", upload)
        self.assertNotIn("SSH_DIR", upload)

        self.assertNotIn("systemctl status deadlock-web", workflow)
        self.assertNotIn("web_cgroup_status", workflow)
        self.assertNotIn("active_release=", workflow)
        self.assertNotIn("sed -E", workflow[collection_start:cleanup_start])

    def test_runtime_diagnostic_summary_is_fixed_and_redacts_adversarial_lines(self) -> None:
        lines = (
            b'2026-09-11T10:00:00+00:00 web[1]: error request="GET /private?token=secret" '
            b'client=192.0.2.4 host=private.example command=/bin/sh -c secret\n',
            b"2026-09-11T10:00:01+00:00 web[1]: SIGTERM shutdown\n",
            b"2026-09-11T10:00:02+00:00 kernel: Out of memory: Killed process 42\n",
        )

        journal = platform_web_runtime_diagnostics_summary.summarize_log_lines(
            lines, kind="web_journal"
        )
        kernel = platform_web_runtime_diagnostics_summary.summarize_log_lines(
            lines, kind="kernel_oom"
        )
        properties = platform_web_runtime_diagnostics_summary.summarize_properties(
            (
                b"ActiveState=active\n",
                b"Result=success\n",
                b"ExecMainStatus=143\n",
                b"RestartUSec=5s\n",
                b"ExecMainStartTimestamp=Thu 2026-09-11 10:00:00 UTC\n",
                b"ExecMainExitTimestamp=https://private.example/?at=2027-01-01T00:00:00Z\n",
                b"Environment=PLATFORM_SECRET_KEY=do-not-return\n",
                b"ExecStart=/private/command-line\n",
            ),
            service="web",
        )

        encoded = json.dumps(
            {"journal": journal, "kernel": kernel, "properties": properties},
            sort_keys=True,
        )
        for secret in (
            "192.0.2.4",
            "private.example",
            "/private",
            "token=secret",
            "do-not-return",
            "/private/command-line",
            "2027-01-01T00:00:00Z",
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(journal["class_counts"]["error"], 1)
        self.assertEqual(journal["class_counts"]["shutdown"], 1)
        self.assertEqual(kernel["class_counts"]["oom"], 1)
        self.assertEqual(properties["status"], "ok")
        self.assertEqual(properties["properties"]["ActiveState"], "active")
        self.assertEqual(properties["properties"]["ExecMainStatus"], 143)
        self.assertEqual(properties["properties"]["RestartUSec"], "5s")
        self.assertIsNone(properties["properties"]["ExecMainExitTimestamp"])
        self.assertEqual(
            platform_web_runtime_diagnostics_summary.summarize_log_lines(
                (), kind="web_journal"
            )["status"],
            "empty",
        )
        self.assertEqual(platform_nginx_error_summary.summarize_lines(())["status"], "empty")
        url_only = platform_web_runtime_diagnostics_summary.summarize_log_lines(
            (b'web error request="GET https://private.example/2027-01-01T00:00:00Z?query=secret"\n',),
            kind="web_journal",
        )
        self.assertIsNone(url_only["first_timestamp"])

    def test_nginx_error_summary_never_returns_request_or_client_data(self) -> None:
        lines = (
            b'2026/09/11 10:00:00 [error] 1#1: *1 connect() failed '
            b'(111: Connection refused) while connecting to upstream, '
            b'client: 192.0.2.1, server: old-sparky.com, '
            b'request: "GET /private?token=do-not-return HTTP/1.1", '
            b'upstream: "http://127.0.0.1:3000/private", '
            b'host: "old-sparky.com"\n',
            b'2026/09/11 10:00:01 [warn] 1#1: *2 upstream timed out, '
            b'client: 198.51.100.7, request: "GET /secret"\n',
        )

        payload = platform_nginx_error_summary.summarize_lines(lines)
        encoded = json.dumps(payload, sort_keys=True)

        self.assertEqual(payload["line_count"], 2)
        self.assertEqual(
            payload["error_class_counts"],
            {
                "client_closed": 0,
                "client_timeout": 0,
                "other": 0,
                "upstream_connect_failed": 1,
                "upstream_invalid_response": 0,
                "upstream_premature_close": 0,
                "upstream_reset": 0,
                "upstream_timeout": 1,
                "worker_process": 0,
            },
        )
        self.assertNotIn("192.0.2.1", encoded)
        self.assertNotIn("old-sparky.com", encoded)
        self.assertNotIn("/private", encoded)
        self.assertNotIn("do-not-return", encoded)
        self.assertEqual(
            platform_nginx_error_summary._severity(b"[request-token] arbitrary"),
            "unknown",
        )

    def test_nginx_error_summary_enforces_a_hard_line_bound(self) -> None:
        payload = platform_nginx_error_summary.summarize_lines(
            (b"2026/09/11 10:00:00 [error] other\n" for _ in range(301))
        )

        self.assertEqual(payload["line_count"], 300)
        self.assertTrue(payload["truncated"])

        giant_line = io.BytesIO(b"x" * (platform_nginx_error_summary.MAX_INPUT_BYTES * 2))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(platform_nginx_error_summary.main([], stream=giant_line), 0)
        bounded = json.loads(output.getvalue())
        self.assertEqual(bounded["line_count"], 1)
        self.assertEqual(bounded["overlong_lines"], 1)
        self.assertLessEqual(
            giant_line.tell(), platform_nginx_error_summary.MAX_INPUT_BYTES
        )

    def test_operator_rollback_cannot_complete_without_restart_and_smoke(self) -> None:
        rollback = self.read_tool("platform_release_rollback.sh")
        self.assertIn("Production rollback requires restart, readiness and smoke", rollback)
        self.assertIn('"$APP_DIR" == "/opt/oldsparky/platform"', rollback)
        self.assertIn("rollback-runtime-pending", rollback)
        self.assertIn("smoke-passed", rollback)

    def test_mutating_diagnostics_are_manual_or_post_deploy_and_sha_locked(self) -> None:
        content = (
            WORKFLOW_DIR / "platform-production-content-diagnostics.yml"
        ).read_text(encoding="utf-8")
        diagnostics = (WORKFLOW_DIR / "platform-production-diagnostics.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("\n  push:\n", content)
        self.assertNotIn("\n  push:\n", diagnostics)
        for workflow in (content, diagnostics):
            self.assertIn("platform_release_lock_exec.sh", workflow)
            self.assertIn("--expected-sha", workflow)

        for path in (*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")):
            workflow = path.read_text(encoding="utf-8")
            if "platform/tools/" in workflow:
                self.assertRegex(workflow, r"actions/checkout@[0-9a-f]{40}", path.name)
                self.assertIn("persist-credentials: false", workflow, path.name)

        profile_fixture = (
            WORKFLOW_DIR / "platform-production-profile-review-fixture.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("continue-on-error: true", profile_fixture)
        self.assertIn("id: create_fixture", profile_fixture)
        self.assertIn('report.get("status") != "passed"', profile_fixture)
        self.assertIn('report.get("success") is not True', profile_fixture)
        self.assertIn('report.get("process_exit_code") != 0', profile_fixture)
        self.assertIn("steps.create_fixture.outcome == 'success'", profile_fixture)
        self.assertIn("if: ${{ always() }}", profile_fixture)
        retained_abort = (
            WORKFLOW_DIR / "platform-production-retained-load-abort.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("trap 'rm -f -- \"$raw_log\"' EXIT", retained_abort)

        for name, marker in (
            ("platform-patch-translation-qa.yml", "platform-patch-translation-warmup"),
            ("platform-production-content-diagnostics.yml", "platform-content-diagnostics"),
        ):
            workflow = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
            verify_at = workflow.index("Verify exact completed production deployment provenance")
            pending_at = workflow.index("Mark ", verify_at + 1)
            self.assertLess(verify_at, pending_at, name)
            self.assertIn('"path": ".github/workflows/platform-production-deploy.yml"', workflow, name)
            self.assertIn('"event": "workflow_dispatch"', workflow, name)
            self.assertIn('"conclusion": "success"', workflow, name)
            self.assertIn('status.get("context") == "platform-production-deploy"', workflow, name)
            self.assertIn('status.get("target_url") == os.environ["SOURCE_RUN_URL"]', workflow, name)
            self.assertIn('output.write("deploy_ready=false\\n")', workflow, name)
            self.assertIn('output.write("deploy_ready=true\\n")', workflow, name)
            self.assertIn(
                "steps.verify_deploy_provenance.outputs.deploy_ready == 'true'",
                workflow,
                name,
            )
            self.assertIn(f'context\\":\\"{marker}', workflow, name)

        self.assertEqual(diagnostics.count("home_content.refresh_home_content()"), 1)
        self.assertIn('print("PRODUCTION_PATCH_ID=" + patch_id)', diagnostics)
        self.assertIn('sed -n \'s/^PRODUCTION_PATCH_ID=//p\'', diagnostics)
        deploy = (WORKFLOW_DIR / "platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        production_start = deploy.index("  production:")
        self.assertIn("inputs.mode == 'deploy'", deploy[production_start:])
        marker_at = deploy.index("Mark production deployment successful")
        self.assertIn('context\\":\\"platform-production-deploy', deploy[marker_at:])
        self.assertIn('description\\":\\"Production deployment and live smoke passed', deploy[marker_at:])

    def test_all_production_ssh_workflows_pin_host_identity(self) -> None:
        workflow_names = ('platform-live-launch.yml', 'platform-live-user-qa.yml', 'platform-media-migration-diagnostics.yml', 'platform-patch-translation-qa.yml', 'platform-production-as12-proof.yml', 'platform-production-content-diagnostics.yml', 'platform-production-deploy.yml', 'platform-production-diagnostics.yml', 'platform-production-external-load.yml', 'platform-production-release-abort.yml', 'platform-production-retained-load-cleanup.yml', 'platform-production-retained-load-abort.yml', 'platform-production-storage-diagnostics.yml', 'platform-production-web-runtime-diagnostics.yml')
        expected_fingerprint = "SHA256:1SvoVPU2QXAxj3TlwX3DO/7wGPdl3WcKXPIM87xSQ+Y"
        for name in workflow_names:
            workflow = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
            self.assertIn(expected_fingerprint, workflow, name)
            self.assertTrue(
                "StrictHostKeyChecking yes" in workflow
                or "StrictHostKeyChecking=yes" in workflow,
                name,
            )
            self.assertIn("ssh-keygen -lf", workflow, name)
            self.assertNotIn(
                'ssh-keyscan -T 10 -H "$PROD_SSH_HOST" >> ~/.ssh/known_hosts',
                workflow,
                name,
            )

    def test_all_workflow_ssh_secrets_are_scoped_to_trusted_run_steps(self) -> None:
        secret_names = {"PROD_SSH_HOST", "PROD_SSH_USER", "PROD_SSH_KEY"}
        secret_assignment = re.compile(
            r"^\s+(?P<name>PROD_SSH_(?:HOST|USER|KEY)):\s*"
            r"\$\{\{\s*secrets\.(?P=name)\s*\}\}\s*$",
            re.MULTILINE,
        )
        job_pattern = re.compile(
            r"^  (?P<name>[A-Za-z0-9_-]+):\n"
            r"(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        step_start = re.compile(r"^      - ", re.MULTILINE)
        ssh_cleanup_target = re.compile(
            r"(?m)^\s*\"(?:\$(?:ssh_dir|SSH_DIR)/|\$HOME/\.ssh/|~\/\.ssh\/)(?:"
            r"id_ed25519|old_sparky_prod|known_hosts(?:\.scan)?|config|"
            r"control(?:\.sock|-master(?:\.sock)?)"
            r")\"\s*\\?\s*$"
        )
        ssh_directory_cleanup = re.compile(
            r'(?m)^\s*rm -rf -- "\$(?:ssh_dir|SSH_DIR)"\s*$'
        )
        workflow_paths = sorted(
            (*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml"))
        )
        self.assertTrue(workflow_paths)
        expression_count = 0
        standard_cleanup_dirs = {
            "platform-cloudflare-range-alert.yml": "platform-cloudflare-range-alert-ssh",
            "platform-live-user-qa.yml": "platform-live-user-qa-ssh",
            "platform-media-migration-diagnostics.yml": "platform-media-migration-diagnostics-ssh",
            "platform-patch-translation-qa.yml": "platform-patch-translation-qa-ssh",
            "platform-production-backup.yml": "platform-production-backup-ssh",
            "platform-production-content-diagnostics.yml": "platform-production-content-diagnostics-ssh",
            "platform-production-deploy.yml": "platform-production-deploy-ssh",
            "platform-production-diagnostics.yml": "platform-production-diagnostics-ssh",
            "platform-production-release-abort.yml": "platform-production-release-abort-ssh",
            "platform-production-release-recover.yml": "platform-production-release-recovery-ssh",
            "platform-production-service-recovery.yml": "platform-production-service-recovery-ssh",
        }

        for path in workflow_paths:
            workflow_expression_count = 0
            source = path.read_text(encoding="utf-8")
            self.assertFalse(
                re.search(r"^  PROD_SSH_(?:HOST|USER|KEY):", source, re.MULTILINE),
                f"{path.name} has workflow-level SSH secrets",
            )
            jobs = tuple(job_pattern.finditer(source))
            self.assertTrue(jobs, path.name)

            for job_match in jobs:
                job_name = job_match.group("name")
                job = job_match.group("body")
                ssh_material_created = False
                ssh_material_cleanup_seen = False
                job_env = re.search(
                    r"^    env:\n(?P<body>.*?)(?=^    steps:\n|\Z)",
                    job,
                    re.MULTILINE | re.DOTALL,
                )
                if job_env is not None:
                    self.assertIsNone(
                        secret_assignment.search(job_env.group("body")),
                        f"{path.name}:{job_name} has job-level SSH secrets",
                    )
                    self.assertNotRegex(
                        job_env.group("body"),
                        r"secrets\.PROD_SSH_(?:HOST|USER|KEY)",
                        f"{path.name}:{job_name} has a job-level SSH secret expression",
                    )

                starts = [match.start() for match in step_start.finditer(job)]
                steps = tuple(
                    job[start:end]
                    for start, end in zip(starts, (*starts[1:], len(job)))
                )
                for step_index, step in enumerate(steps):
                    location = f"{path.name}:{job_name}:{step_index}"
                    step_secrets = {
                        match.group("name")
                        for match in secret_assignment.finditer(step)
                    }
                    uses_match = re.search(
                        r"^        uses:\s*([^\s#]+)", step, re.MULTILINE
                    )
                    uses = "" if uses_match is None else uses_match.group(1)
                    run_match = re.search(
                        r"^        run:\s*\|?\s*$", step, re.MULTILINE
                    )
                    run = "" if run_match is None else step[run_match.end() :]

                    if uses.startswith("actions/checkout@"):
                        self.assertRegex(
                            step,
                            r"(?m)^\s+persist-credentials:\s*false\s*$",
                            f"{location} must disable checkout credentials",
                        )
                    if uses.startswith(("actions/checkout@", "actions/download-artifact@")):
                        self.assertFalse(
                            ssh_material_created,
                            f"{location} must not run while production SSH material is present",
                        )
                    if uses.startswith(("actions/checkout@", "actions/download-artifact@")):
                        self.assertFalse(
                            ssh_material_created,
                            f"{location} must not run while a private SSH key is present",
                        )
                        self.assertFalse(
                            secret_names.intersection(step_secrets), location
                        )
                        self.assertNotIn("secrets.PROD_SSH_", step, location)
                    storage_evidence_upload = (
                        # This one upload is deliberately after SSH cleanup:
                        # the sanitizer creates a fixed schema artifact even
                        # when cleanup itself failed, so observability is not
                        # lost with the fail-closed result.
                        uses.startswith("actions/upload-artifact@")
                        and "platform-production-storage-diagnostics-artifact.txt" in step
                    )
                    if uses and ssh_material_cleanup_seen and not storage_evidence_upload:
                        self.assertRegex(
                            step,
                            r"steps\.cleanup_ssh\.outcome\s*==\s*['\"]success['\"]",
                            f"{location} must not run after an unsuccessful SSH cleanup",
                        )
                        self.assertNotRegex(
                            step,
                            r"(?:PROD_SSH_(?:HOST|USER|KEY)|SSH_DIR|id_ed25519|known_hosts|old_sparky_prod)",
                            f"{location} must not inherit cleaned production SSH material",
                        )
                    if "run_checked_out_client" in run:
                        self.assertFalse(
                            secret_names.intersection(step_secrets), location
                        )
                        self.assertNotIn("secrets.PROD_SSH_", step, location)

                    if re.search(
                        r"printf .*PROD_SSH_KEY.*>.*(?:id_ed25519|old_sparky_prod)",
                        run,
                        re.DOTALL,
                    ):
                        ssh_material_created = True
                        ssh_material_cleanup_seen = False
                    has_ssh_cleanup = (
                        ssh_cleanup_target.search(run) is not None
                        or ssh_directory_cleanup.search(run) is not None
                        or (
                            re.search(r'rm -f -- .*"\$ssh_dir/id_ed25519"', run)
                            and 'test ! -e "$ssh_dir"' in run
                        )
                    )
                    if has_ssh_cleanup and "test ! -e" in run:
                        always_guarded = re.search(
                            r"(?m)^\s+if:\s*(?:\$\{\{\s*)?.*always\(\)",
                            step,
                        )
                        trap_guarded = "trap " in run and " EXIT" in run
                        self.assertTrue(
                            always_guarded or trap_guarded,
                            f"{location} must clean production SSH material on every exit",
                        )
                        ssh_material_created = False
                        ssh_material_cleanup_seen = True

                    for secret_name in secret_names:
                        if re.search(rf"\${{{secret_name}}}|\${secret_name}\b", run):
                            self.assertIn(secret_name, step_secrets, location)
                    workflow_expression_count += len(step_secrets)

                self.assertFalse(
                    ssh_material_created,
                    f"{path.name}:{job_name} must clean production SSH material before job exit",
                )

            self.assertEqual(
                len(re.findall(r"secrets\.PROD_SSH_(?:HOST|USER|KEY)", source)),
                workflow_expression_count,
                f"{path.name} has an out-of-scope SSH secret expression",
            )
            expression_count += workflow_expression_count

        for workflow_name, directory_name in standard_cleanup_dirs.items():
            source = (WORKFLOW_DIR / workflow_name).read_text(encoding="utf-8")
            cleanup_start = source.index("      - name: Remove production SSH material")
            cleanup = source[cleanup_start:]
            self.assertIn(
                f'ssh_dir="$RUNNER_TEMP/{directory_name}"',
                cleanup,
                workflow_name,
            )
            self.assertIn(
                f'test "$ssh_dir" = "$RUNNER_TEMP/{directory_name}"',
                cleanup,
                workflow_name,
            )
            self.assertIn("if: ${{ always() }}", cleanup, workflow_name)
            self.assertNotIn("continue-on-error: true", cleanup, workflow_name)
            self.assertIn('test -d "$ssh_dir" && test ! -L "$ssh_dir"', cleanup, workflow_name)
            self.assertIn('rm -f -- \\', cleanup, workflow_name)
            self.assertIn('"$ssh_dir/id_ed25519"', cleanup, workflow_name)
            self.assertIn('"$ssh_dir/known_hosts"', cleanup, workflow_name)
            self.assertIn('"$ssh_dir/known_hosts.scan"', cleanup, workflow_name)
            self.assertIn('"$ssh_dir/config"', cleanup, workflow_name)
            self.assertIn('"$ssh_dir/control.sock"', cleanup, workflow_name)
            self.assertIn('"$ssh_dir/control-master"', cleanup, workflow_name)
            self.assertIn('"$ssh_dir/control-master.sock"', cleanup, workflow_name)
            self.assertIn('rmdir -- "$ssh_dir"', cleanup, workflow_name)
            self.assertNotIn('rm -rf -- "$ssh_dir"', cleanup, workflow_name)
            self.assertIn("printf '%s\\n' 'SSH_DIR=' >> \"$GITHUB_ENV\"", cleanup)
            self.assertIn('test -z "${SSH_DIR:-}"', cleanup, workflow_name)

        self.assertGreater(expression_count, 0)

    def test_storage_diagnostics_are_read_only(self) -> None:
        workflow = (
            WORKFLOW_DIR / "platform-production-storage-diagnostics.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("expected_sha", workflow)
        self.assertIn("platform_storage_maintenance.py", workflow)
        self.assertIn("--json", workflow)
        self.assertIn(
            '"$python_bin" "$maintenance_tool" --json --skip-backup',
            workflow,
        )
        self.assertIn(
            "df -B1 --output=size,used,avail,pcent -- \"$path\"", workflow
        )
        self.assertIn(
            "df --output=iused,iavail,ipcent -- \"$path\"", workflow
        )
        self.assertIn("journalctl --disk-usage", workflow)
        self.assertIn("du -x -s -B1 -- \"$path\"", workflow)
        self.assertIn("platform_storage_diagnostics_sanitizer.py", workflow)
        self.assertIn("platform_storage_maintenance.py", workflow)
        self.assertIn("sha256sum --strict --check -- manifest.sha256", workflow)
        self.assertNotIn("platform_workflow_input_guard.py", workflow)
        self.assertIn("platform_storage_evidence_summary.py", workflow)
        self.assertNotIn('summary_tool="$current/tools/platform_storage_evidence_summary.py"', workflow)
        self.assertIn(
            "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
            workflow,
        )
        self.assertIn('ref: ${{ github.sha }}', workflow)
        self.assertIn("timeout --foreground 600s ssh", workflow)
        self.assertIn("--signal=TERM --kill-after=5s 600s ssh", workflow)
        self.assertIn("timeout --foreground --signal=TERM --kill-after=5s 90s", workflow)
        self.assertIn("ulimit -f 1025", workflow)
        self.assertIn('test "$(ulimit -f)" = 1025', workflow)
        self.assertNotIn('exec 9>"$retained_load_lock"', workflow)
        self.assertIn("os.O_RDONLY | os.O_CLOEXEC | getattr(os, \"O_NOFOLLOW\", 0)", workflow)
        self.assertIn("os.O_EXCL", workflow)
        self.assertIn("platform_storage_diagnostics_contract.py", workflow)
        contract_source = self.read_tool("platform_storage_diagnostics_contract.py")
        self.assertIn("os.replace(temporary, path)", contract_source)
        self.assertIn("allow_nan=False", contract_source)
        self.assertIn("Validate canonical prepare-failure schema", workflow)
        self.assertIn('--write-failure "$failure_artifact"', workflow)
        self.assertIn('--validate "$failure_artifact"', workflow)
        self.assertIn("validate_artifact()", workflow)
        self.assertIn("needs.prepare.result == 'success'", workflow)
        self.assertIn('remote_stderr_bytes="$(wc -c <"$ssh_error"', workflow)
        self.assertIn('report_present=false', workflow)
        self.assertIn('if [[ "$remote_status" = 0 && "$report_present" = true ]]', workflow)
        self.assertIn('DIAGNOSTICS_REMOTE_STATUS=', workflow)
        self.assertIn('DIAGNOSTICS_REPORT_PRESENT=', workflow)
        self.assertIn('SSH_CLEANUP_OUTCOME:', workflow)
        self.assertIn('write_fallback()', workflow)
        self.assertIn('>/dev/null 2>/dev/null', workflow)
        self.assertIn('raw_output_included', contract_source)
        self.assertIn('regular file:1:600', workflow)
        self.assertIn('bounded evidence was published', workflow)
        self.assertIn(
            'if: ${{ always() }}',
            workflow,
        )
        self.assertIn(
            '"$RUNNER_TEMP/platform-production-storage-diagnostics.txt"',
            workflow,
        )
        self.assertIn(
            '"$RUNNER_TEMP/platform-production-storage-diagnostics-ssh-error"',
            workflow,
        )
        self.assertNotIn('cat "$ssh_error"', workflow)
        self.assertNotIn('echo "$ssh_error"', workflow)
        self.assertNotIn('cp -- "$remote_report" "$public_artifact"', workflow)
        self.assertNotIn("df -hT", workflow)
        self.assertNotIn("findmnt", workflow)
        self.assertNotIn("fuser", workflow)
        self.assertNotIn("lslocks", workflow)
        self.assertNotIn("--apply", workflow)
        self.assertNotIn("systemctl restart", workflow)
        self.assertNotIn("rm -rf", workflow)
        self.assertLess(
            workflow.index("- name: Remove production SSH material"),
            workflow.index("- name: Sanitize storage diagnostic evidence"),
        )
        self.assertLess(
            workflow.index("- name: Remove private storage diagnostic captures"),
            workflow.index("- name: Upload storage diagnostic evidence"),
        )
        self.assertIn('rmdir -- "$projector_dir"', workflow)
        sanitize_start = workflow.index(
            "- name: Sanitize storage diagnostic evidence"
        )
        cleanup_start = workflow.index(
            "- name: Remove private storage diagnostic captures"
        )
        upload_start = workflow.index("- name: Upload storage diagnostic evidence")
        self.assertLess(sanitize_start, cleanup_start)
        self.assertLess(cleanup_start, upload_start)
        cleanup = workflow[cleanup_start:upload_start]
        upload = workflow[upload_start:]
        self.assertIn("if: ${{ always() }}", cleanup)
        self.assertNotIn(
            "platform-production-storage-diagnostics-artifact.txt", cleanup
        )
        self.assertIn(
            "if: ${{ always() && steps.cleanup_ssh.outcome == 'success' && steps.cleanup_captures.outcome == 'success' }}",
            upload,
        )
        self.assertNotIn("\n      - name:", upload)
        sanitize = workflow[sanitize_start:cleanup_start]
        self.assertIn("projector_status=1", sanitize)
        self.assertIn("exit 1", sanitize)
        probe_start = workflow.index(
            "          import errno", workflow.index("lock_state=")
        )
        probe_end = workflow.index("          PY", probe_start)
        probe = textwrap.dedent(workflow[probe_start:probe_end])
        self.assertIn("os.O_RDONLY", probe)
        self.assertIn("getattr(os, \"O_NOFOLLOW\", 0)", probe)
        self.assertIn("fcntl.LOCK_EX | fcntl.LOCK_NB", probe)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = root / "retained-load.lock"
            lock_path.write_bytes(b"coordination lock\n")
            lock_path.chmod(0o600)
            before = lock_path.stat()
            probe_path = root / "probe.py"
            probe_path.write_text(probe, encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-I", str(probe_path), str(lock_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "unlocked")
            after = lock_path.stat()
            self.assertEqual(after.st_size, before.st_size)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def test_as12_proof_is_read_only_and_sha_locked(self) -> None:
        proof = (WORKFLOW_DIR / "platform-production-as12-proof.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("workflow_dispatch:", proof)
        self.assertIn("--expected-sha", proof)
        self.assertIn("platform_validate_edge_policy.py", proof)
        self.assertIn("direct_origin_", proof)
        self.assertIn("expected_dhcp_listener", proof)
        self.assertIn("systemd-network", proof)
        self.assertNotIn("platform_release_deploy.sh", proof)
        self.assertNotIn("systemctl restart", proof)
        self.assertNotIn("systemctl reload", proof)

    def test_media_migration_public_projection_drops_adversarial_report_values(self) -> None:
        private_key_marker = "".join(
            ("-----BEGIN ", "OPENSSH ", "PRIVATE ", "KEY-----")
        )
        payload = {
            "ok": False,
            "mode": "check",
            "mutated": False,
            "code": "manual_conflicts_present",
            "inventory_before": {
                "legacy_upload_references": 4,
                "packaged_asset_references": 10**40,
                "manual_conflicts": 1,
                "nested": {
                    "body": "request body email=alice@example.test",
                    "sql": "SELECT email FROM users WHERE password='secret-password'",
                },
            },
            "source_locations": {
                "r2": 1,
                "https://private.invalid/invite?token=secret-token": 1,
            },
            "source_results": [
                {
                    "ok": False,
                    "location": "198.51.100.42",
                    "code": "Authorization: Bearer secret-token",
                    "nested": [
                        "Cookie=session=secret-session",
                        "2001:db8::42",
                        "/home/root/private-report.json",
                        "ssh -i /root/.ssh/id_ed25519 operator@example.test",
                        private_key_marker,
                    ],
                }
            ],
            "operations": {
                "r2_gets": 10**40,
                "raw": "https://private.invalid/?query=secret",
            },
            "raw": {
                "command": "curl -H 'Authorization: Bearer secret-token'",
                "body": "invite=INVITE-CODE",
            },
        }

        report = platform_media_migration_diagnostics_summary.public_summary(
            payload=payload,
            producer_exit_code=2,
            stderr_bytes=10**40,
        )
        serialized = json.dumps(report, sort_keys=True, ensure_ascii=True)
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["kind"], "media_migration_diagnostics")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["error_class"], "manual_conflict")
        self.assertEqual(report["source_class"], "unknown")
        self.assertEqual(report["inventory_before_packaged_asset_references"], 1_000_000)
        self.assertEqual(report["operation_r2_gets"], 1_000_000)
        self.assertEqual(report["stderr_bytes"], 1_000_000)
        self.assertFalse(
            any(isinstance(value, (dict, list)) for value in report.values())
        )
        for forbidden in (
            "alice@example.test",
            "Authorization: Bearer secret-token",
            "Cookie=session=secret-session",
            "198.51.100.42",
            "2001:db8::42",
            "https://private.invalid/invite?token=secret-token",
            "invite=INVITE-CODE",
            "SELECT email FROM users WHERE password='secret-password'",
            "/home/root/private-report.json",
            "/root/.ssh/id_ed25519",
            private_key_marker,
            "curl -H 'Authorization: Bearer secret-token'",
        ):
            self.assertNotIn(forbidden.lower(), serialized.lower())

    def test_media_migration_invalid_public_report_fails_closed(self) -> None:
        report = platform_media_migration_diagnostics_summary.public_summary(
            payload={"nested": ["alice@example.test"], "mutated": "false"},
            producer_exit_code="not-a-status",
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error_class"], "unexpected_exit")
        self.assertFalse(report["mutated"])
        self.assertEqual(report["producer_exit_code"], 255)
        self.assertNotIn("alice@example.test", json.dumps(report))

    def test_media_workflow_projects_and_cleans_private_report_before_public_output(self) -> None:
        workflow = (
            WORKFLOW_DIR / "platform-media-migration-diagnostics.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "platform_media_migration_diagnostics_summary.py", workflow
        )
        self.assertIn(
            'chmod 600 "$private_report" "$private_error" "$public_report" "$projector_error"',
            workflow,
        )
        self.assertIn("trap cleanup_media_files EXIT", workflow)
        self.assertIn(
            'rm -f -- "$private_report" "$private_error" "$projector_error"',
            workflow,
        )
        self.assertIn("MEDIA_INVENTORY", workflow)
        self.assertIn("if: ${{ always() }}", workflow)
        self.assertLess(
            workflow.index("Remove private media diagnostic capture"),
            workflow.index("Remove production SSH material"),
        )
        self.assertNotIn('"source_locations"', workflow)
        self.assertNotIn('"source_results"', workflow)
        self.assertNotIn('"operations":', workflow)
        self.assertNotIn("json.dumps(summary", workflow)
        self.assertNotIn('cat "$remote_log"', workflow)

    def test_live_mutations_share_release_lock_and_exact_sha(self) -> None:
        for name in (
            "platform-live-launch.yml",
            "platform-live-user-qa.yml",
            "platform-patch-translation-qa.yml",
        ):
            workflow = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
            if name == "platform-live-launch.yml":
                workflow += "\n" + (
                    PLATFORM_ROOT / "tools/platform_live_launch_trusted.sh"
                ).read_text(encoding="utf-8")
                workflow += "\n" + (
                    PLATFORM_ROOT / "tools/platform_live_launch_supervisor.sh"
                ).read_text(encoding="utf-8")
            self.assertIn("platform_release_lock_exec.sh", workflow, name)
            self.assertIn("--expected-sha", workflow, name)

    def test_translation_workflows_never_source_production_dotenv(self) -> None:
        for name in (
            "platform-patch-translation-qa.yml",
            "platform-production-diagnostics.yml",
        ):
            workflow = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn('. "$PLATFORM_ENV_FILE"', workflow, name)
            self.assertIn("platform_load_env_file", workflow, name)


if __name__ == "__main__":
    unittest.main()
