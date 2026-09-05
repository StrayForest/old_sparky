from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = PLATFORM_ROOT / "tools"
SYSTEMD_DIR = PLATFORM_ROOT / "deploy" / "systemd"
WORKFLOW_DIR = PLATFORM_ROOT.parent / ".github" / "workflows"

sys.path.insert(0, str(TOOLS_DIR))
import platform_safe_env_exec as safe_env  # noqa: E402


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
        self.assertIn("migration-pending", alembic)
        stop_at = alembic.index("systemctl stop deadlock-api deadlock-worker deadlock-web")
        exec_at = alembic.index('exec "$PLATFORM_PYTHON_BIN" -m alembic')
        self.assertLess(stop_at, exec_at)

    def test_deploy_quiesces_before_stage_and_migration(self) -> None:
        deploy = self.read_tool("platform_release_deploy.sh")
        quiesce_call = deploy.index(
            "quiesce_runtime_writers", deploy.index('if [[ "$RESUME" -eq 0 ]]')
        )
        stage = deploy.index('"$INSTALL_TOOL" --stage-only')
        migration = deploy.index("tools/platform_run_alembic.sh upgrade head")
        self.assertLess(quiesce_call, stage)
        self.assertLess(stage, migration)
        self.assertIn("release_preflight", deploy[stage:migration])

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


    def test_all_production_ssh_workflows_pin_host_identity(self) -> None:
        workflow_names = ('platform-live-launch.yml', 'platform-live-user-qa.yml', 'platform-media-migration-diagnostics.yml', 'platform-patch-translation-qa.yml', 'platform-production-as12-proof.yml', 'platform-production-content-diagnostics.yml', 'platform-production-deploy.yml', 'platform-production-diagnostics.yml', 'platform-production-external-load.yml', 'platform-production-retained-load-cleanup.yml', 'platform-production-retained-load-abort.yml')
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

    def test_live_mutations_share_release_lock_and_exact_sha(self) -> None:
        for name in (
            "platform-live-launch.yml",
            "platform-live-user-qa.yml",
            "platform-patch-translation-qa.yml",
        ):
            workflow = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
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
