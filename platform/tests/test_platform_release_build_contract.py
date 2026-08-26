from __future__ import annotations

import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "platform/tools/platform_build_release.sh"


class PlatformReleaseBuildContractTests(unittest.TestCase):
    def test_systemd_install_prepares_current_release_runtime_before_restart(
        self,
    ) -> None:
        systemd_installer = (
            REPO_ROOT / "platform/tools/platform_install_systemd_units.sh"
        ).read_text()
        release_installer = (
            REPO_ROOT / "platform/tools/platform_release_install.sh"
        ).read_text()

        prepare = '"$ROOT_DIR/tools/platform_prepare_service_user.sh"'
        self.assertIn(prepare, systemd_installer)
        self.assertLess(
            systemd_installer.index(prepare),
            systemd_installer.index("systemctl daemon-reload"),
        )
        self.assertIn(
            "Install units and prepare release-specific writable paths",
            release_installer,
        )
        deploy = (REPO_ROOT / "platform/tools/platform_release_deploy.sh").read_text()
        self.assertIn("--stage-only", release_installer)
        self.assertIn("--artifact", deploy)
        self.assertIn("migration-pending", deploy)
        self.assertIn("nginx-pending", deploy)
        self.assertIn("--resume", deploy)
        self.assertIn("--abort-retained", deploy)
        self.assertIn("MIGRATION_NOT_REVERSED", deploy)
        self.assertIn('"$TOOLS_DIR/platform_release_preflight.sh"', deploy)
        self.assertIn("acquire_release_lock", deploy)
        self.assertIn("platform_install_systemd_units.sh", deploy)
        self.assertIn("platform_deploy_smoke.py", deploy)
        self.assertIn("platform_release_restore_runtime.sh", deploy)
        rollback = (REPO_ROOT / "platform/tools/platform_release_rollback.sh").read_text()
        self.assertIn("rollback-runtime-pending", rollback)
        self.assertIn("platform_release_restore_runtime.sh", rollback)
        self.assertIn(".release-recovery", rollback)
        self.assertIn("install_recovery_shim", rollback)
        recovery_shim = (
            REPO_ROOT / "platform/tools/platform_release_recovery_shim.sh"
        ).read_text()
        self.assertIn('RECOVERY_DIR="$SHARED_DIR/.release-recovery"', recovery_shim)
        self.assertIn('RECOVERY_TOOL="$RECOVERY_DIR/platform_release_rollback.sh"', recovery_shim)

        systemd_units = (
            REPO_ROOT / "platform/tools/platform_install_systemd_units.sh"
        ).read_text()
        preflight = (REPO_ROOT / "platform/tools/platform_release_preflight.sh").read_text()
        self.assertIn('RENDER_SERVICE_ENVS_TOOL="$SCRIPT_DIR/platform_render_service_envs.py"', preflight)
        self.assertIn('EDGE_POLICY_TOOL="$SCRIPT_DIR/platform_validate_edge_policy.py"', preflight)
        self.assertIn("deadlock-offsite-backup.service", systemd_units)
        self.assertIn("deadlock-offsite-backup.timer", systemd_units)

    def test_production_deploy_requires_green_security_status(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text()

        self.assertIn("Require successful platform security build", workflow)
        self.assertIn(
            '"${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/commits/${TARGET_SHA}/status"',
            workflow,
        )
        self.assertIn('item.get("context") == "platform-security-build"', workflow)
        self.assertIn('test "$security_state" = success', workflow)
        self.assertLess(
            workflow.index("Require successful platform security build"),
            workflow.index("Mark production deployment pending"),
        )

    def test_production_preflight_requires_edge_parity_before_preflight_exit(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text()
        preflight_start = workflow.index('"$current/tools/platform_release_preflight.sh"')
        preflight_exit = workflow.index(
            'if [[ "$deploy_mode" == "preflight" ]]',
            preflight_start,
        )
        initial_preflight = workflow[preflight_start:preflight_exit]
        self.assertIn("--require-edge-parity", initial_preflight)

    def test_production_deploy_consumes_ci_artifact_without_host_build(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text()
        self.assertIn("Build immutable release artifact in CI", workflow)
        self.assertIn("actions/upload-artifact", workflow)
        self.assertIn("actions/download-artifact", workflow)
        self.assertIn("PUBLISHED_ARTIFACT_DIGEST", workflow)
        self.assertIn("RELEASE.provenance.json", workflow)
        self.assertIn("artifact_sha256", workflow)
        self.assertIn('ci_build_root=/root/old_sparky', workflow)
        self.assertIn('sudo -EH env \\\n            PLATFORM_RELEASE_OUTPUT_DIR=', workflow)
        self.assertIn('sudo find "$ci_build_root/platform/dist/releases"', workflow)
        self.assertIn('sudo chown "$(id -u):$(id -g)"', workflow)
        self.assertIn(
            '(cd "$release_output" && sha256sum -c "$(basename "$release_checksum")")',
            workflow,
        )
        self.assertIn(
            'bootstrap_dir="$(mktemp -d /tmp/old-sparky-release-bootstrap.XXXXXX)"',
            workflow,
        )
        self.assertIn('--extract-to "$bootstrap_dir"', workflow)
        self.assertIn(
            'candidate_deploy="$bootstrap_dir/$artifact_slug/tools/platform_release_deploy.sh"',
            workflow,
        )
        remote_start = workflow.index("<<'REMOTE'")
        remote_script = workflow[remote_start:]
        self.assertNotIn("platform_build_release.sh", remote_script)
        self.assertNotIn("pip install -r platform/requirements-platform.lock.txt", remote_script)
        validator = (REPO_ROOT / "platform/tools/platform_validate_release_artifact.py").read_text()
        self.assertIn("source_git_commit", workflow)
        self.assertIn("expected source commit", validator)

    def test_production_env_contract_matches_runtime_policy(self) -> None:
        example = (REPO_ROOT / "platform/.env.platform.example").read_text()
        preflight = (REPO_ROOT / "platform/tools/platform_release_preflight.sh").read_text()
        operations = (REPO_ROOT / "platform/docs/operations-runbook.md").read_text()
        self.assertIn("127.0.0.1:5432/platformdb", example)
        self.assertNotIn("127.0.0.1:6432", example)
        self.assertIn('root:root 0600', preflight)
        self.assertIn("directly to PostgreSQL", operations)

    def test_security_workflow_runs_docs_and_reports_all_required_jobs(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-security.yml").read_text()

        self.assertIn('".github/workflows/**"', workflow)
        self.assertIn("docs:", workflow)
        self.assertIn("platform_docs_check.py", workflow)
        self.assertIn("docs, migration, smoke", workflow)
        self.assertIn("DOCS_RESULT", workflow)
        self.assertIn('github.event_name == \'workflow_dispatch\'', workflow)

    def test_server_diagnostics_have_github_dispatch_contours(self) -> None:
        for workflow_name in (
            "platform-media-migration-diagnostics.yml",
            "platform-production-content-diagnostics.yml",
            "platform-production-diagnostics.yml",
            "platform-live-launch.yml",
            "platform-live-user-qa.yml",
        ):
            with self.subTest(workflow=workflow_name):
                workflow = (REPO_ROOT / ".github/workflows" / workflow_name).read_text()
                self.assertIn("workflow_dispatch:", workflow)

    def test_retained_load_matrix_is_manual_and_preproduction_only(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-retained-load-matrix.yml"
        ).read_text()
        supervisor = (
            REPO_ROOT / "platform/tools/platform_retained_load_matrix_qa.sh"
        ).read_text()

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn('test "$GITHUB_REF" = "refs/heads/dev"', workflow)
        self.assertIn("RUN-RETAINED-LOAD-MATRIX", workflow)
        self.assertIn("actions/upload-artifact@v6", workflow)
        self.assertIn("GITHUB_STEP_SUMMARY", workflow)
        self.assertIn("http://127.0.0.1", supervisor)
        self.assertIn('[[ "$EUID" -ne 0 ]]', supervisor)
        self.assertIn('flock -n 9', supervisor)
        self.assertIn('test "$release_sha" = "$target_sha"', supervisor)
        self.assertIn("canonical production origin", supervisor)
        self.assertIn("RETAINED_LOAD_MATRIX_EXPORT=", supervisor)

    def test_production_retained_load_has_exact_manual_cleanup_contour(self) -> None:
        load_workflow = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-matrix.yml"
        ).read_text()
        cleanup_workflow = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-cleanup.yml"
        ).read_text()
        abort_workflow = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-abort.yml"
        ).read_text()
        load_supervisor = (
            REPO_ROOT / "platform/tools/platform_production_retained_load_matrix_qa.sh"
        ).read_text()
        cleanup_supervisor = (
            REPO_ROOT / "platform/tools/platform_production_retained_load_cleanup_qa.sh"
        ).read_text()
        cleanup_tool = (
            REPO_ROOT / "platform/tools/platform_cleanup_retained_matrix.py"
        ).read_text()

        for workflow in (load_workflow, cleanup_workflow, abort_workflow):
            self.assertIn("workflow_dispatch:", workflow)
            self.assertNotIn("schedule:", workflow)
            self.assertIn('test "$GITHUB_REF" = "refs/heads/dev"', workflow)
            self.assertIn("environment: production", workflow)
            self.assertIn("actions/upload-artifact@v6", workflow)
        self.assertIn("RUN-PRODUCTION-RETAINED-LOAD-MATRIX", load_workflow)
        self.assertIn("sse_origin_mode", load_workflow)
        self.assertIn("origin-local", load_workflow)
        self.assertIn("DELETE-PRODUCTION-RETAINED-LOAD", cleanup_workflow)
        self.assertIn("ABORT-PRODUCTION-RETAINED-LOAD", abort_workflow)
        self.assertIn("ABORT_EVIDENCE_EXPORT=", abort_workflow)
        self.assertIn("server-observability.log", abort_workflow)
        self.assertIn("https://old-sparky.com", load_supervisor)
        self.assertIn("https://old-sparky.com", cleanup_supervisor)
        self.assertIn("flock -n 9", load_supervisor)
        self.assertIn("flock -n 9", cleanup_supervisor)
        self.assertIn("PRODUCTION_RETAINED_LOAD_MATRIX_EXPORT=", load_supervisor)
        self.assertIn("server-observability.log", load_supervisor)
        self.assertIn("server-observability.log", load_workflow)
        self.assertIn("qa-command.log", load_supervisor)
        self.assertIn("qa-command.log", load_workflow)
        self.assertIn("PRODUCTION_RETAINED_LOAD_CLEANUP_OK=1", cleanup_supervisor)
        self.assertIn("control account", cleanup_tool)
        self.assertIn("_validate_tournament_graph_boundary", cleanup_tool)
        self.assertIn("platform_cleanup_retained_matrix.py", load_supervisor + cleanup_supervisor)
        self.assertIn("async def _main()", cleanup_tool)
        self.assertNotIn("asyncio.run(dispose_engine())", cleanup_tool)
        self.assertIn("platform_recover_retained_browser_report.py", cleanup_supervisor)
        self.assertIn("timeout --signal=TERM --kill-after=30s", load_supervisor)
        self.assertIn('HTTP_MAX_CONNECTIONS="${PLATFORM_QA_HTTP_MAX_CONNECTIONS:-512}"', load_supervisor)
        self.assertIn('browser_http_connections="$HTTP_MAX_CONNECTIONS"', load_supervisor)
        self.assertNotIn('browser_http_connections=40', load_supervisor)
        self.assertIn('browser_setup_concurrency=20', load_supervisor)
        self.assertIn('--concurrency "$browser_setup_concurrency"', load_supervisor)
        self.assertIn('--browser-polling-duration 30', load_supervisor)
        self.assertIn('--browser-polling-open-stagger 300', load_supervisor)
        self.assertNotIn('--browser-polling-active-users-only', load_supervisor)

    def test_browser_qa_does_not_silently_fallback_to_production_env(self) -> None:
        qa_source = (REPO_ROOT / "platform/tools/platform_production_qa.py").read_text()

        self.assertIn(
            'configured_env = os.environ.get("PLATFORM_ENV_FILE", "").strip()',
            qa_source,
        )
        self.assertIn(
            'env_file = Path(configured_env) if configured_env else PLATFORM_ROOT / ".env.platform"',
            qa_source,
        )
        self.assertNotIn(
            'live_env = Path("/opt/oldsparky/platform/shared/.env.platform")',
            qa_source,
        )
        self.assertIn(
            "ANALYZE platform.users, platform.sessions, platform.user_roles",
            qa_source,
        )
        self.assertIn("active_query_samples", qa_source)

    def test_release_ref_is_rejected_before_any_build_or_network_work(self) -> None:
        unsafe_refs = ("../escape", "bad/ref", 'bad"json', "-leading", "x" * 101)
        with tempfile.TemporaryDirectory() as temp_dir:
            for release_ref in unsafe_refs:
                with self.subTest(release_ref=release_ref):
                    result = subprocess.run(
                        [str(BUILD_SCRIPT), release_ref],
                        cwd=REPO_ROOT,
                        env={
                            **os.environ,
                            "PLATFORM_RELEASE_OUTPUT_DIR": str(
                                Path(temp_dir) / "releases"
                            ),
                        },
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Release ref", result.stderr)
            self.assertFalse((Path(temp_dir) / "releases").exists())

    def test_build_uses_only_tracked_source_and_lock_driven_node_install(self) -> None:
        script = BUILD_SCRIPT.read_text()

        self.assertIn(
            'git -C "$REPO_ROOT" archive --format=tar "$SOURCE_GIT_COMMIT" platform',
            script,
        )
        self.assertIn("/usr/bin/tar --no-same-permissions -xf -", script)
        self.assertIn("status --porcelain=v1 --untracked-files=all -- platform", script)
        self.assertIn('"$PLATFORM_NODE_BIN" "$NPM_CLI" ci', script)
        self.assertIn("Tracked package.json must pin an exact npm version", script)
        self.assertNotIn("rsync", script)
        self.assertNotIn('node_modules/" "$STAGING_DIR', script)
        self.assertIn("rm -rf node_modules .next/cache", script)

    def test_build_resolves_and_freezes_python_dependencies_into_artifact(self) -> None:
        script = BUILD_SCRIPT.read_text()

        self.assertIn(
            '"$ROOT_DIR/.venv_platform/bin/python" -I -m pip download', script
        )
        self.assertIn("--only-binary=:all:", script)
        self.assertIn("--require-hashes", script)
        self.assertIn('/usr/bin/python3 -I -m venv "$VERIFY_VENV"', script)
        self.assertIn('"$VERIFY_VENV/bin/python" -I -m pip check', script)
        self.assertNotIn('bin/python" -m pip', script)
        self.assertNotIn("/usr/bin/python3 -m venv", script)
        self.assertIn("requirements-platform.lock.txt", script)
        self.assertIn("Resolved Python freeze does not match the tracked lock", script)
        self.assertIn("requirements-platform.freeze.txt", script)
        self.assertIn('platform_validate_wheelhouse.py" create', script)
        self.assertIn('platform_validate_wheelhouse.py" verify', script)
        self.assertIn('platform_validate_release_artifact.py"', script)

    def test_build_derives_pip_wheel_from_tracked_lock(self) -> None:
        script = BUILD_SCRIPT.read_text()

        self.assertIn('PINNED_PIP_VERSION="$(\n', script)
        self.assertIn(
            '/usr/bin/python3 -I - "$STAGING_DIR/requirements-platform.lock.txt"',
            script,
        )
        self.assertIn(
            "Tracked Python lock must contain exactly one pinned pip version", script
        )
        self.assertIn(
            'PIP_WHEELS=("$WHEELHOUSE_DIR"/pip-"$PINNED_PIP_VERSION"-*.whl)',
            script,
        )
        self.assertNotIn("pip-26.1.2-", script)

    def test_checksum_record_is_portable_and_installer_validator_is_authoritative(
        self,
    ) -> None:
        build = BUILD_SCRIPT.read_text()
        install = (REPO_ROOT / "platform/tools/platform_release_install.sh").read_text()

        self.assertIn('cd "$OUTPUT_DIR"', build)
        self.assertIn('/usr/bin/sha256sum "$(basename "$ARTIFACT_PATH")"', build)
        self.assertIn("--extract-to", install)
        self.assertNotIn("sha256sum -c", install)
        self.assertIn('/usr/bin/python3 -I -m venv "$NEW_VENV_DIR"', install)
        self.assertIn("--no-index", install)
        self.assertIn('"$venv_dir/bin/python" -I -m pip check', install)
        self.assertIn(
            '--requirement "$RELEASE_DIR/requirements-platform.lock.txt"',
            install,
        )
        self.assertIn("--require-hashes", install)
        self.assertNotIn('"$SHARED_VENV_DIR/bin/pip" install', install)

    def test_build_lock_contention_exits_before_source_or_target_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "releases"
            output.mkdir()
            lock_fd = os.open(output, os.O_RDONLY)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    [str(BUILD_SCRIPT), "contention"],
                    cwd=REPO_ROOT,
                    env={**os.environ, "PLATFORM_RELEASE_OUTPUT_DIR": str(output)},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

            self.assertEqual(result.returncode, 3)
            self.assertIn("output lock", result.stderr)
            self.assertEqual(list(output.iterdir()), [])

    def test_dependency_baseline_cli_and_exact_comparison_contract(self) -> None:
        script = BUILD_SCRIPT.read_text()

        self.assertIn("--dependency-baseline", script)
        self.assertIn(
            "Dependency baseline must be a direct release in the output root", script
        )
        for relative in (
            "requirements-platform.txt",
            "requirements-platform.lock.txt",
            "requirements-platform.freeze.txt",
            "wheelhouse/WHEELHOUSE.sha256",
            "apps/platform_web/package-lock.json",
        ):
            self.assertIn(relative, script)
        self.assertIn("/usr/bin/cmp -s", script)
        self.assertIn(
            '"$(path_identity "$DEPENDENCY_BASELINE")" != "$BASELINE_ID"', script
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "releases"
            result = subprocess.run(
                [
                    str(BUILD_SCRIPT),
                    "--dependency-baseline",
                    "relative/release",
                    "enforce",
                ],
                cwd=REPO_ROOT,
                env={**os.environ, "PLATFORM_RELEASE_OUTPUT_DIR": str(output)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("absolute", result.stderr)
            self.assertFalse(output.exists())

    def test_baseline_hardlinked_file_is_rejected_before_build_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "releases"
            baseline = output / "candidate-20260811T120000Z"
            (baseline / "wheelhouse").mkdir(parents=True)
            (baseline / "apps/platform_web").mkdir(parents=True)
            for relative in (
                "requirements-platform.txt",
                "requirements-platform.lock.txt",
                "requirements-platform.freeze.txt",
                "wheelhouse/WHEELHOUSE.sha256",
                "apps/platform_web/package-lock.json",
            ):
                path = baseline / relative
                path.write_text("locked\n")
            os.link(
                baseline / "requirements-platform.txt",
                baseline / "requirements-platform.hardlink",
            )

            result = subprocess.run(
                [
                    str(BUILD_SCRIPT),
                    "--dependency-baseline",
                    str(baseline),
                    "enforce",
                ],
                cwd=REPO_ROOT,
                env={**os.environ, "PLATFORM_RELEASE_OUTPUT_DIR": str(output)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("metadata is unsafe", result.stderr)
            self.assertEqual(
                sorted(path.name for path in output.iterdir()),
                [baseline.name],
            )

    def test_build_uses_exclusive_promotions_and_records_exact_js_runtime(self) -> None:
        script = BUILD_SCRIPT.read_text()

        self.assertIn('/usr/bin/flock -n "$BUILD_LOCK_FD"', script)
        self.assertIn('/usr/bin/mv -nT -- "$STAGING_DIR" "$RELEASE_DIR"', script)
        self.assertIn('"node_version": node_version', script)
        self.assertIn('"npm_version": npm_version', script)
        self.assertIn('EXPECTED_NODE_VERSION="26.3.1"', script)
        self.assertIn('EXPECTED_NPM_VERSION="11.16.0"', script)
        self.assertIn('/usr/bin/chmod -R go-w -- "$STAGING_DIR"', script)
        self.assertIn("! -type l -perm /022 -print -quit", script)


if __name__ == "__main__":
    unittest.main()
