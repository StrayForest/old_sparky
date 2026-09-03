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
        self.assertIn("deadlock-logrotate.service", systemd_units)
        self.assertIn("deadlock-logrotate.timer", systemd_units)

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

    def test_baseline_runtime_profile_restores_ready_vote_admission_limits(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text()
        baseline_start = workflow.index("            baseline)")
        static_start = workflow.index(
            "            ready-vote-static-4|", baseline_start
        )
        baseline_branch = workflow[baseline_start:static_start]
        for key in (
            "PLATFORM_READY_VOTE_ADMISSION_MIN_CONCURRENCY",
            "PLATFORM_READY_VOTE_ADMISSION_INITIAL_CONCURRENCY",
            "PLATFORM_READY_VOTE_ADMISSION_MAX_CONCURRENCY",
        ):
            self.assertIn(f"--only {key}", baseline_branch)

    def test_auto_deploy_preserves_static_eight_runtime_profile(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-autodeploy.yml"
        ).read_text()
        self.assertIn(
            '"runtime_profile":"ready-vote-static-8"',
            workflow,
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

    def test_security_workflow_invokes_all_canonical_required_gates(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-security.yml").read_text()

        self.assertIn('".github/workflows/**"', workflow)
        self.assertIn("docs:", workflow)
        self.assertIn("platform_verify.py docs", workflow)
        for gate_id in (
            "backend",
            "python-quality",
            "security",
            "migration",
            "docs",
            "web-quality",
            "web-hermetic",
            "verification-contract",
        ):
            self.assertIn(f"platform_verify.py {gate_id}", workflow)
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

    def test_external_public_load_keeps_measurement_outside_origin(self) -> None:
        retired_production_workflow = (
            REPO_ROOT
            / ".github/workflows/platform-production-retained-load-matrix.yml"
        )
        self.assertFalse(
            retired_production_workflow.exists()
        )
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-external-load.yml"
        ).read_text()
        external_client = (
            REPO_ROOT / "platform/tools/platform_external_load.py"
        ).read_text()
        fixture = (
            REPO_ROOT / "platform/tools/platform_prepare_external_vote_fixture.py"
        ).read_text()
        supervisor = (
            REPO_ROOT / "platform/tools/platform_production_external_fixture_qa.sh"
        ).read_text()
        observer = (
            REPO_ROOT / "platform/tools/platform_external_load_observer.py"
        ).read_text()

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("RUN-PRODUCTION-EXTERNAL-LOAD", workflow)
        self.assertIn("external-vote", workflow)
        self.assertIn("platform_load.py", workflow)
        self.assertIn("profile_id", workflow)
        self.assertIn("platform_external_load_observer.py", supervisor)
        self.assertIn('EXTERNAL_CONFIRMATION="RUN-PRODUCTION-EXTERNAL-LOAD"', supervisor)
        self.assertIn("External-load fixture requires the dedicated external-load confirmation.", supervisor)
        self.assertIn("supports only the external-vote profile.", supervisor)
        self.assertNotIn('--mode read-mix', supervisor)
        self.assertNotIn('--mode write-burst', supervisor)
        self.assertIn("observer_deadline=$(( $(date +%s) + 10800 ))", supervisor)
        self.assertIn("ControlMaster auto", workflow)
        self.assertIn("ControlPersist 15m", workflow)
        self.assertIn("control_path=\"/tmp/old-sparky-external-load-ssh-$GITHUB_RUN_ID\"", workflow)
        self.assertIn("ControlPath %s", workflow)
        self.assertIn("Remove external-load SSH control socket", workflow)
        self.assertIn("platform_production_retained_load_cleanup_qa.sh", workflow)
        self.assertIn("Always invoke the exact supervisor", workflow)
        self.assertNotIn('echo \'{"ok":true,"fixture_absent":true}\'', workflow)
        cleanup_supervisor = (
            REPO_ROOT
            / "platform/tools/platform_production_retained_load_cleanup_qa.sh"
        ).read_text()
        self.assertIn(
            "for candidate_profile in read-mix write-burst external-vote",
            cleanup_supervisor,
        )
        self.assertIn(
            '[[ "$recovery_profile" == "external-vote" ]] && (( profile_count == 1 ))',
            cleanup_supervisor,
        )
        self.assertIn("recovery_profile/$recovery_profile.json", cleanup_supervisor)
        self.assertNotIn("manifest.json\n", workflow.split("Publish external load evidence", 1)[1])
        self.assertIn("ThreadPoolExecutor", external_client)
        self.assertIn("manual_refresh_count", external_client)
        self.assertIn("If-None-Match", external_client)
        self.assertIn("external_ready_vote", fixture)
        self.assertIn('LOCAL_API_ORIGIN = "http://127.0.0.1:8010"', fixture)
        self.assertIn('--local-origin "http://127.0.0.1:8010"', supervisor)
        self.assertIn("session_cookie_name", fixture)
        self.assertIn("csrf_cookie_name", fixture)
        self.assertIn("SystemSampler", observer)

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
