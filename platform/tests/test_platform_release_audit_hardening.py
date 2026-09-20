from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = PLATFORM_ROOT / "tools"
SYSTEMD_DIR = PLATFORM_ROOT / "deploy" / "systemd"
WORKFLOW_DIR = PLATFORM_ROOT.parent / ".github" / "workflows"

sys.path.insert(0, str(TOOLS_DIR))
import platform_safe_env_exec as safe_env  # noqa: E402
from tools import platform_nginx_error_summary  # noqa: E402
from tools import platform_media_migration_diagnostics_summary  # noqa: E402
from tools import platform_web_runtime_diagnostics_summary  # noqa: E402


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
        exec_at = alembic.index('exec "$PLATFORM_PYTHON_BIN" -m alembic')
        self.assertLess(stop_at, exec_at)

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
        recover_lock = recover_workflow.index(
            'lock_helper="$runtime/current/tools/platform_release_lock.sh"'
        )
        recover_supervisor = recover_workflow.index(
            '"$lock_helper" --run /bin/bash -s <<\'LOCKED\'', recover_lock
        )
        retain_recovery = recover_workflow.index(
            'recover \\\n            --retain --state "$state"', recover_supervisor
        )
        restore_runtime = recover_workflow.index(
            'PLATFORM_ENABLE_SYSTEMD_UNITS=0 "$restore"', retain_recovery
        )
        verify_runtime = recover_workflow.index(
            'verify --state "$systemd_state"', restore_runtime
        )
        nginx_check = recover_workflow.index("nginx -t", verify_runtime)
        api_health_check = recover_workflow.index(
            "http://127.0.0.1:8010/api/v1/health/ready", nginx_check
        )
        web_health_check = recover_workflow.index(
            "http://127.0.0.1:3000/", nginx_check
        )
        clear_systemd_receipt = recover_workflow.index(
            'clear --state "$systemd_state"', web_health_check
        )
        complete_recovery = recover_workflow.index(
            'complete-recovery \\\n            --state "$state"', verify_runtime
        )
        self.assertLess(recover_lock, recover_supervisor)
        self.assertLess(recover_supervisor, retain_recovery)
        self.assertLess(retain_recovery, restore_runtime)
        self.assertLess(restore_runtime, verify_runtime)
        self.assertLess(verify_runtime, nginx_check)
        self.assertLess(nginx_check, api_health_check)
        self.assertLess(nginx_check, web_health_check)
        self.assertLess(api_health_check, complete_recovery)
        self.assertLess(web_health_check, complete_recovery)
        self.assertLess(clear_systemd_receipt, complete_recovery)
        self.assertLess(verify_runtime, complete_recovery)
        self.assertIn("platform_release_lock_supervisor_holds", recover_workflow)
        self.assertIn("PLATFORM_ENABLE_SYSTEMD_UNITS=0", recover_workflow)
        self.assertIn('test ! -e "$systemd_state"', recover_workflow)
        self.assertNotIn("recover-pending", recover_workflow)
        self.assertNotIn("--recover-pending", recover_workflow)
        self.assertNotIn("exec 9<", recover_workflow)
        self.assertNotIn("flock -n 9", recover_workflow)
        self.assertNotIn("PLATFORM_RELEASE_LOCK_FD=9", recover_workflow)
        self.assertIn("STATE_VERSION = 2", abort_workflow)
        self.assertIn("QUIESCE_STATE_VERSION = 1", abort_workflow)
        self.assertIn("status-quiesce", abort_workflow)
        self.assertIn('"service_state_before"', abort_workflow)
        self.assertIn('"timer_active_before"', abort_workflow)
        self.assertIn("trusted_transaction", abort_workflow)
        self.assertIn("trusted_release", abort_workflow)
        self.assertIn("assert_unit_state", abort_workflow)
        self.assertIn('expected_timer', abort_workflow)
        self.assertIn("preserved inactive", abort_workflow)
        self.assertIn("test ! -e \"$state\"", abort_workflow)
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

        guard = TOOLS_DIR / "platform_release_lock_exec.sh"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_root = root / "run-lock"
            lock_root.mkdir(mode=0o700)
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
                "lock_root=$1\napp_dir=$2\nguard=$3\n"
                "marker=$4\nattacker_lock=$5\n"
                "mount --bind \"$lock_root\" /run/lock\n"
                "export PLATFORM_ENVIRONMENT=test PLATFORM_TESTING=1\n"
                "export PLATFORM_TEST_RELEASE_LOCK_PATH=\"$attacker_lock\"\n"
                "set +e\n"
                "/usr/bin/flock -n --close /run/lock/oldsparky-platform-release.lock "
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
                    "/usr/bin/unshare",
                    "-m",
                    "--propagation",
                    "private",
                    str(runner),
                    str(lock_root),
                    str(app_dir),
                    str(guard),
                    str(marker),
                    str(attacker_lock),
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
        helper = TOOLS_DIR / "platform_release_lock.sh"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_root = root / "run-lock"
            lock_root.mkdir(mode=0o700)
            runner = root / "run-shared-lock-regression.sh"
            runner.write_text(
                "#!/usr/bin/env bash\n"
                "set -Eeuo pipefail\n"
                "lock_root=$1\nhelper=$2\n"
                "mount --bind \"$lock_root\" /run/lock\n"
                "/usr/bin/flock -n -s --close /run/lock/oldsparky-platform-release.lock "
                "/bin/bash -c '\n"
                "  set -Eeuo pipefail\n"
                "  source \"$1\"\n"
                "  if platform_release_lock_supervisor_holds; then exit 42; fi\n"
                "' bash \"$helper\"\n",
                encoding="utf-8",
            )
            runner.chmod(0o755)
            result = subprocess.run(
                [
                    "/usr/bin/unshare",
                    "-m",
                    "--propagation",
                    "private",
                    str(runner),
                    str(lock_root),
                    str(helper),
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
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "9").exists(), result.stderr)

    def test_pathname_supervisor_body_has_no_release_fd(self) -> None:
        helper = TOOLS_DIR / "platform_release_lock.sh"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_root = root / "run-lock"
            lock_root.mkdir(mode=0o700)
            runner = root / "run-fd-regression.sh"
            runner.write_text(
                "#!/usr/bin/env bash\n"
                "set -Eeuo pipefail\n"
                "lock_root=$1\nhelper=$2\n"
                "mount --bind \"$lock_root\" /run/lock\n"
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
            runner.chmod(0o755)
            result = subprocess.run(
                [
                    "/usr/bin/unshare",
                    "-m",
                    "--propagation",
                    "private",
                    str(runner),
                    str(lock_root),
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
        self.assertIn("systemctl disable", installer)

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
            self.assertIn("StrictHostKeyChecking yes", workflow, name)
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
                    if uses and ssh_material_cleanup_seen:
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
        self.assertIn("platform_storage_maintenance.py\" --json", workflow)
        self.assertIn(
            "df -B1 --output=size,used,avail,pcent -- \"$path\"", workflow
        )
        self.assertIn(
            "df --output=iused,iavail,ipcent -- \"$path\"", workflow
        )
        self.assertIn("journalctl --disk-usage", workflow)
        self.assertIn("du -x -s -B1 -- \"$path\"", workflow)
        self.assertIn("platform_storage_evidence_summary.py", workflow)
        self.assertNotIn("df -hT", workflow)
        self.assertNotIn("findmnt", workflow)
        self.assertNotIn("fuser", workflow)
        self.assertNotIn("lslocks", workflow)
        self.assertNotIn("--apply", workflow)
        self.assertNotIn("systemctl restart", workflow)
        self.assertNotIn("rm -rf", workflow)

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
