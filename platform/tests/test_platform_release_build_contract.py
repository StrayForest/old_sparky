from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import stat
import textwrap
import unittest
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "platform/tools/platform_build_release.sh"
TOOLS_DIR = REPO_ROOT / "platform/tools"
DEPLOY_SUPERVISOR = TOOLS_DIR / "platform_production_deploy_supervisor.sh"


def workflow_job(source: str, name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"workflow job is missing: {name}")
    return match.group("body")


class PlatformReleaseBuildContractTests(unittest.TestCase):
    @staticmethod
    def _copy_staged_live_qa_builder(root: Path) -> tuple[Path, Path]:
        tools = root / "candidate" / "tools"
        tools.mkdir(parents=True, mode=0o755)
        for name in (
            "platform_build_live_qa_runtime.py",
            "platform_live_qa_guard.py",
        ):
            destination = tools / name
            shutil.copyfile(TOOLS_DIR / name, destination)
            os.chmod(destination, 0o644)
        return (
            tools / "platform_build_live_qa_runtime.py",
            tools / "platform_live_qa_guard.py",
        )

    @staticmethod
    def _run_staged_live_qa_help(builder: Path, root: Path) -> subprocess.CompletedProcess[str]:
        decoy = root / "decoy"
        decoy.mkdir(mode=0o755)
        (decoy / "platform_live_qa_guard.py").write_text(
            "raise RuntimeError('ambient guard loaded')\n",
            encoding="utf-8",
        )
        environment = {
            "HOME": str(root / "home"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(decoy),
        }
        return subprocess.run(
            ["/usr/bin/python3", "-I", str(builder), "--help"],
            cwd=decoy,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_staged_live_qa_builder_import_is_hermetic(self) -> None:
        build_script = BUILD_SCRIPT.read_text()
        runtime_source = (TOOLS_DIR / "platform_build_live_qa_runtime.py").read_text()
        self.assertIn(
            '"$ROOT_DIR/.venv_platform/bin/python" -I \\\n'
            '  "$STAGING_DIR/tools/platform_build_live_qa_runtime.py"',
            build_script,
        )
        self.assertIn("importlib.util.spec_from_file_location", runtime_source)
        self.assertNotIn("sys.path.insert", runtime_source)
        self.assertNotIn("PYTHONPATH", runtime_source)

        with tempfile.TemporaryDirectory() as temporary:
            builder, _guard = self._copy_staged_live_qa_builder(Path(temporary))
            completed = self._run_staged_live_qa_help(builder, Path(temporary))

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--platform-root", completed.stdout)
        self.assertNotIn("ambient guard loaded", completed.stderr)

    def test_staged_live_qa_builder_rejects_unsafe_guard_metadata(self) -> None:
        for mutation in ("symlink", "writable"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                builder, guard = self._copy_staged_live_qa_builder(Path(temporary))
                if mutation == "symlink":
                    decoy = Path(temporary) / "guard-decoy.py"
                    decoy.write_text("# decoy\n", encoding="utf-8")
                    guard.unlink()
                    guard.symlink_to(decoy)
                else:
                    os.chmod(guard, 0o666)

                completed = self._run_staged_live_qa_help(
                    builder, Path(temporary)
                )

            self.assertNotEqual(completed.returncode, 0)
            diagnostic = json.loads(completed.stderr)
            self.assertEqual(diagnostic["phase"], "validate-input")
            self.assertEqual(diagnostic["reason"], "invalid-input")
            self.assertEqual(diagnostic["cleanup"], "not-needed")
            self.assertNotIn("Traceback", completed.stderr)

    @staticmethod
    def _write_fixture_file(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_bytes(payload)
        os.chown(path, 0, 0)
        os.chmod(path, mode)

    @staticmethod
    def _write_fixture_zip(
        path: Path,
        entries: list[tuple[str, bytes, str]],
    ) -> None:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, payload, kind in entries:
                if kind == "directory":
                    info = zipfile.ZipInfo(name.rstrip("/") + "/")
                    info.create_system = 3
                    info.external_attr = (stat.S_IFDIR | 0o755) << 16
                    archive.writestr(info, b"")
                elif kind == "symlink":
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, payload)
                elif kind == "special":
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFIFO | 0o644) << 16
                    archive.writestr(info, payload)
                else:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    archive.writestr(info, payload)
        os.chown(path, 0, 0)
        os.chmod(path, 0o644)

    @classmethod
    def _prepare_local_runtime_fixture(
        cls,
        root: Path,
        *,
        webkit_entries: list[tuple[str, bytes, str]] | None = None,
    ) -> tuple[Path, Path, Path, Path, bytes]:
        builder, guard = cls._copy_staged_live_qa_builder(root)
        platform_root = root / "platform"
        web = platform_root / "apps/platform_web"
        web.mkdir(mode=0o755, parents=True)
        node_home = root / "node"
        (node_home / "bin").mkdir(mode=0o755, parents=True)
        cls._write_fixture_file(node_home / "bin/node", b"#!/bin/sh\n", mode=0o755)
        for relative in (
            "playwright.live.config.ts",
            "tests/smoke/live-user-journey.spec.ts",
            "tests/support/live-qa-origin.ts",
            "tests/support/live-qa-sandbox.ts",
            "package-lock.json",
        ):
            cls._write_fixture_file(web / relative, relative.encode("ascii"))
        for package in ("@playwright/test", "playwright", "playwright-core"):
            cls._write_fixture_file(
                web / "node_modules" / package / "package.json",
                ("{\"name\":%r}\n" % package).encode("ascii"),
            )

        sandbox = b"small pinned sandbox fixture\n"
        archive_entries = {
            "chromium-1228": [
                ("chrome-linux64/chrome_sandbox", sandbox, "file"),
                ("chrome-linux64/chrome", b"chromium\n", "file"),
            ],
            "chromium_headless_shell-1228": [("chrome-headless-shell", b"headless\n", "file")],
            "webkit-2311": webkit_entries
            or [
                ("lib/real", b"webkit\n", "file"),
                ("lib/alias", b"real", "symlink"),
                ("lib/chain", b"alias", "symlink"),
            ],
            "ffmpeg-1011": [("ffmpeg", b"ffmpeg\n", "file")],
        }
        archives: list[tuple[str, str, str, int]] = []
        for name, entries in archive_entries.items():
            archive = root / f"{name}.zip"
            cls._write_fixture_zip(archive, entries)
            archives.append(
                (
                    name,
                    archive.as_uri(),
                    hashlib.sha256(archive.read_bytes()).hexdigest(),
                    archive.stat().st_size,
                )
            )

        guard_source = guard.read_text(encoding="utf-8")
        archives_start = guard_source.index("PLAYWRIGHT_ARCHIVES = (")
        archives_end = guard_source.index(
            "\n)\nCHROMIUM_SANDBOX_RELATIVE", archives_start
        ) + 2
        archive_literal = "PLAYWRIGHT_ARCHIVES = (\n" + "".join(
            f"    {row!r},\n" for row in archives
        ) + ")"
        guard_source = (
            guard_source[:archives_start]
            + archive_literal
            + guard_source[archives_end:]
        )
        sandbox_start = guard_source.index("CHROMIUM_SANDBOX_RELATIVE =")
        sandbox_end = guard_source.index("\nSTATE_NAME_PATTERN", sandbox_start)
        sandbox_literal = (
            "CHROMIUM_SANDBOX_RELATIVE = Path("
            "'browsers/chromium-1228/chrome-linux64/chrome_sandbox')\n"
            f"CHROMIUM_SANDBOX_SIZE = {len(sandbox)}\n"
            f"CHROMIUM_SANDBOX_SHA256 = {hashlib.sha256(sandbox).hexdigest()!r}\n"
        )
        guard_source = guard_source[:sandbox_start] + sandbox_literal + guard_source[sandbox_end:]
        guard.write_text(guard_source, encoding="utf-8")
        os.chown(guard, 0, 0)
        os.chmod(guard, 0o644)
        # The installer validates the source member name as well as its tree;
        # use the canonical staged directory name so this is a real
        # downstream-contract check rather than a fixture-only tree walk.
        output = root / "liveqa-runtime"
        return builder, platform_root, node_home, output, sandbox

    @staticmethod
    def _run_staged_live_qa_build(
        builder: Path,
        platform_root: Path,
        node_home: Path,
        output: Path,
        root: Path,
    ) -> subprocess.CompletedProcess[str]:
        decoy = root / "build-decoy"
        decoy.mkdir(mode=0o755)
        (decoy / "platform_live_qa_guard.py").write_text(
            "raise RuntimeError('ambient guard loaded')\n",
            encoding="utf-8",
        )
        environment = {
            "HOME": str(root / "home"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(decoy),
        }
        return subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                str(builder),
                "--platform-root",
                str(platform_root),
                "--node-home",
                str(node_home),
                "--output",
                str(output),
            ],
            cwd=decoy,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_staged_live_qa_build_materializes_validated_browser_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder, platform_root, node_home, output, sandbox = (
                self._prepare_local_runtime_fixture(root)
            )
            completed = self._run_staged_live_qa_build(
                builder, platform_root, node_home, output, root
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            diagnostic = json.loads(completed.stdout)
            self.assertEqual(
                set(diagnostic),
                {"schema", "phase", "status", "reason", "cleanup", "tree_sha256"},
            )
            self.assertEqual(diagnostic["phase"], "complete")
            self.assertEqual(diagnostic["status"], "passed")
            self.assertEqual(diagnostic["reason"], "ok")
            self.assertEqual(diagnostic["cleanup"], "not-needed")
            manifest = json.loads((output / "runtime-manifest.json").read_text())
            self.assertEqual(manifest["tree_sha256"], diagnostic["tree_sha256"])
            self.assertEqual(
                (output / "browsers/webkit-2311/lib/alias").read_bytes(),
                b"webkit\n",
            )
            self.assertEqual(
                (output / "browsers/webkit-2311/lib/chain").read_bytes(),
                b"webkit\n",
            )
            self.assertFalse(any(path.is_symlink() for path in output.rglob("*")))
            self.assertEqual(
                stat.S_IMODE((output / "browsers/chromium-1228/chrome-linux64/chrome_sandbox").stat().st_mode),
                0o4755,
            )
            self.assertEqual(
                hashlib.sha256(
                    (output / "browsers/chromium-1228/chrome-linux64/chrome_sandbox").read_bytes()
                ).digest(),
                hashlib.sha256(sandbox).digest(),
            )
            for path in output.rglob("*"):
                metadata = path.lstat()
                if stat.S_ISREG(metadata.st_mode):
                    self.assertEqual(metadata.st_nlink, 1, path)
                    if path.name != "chrome_sandbox":
                        self.assertIn(stat.S_IMODE(metadata.st_mode), {0o444, 0o555})
                else:
                    self.assertTrue(stat.S_ISDIR(metadata.st_mode), path)

            installer_path = TOOLS_DIR / "platform_live_qa_runtime_install.py"
            spec = importlib.util.spec_from_file_location("fixture_runtime_install", installer_path)
            self.assertIsNotNone(spec)
            assert spec is not None and spec.loader is not None
            installer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(installer)
            installer.CHROMIUM_SANDBOX_SIZE = len(sandbox)
            installer.CHROMIUM_SANDBOX_SHA256 = hashlib.sha256(sandbox).hexdigest()
            installer._validate_runtime_source(output)

    def test_staged_live_qa_build_fails_closed_for_browser_link_inputs(self) -> None:
        negative_cases = {
            "absolute": [("alias", b"/real", "symlink")],
            "nul": [("alias", b"real\x00tail", "symlink")],
            "backslash": [("alias", b"..\\real", "symlink")],
            "escape": [("alias", b"../outside", "symlink")],
            "dangling": [("alias", b"missing", "symlink")],
            "cycle": [
                ("a", b"b", "symlink"),
                ("b", b"a", "symlink"),
            ],
            "chain-nonregular": [
                ("target", b"", "directory"),
                ("alias", b"target", "symlink"),
            ],
            "special": [
                ("fifo", b"", "special"),
            ],
        }
        for case, entries in negative_cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                builder, platform_root, node_home, output, _sandbox = (
                    self._prepare_local_runtime_fixture(root, webkit_entries=entries)
                )
                completed = self._run_staged_live_qa_build(
                    builder, platform_root, node_home, output, root
                )
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                diagnostic = json.loads(completed.stderr)
                self.assertEqual(
                    set(diagnostic),
                    {"schema", "phase", "status", "reason", "cleanup"},
                )
                self.assertEqual(diagnostic["status"], "failed")
                self.assertEqual(diagnostic["cleanup"], "passed")
                self.assertNotIn(str(root), completed.stderr)
                self.assertNotIn("file://", completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)
                self.assertFalse(output.exists())

    def test_browser_materializer_rejects_filesystem_metadata_and_specials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder, _platform_root, _node_home, _output, _sandbox = (
                self._prepare_local_runtime_fixture(root)
            )
            spec = importlib.util.spec_from_file_location("fixture_runtime_builder", builder)
            self.assertIsNotNone(spec)
            assert spec is not None and spec.loader is not None
            runtime_builder = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runtime_builder)

            def reset() -> Path:
                browser = root / "manual-browser"
                if browser.exists():
                    shutil.rmtree(browser)
                browser.mkdir(mode=0o755)
                return browser

            cases = {
                "hardlink": lambda browser: (
                    (browser / "target").write_bytes(b"x"),
                    os.link(browser / "target", browser / "hard-target"),
                    (browser / "alias").symlink_to("hard-target"),
                ),
                "setid": lambda browser: (
                    (browser / "target").write_bytes(b"x"),
                    os.chmod(browser / "target", 0o4644),
                    (browser / "alias").symlink_to("target"),
                ),
                "mode": lambda browser: (
                    (browser / "target").write_bytes(b"x"),
                    os.chmod(browser / "target", 0o664),
                    (browser / "alias").symlink_to("target"),
                ),
                "ownership": lambda browser: (
                    (browser / "target").write_bytes(b"x"),
                    os.chown(browser / "target", 65534, 65534),
                    (browser / "alias").symlink_to("target"),
                ),
                "symlink-hardlink": lambda browser: (
                    (browser / "target").write_bytes(b"x"),
                    (browser / "alias").symlink_to("target"),
                    os.link(browser / "alias", browser / "alias-hardlink", follow_symlinks=False),
                ),
                "symlink-ownership": lambda browser: (
                    (browser / "target").write_bytes(b"x"),
                    (browser / "alias").symlink_to("target"),
                    os.chown(browser / "alias", 65534, 65534, follow_symlinks=False),
                ),
            }

            for reason, setup in cases.items():
                with self.subTest(reason=reason):
                    browser = reset()
                    setup(browser)
                    with self.assertRaises(RuntimeError) as raised:
                        runtime_builder._materialize_browser_tree(browser, total_before=0)
                    self.assertEqual(raised.exception.reason, {
                        "hardlink": "link-hardlink",
                        "setid": "link-mode",
                        "mode": "link-mode",
                        "ownership": "link-ownership",
                        "symlink-hardlink": "link-hardlink",
                        "symlink-ownership": "link-ownership",
                    }[reason])

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
        self.assertIn("platform_release_lock.sh", release_installer)
        self.assertIn("platform_release_lock.sh", deploy)
        self.assertIn("platform_release_lock.sh", rollback)
        self.assertIn(".release-recovery", rollback)
        self.assertIn("install_recovery_shim", rollback)
        recovery_shim = (
            REPO_ROOT / "platform/tools/platform_release_recovery_shim.sh"
        ).read_text()
        self.assertIn('RECOVERY_DIR="$SHARED_DIR/.release-recovery"', recovery_shim)
        self.assertIn('RECOVERY_TOOL="$RECOVERY_DIR/platform_release_rollback.sh"', recovery_shim)
        self.assertIn("platform_release_lock.sh", recovery_shim)

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
        self.assertIn("classifier_run_id is required for production deploy", workflow)
        self.assertIn("classifier_run_attempt is required for production deploy", workflow)
        self.assertIn(
            "actions/workflows/platform-security.yml",
            workflow,
        )
        self.assertIn('run.get("workflow_id") != workflow.get("id")', workflow)
        for field in (
            '"event": "push"',
            '"head_branch": "dev"',
            '"head_sha": os.environ["TARGET_SHA"]',
            '"status": "completed"',
            '"conclusion": "success"',
        ):
            self.assertIn(field, workflow)
        self.assertIn('run.get("run_attempt") != int(expected_attempt)', workflow)
        self.assertIn(
            '"${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/commits/${TARGET_SHA}/statuses?per_page=100&page=${page}"',
            workflow,
        )
        self.assertIn("fetch_statuses", workflow)
        self.assertIn("paginated status response is malformed", workflow)
        self.assertIn("status pagination exceeded its bound", workflow)
        self.assertIn('local pages_dir="$output_path.pages"', workflow)
        self.assertIn('fetch_snapshot "$provenance_dir/$snapshot"', workflow)
        self.assertIn('rm -rf -- "$pages_dir"', workflow)
        self.assertNotIn(
            'local pages_dir="$provenance_dir/pages-$(basename "$output_path")"',
            workflow,
        )
        self.assertNotIn('item.get("context") == "platform-security-build"', workflow)
        self.assertNotIn('latest.get("state") != "success"', workflow)
        self.assertNotIn('latest.get("target_url") != attempt_url', workflow)
        self.assertLess(
            workflow.index("Require successful platform security build"),
            workflow.index("Mark production deployment pending"),
        )

    def test_production_web_compression_is_explicit_and_enabled_by_default(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text()
        build_script = BUILD_SCRIPT.read_text()
        self.assertIn("web_compression:", workflow)
        self.assertIn("default: enabled", workflow)
        self.assertIn("- disabled", workflow)
        self.assertIn(
            "PLATFORM_WEB_NEXT_COMPRESSION: ${{ inputs.web_compression == 'disabled' && 'false' || 'true' }}",
            workflow,
        )
        self.assertIn(
            'PLATFORM_WEB_NEXT_COMPRESSION="$PLATFORM_WEB_NEXT_COMPRESSION"',
            workflow,
        )
        self.assertIn(
            'WEB_NEXT_COMPRESSION="${PLATFORM_WEB_NEXT_COMPRESSION:-true}"',
            build_script,
        )
        self.assertIn(
            'PLATFORM_WEB_NEXT_COMPRESSION="$WEB_NEXT_COMPRESSION"',
            build_script,
        )

    def test_auto_deploy_keeps_compression_enabled(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-autodeploy.yml"
        ).read_text()
        self.assertIn('"web_compression":"enabled"', workflow)

    def test_baseline_runtime_profile_restores_ready_vote_admission_limits(self) -> None:
        workflow = DEPLOY_SUPERVISOR.read_text()
        baseline_start = workflow.index("  baseline)")
        static_start = workflow.index(
            "  ready-vote-static-4|", baseline_start
        )
        baseline_branch = workflow[baseline_start:static_start]
        for key in (
            "PLATFORM_LOG_LEVEL",
            "PLATFORM_PERF_LOG_ENABLED",
            "PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED",
            "PLATFORM_READY_VOTE_ADMISSION_MIN_CONCURRENCY",
            "PLATFORM_READY_VOTE_ADMISSION_INITIAL_CONCURRENCY",
            "PLATFORM_READY_VOTE_ADMISSION_MAX_CONCURRENCY",
        ):
            self.assertIn(f"--only {key}", baseline_branch)
        adaptive_start = workflow.index(
            "  ready-vote-adaptive-v2)", static_start
        )
        static_branch = workflow[static_start:adaptive_start]
        for key in (
            "PLATFORM_LOG_LEVEL",
            "PLATFORM_PERF_LOG_ENABLED",
            "PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED",
        ):
            self.assertIn(f"--only {key}", static_branch)

    def test_ssr_runtime_profiles_restart_the_web_process(self) -> None:
        workflow = DEPLOY_SUPERVISOR.read_text()
        self.assertIn("restart_web_and_wait()", workflow)
        self.assertIn(
            'baseline|ready-vote-static-*|ready-vote-cprofile|ready-vote-adaptive-v2',
            workflow,
        )
        self.assertIn("deadlock-web did not recover after runtime profile", workflow)
        lock_helper = workflow.index(
            'lock_helper="$runtime/current/tools/platform_release_lock.sh"'
        )
        release_supervisor = workflow.index(
            "platform_release_lock_supervise", lock_helper
        )
        retained_load_lock = workflow.index(
            "platform_retained_load_lock_open", release_supervisor
        )
        candidate = workflow.index(
            'candidate_deploy="$bootstrap_dir/$artifact_slug/tools/platform_release_deploy.sh"'
        )
        profile = workflow.index('case "$runtime_profile" in', candidate)
        final_lock_check = workflow.index(
            "platform_release_lock_supervisor_holds", candidate
        )
        self.assertLess(lock_helper, release_supervisor)
        self.assertLess(release_supervisor, retained_load_lock)
        self.assertLess(retained_load_lock, candidate)
        self.assertLess(candidate, final_lock_check)
        self.assertLess(final_lock_check, profile)
        self.assertNotIn("exec 8>/run/lock/oldsparky-retained-load-matrix.lock", workflow)
        self.assertNotIn("flock -n 9", workflow)
        self.assertNotIn("PLATFORM_RELEASE_LOCK_FD=9", workflow)

    def test_ssr_diagnostics_restarts_api_after_applying_api_log_gate(self) -> None:
        workflow = DEPLOY_SUPERVISOR.read_text()
        self.assertIn("restart_api_and_wait()", workflow)
        self.assertIn("http://127.0.0.1:8010/api/v1/health/ready", workflow)
        diagnostics_start = workflow.index("  web-ssr-diagnostics)")
        diagnostics_end = workflow.index(
            "  web-ssr-workers-2)", diagnostics_start
        )
        diagnostics_branch = workflow[diagnostics_start:diagnostics_end]
        self.assertGreaterEqual(
            diagnostics_branch.count("restart_api_and_wait"),
            2,
        )
        self.assertIn(
            "--only PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED",
            diagnostics_branch,
        )
        self.assertIn("--only PLATFORM_LOG_LEVEL", diagnostics_branch)
        self.assertIn("--only PLATFORM_PERF_LOG_ENABLED", diagnostics_branch)
        self.assertIn(
            "grep -qx 'PLATFORM_LOG_LEVEL=INFO' \"$api_env\"",
            diagnostics_branch,
        )
        self.assertIn(
            "grep -qx 'PLATFORM_PERF_LOG_ENABLED=true' \"$api_env\"",
            diagnostics_branch,
        )
        self.assertIn(
            "grep -qx 'PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED=true' \"$api_env\"",
            diagnostics_branch,
        )

    def test_auto_deploy_preserves_static_eight_runtime_profile(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-autodeploy.yml"
        ).read_text()
        self.assertIn(
            '"runtime_profile":"ready-vote-static-8"',
            workflow,
        )

    def test_production_preflight_requires_edge_parity_before_preflight_exit(self) -> None:
        workflow = DEPLOY_SUPERVISOR.read_text()
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
        supervisor = DEPLOY_SUPERVISOR.read_text()
        workflow += "\n" + supervisor
        self.assertIn("Build immutable release artifact in CI", workflow)
        self.assertIn("actions/upload-artifact", workflow)
        self.assertIn("actions/download-artifact", workflow)
        self.assertIn("PUBLISHED_ARTIFACT_DIGEST", workflow)
        self.assertIn("RELEASE.provenance.json", workflow)
        self.assertIn("artifact_sha256", workflow)
        self.assertIn('ci_build_root=/root/old_sparky', workflow)
        build_step_start = workflow.index(
            "      - name: Build immutable release artifact in CI"
        )
        build_step_end = workflow.index(
            "      - name: Publish immutable release artifact",
            build_step_start,
        )
        build_step = workflow[build_step_start:build_step_end]
        secure_env_allowlist = (
            "          sudo env -i \\\n"
            "            HOME=/root \\\n"
            "            LANG=C.UTF-8 \\\n"
            "            LC_ALL=C.UTF-8 \\\n"
            "            PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \\\n"
        )
        self.assertEqual(build_step.count(secure_env_allowlist), 2)
        self.assertNotIn("sudo -E", build_step)
        for ssh_secret_name in (
            "PROD_SSH_HOST",
            "PROD_SSH_USER",
            "PROD_SSH_KEY",
        ):
            self.assertNotIn(ssh_secret_name, build_step)
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
        self.assertIn("candidate_activation_failure", workflow)
        self.assertIn("storage_summary_tool", workflow)
        self.assertIn(
            "df -B1 --output=size,used,avail,pcent -- \"$path\"", workflow
        )
        self.assertIn(
            "df --output=iused,iavail,ipcent -- \"$path\"", workflow
        )
        self.assertNotIn("df -hT", workflow)
        self.assertNotIn("findmnt", workflow)
        self.assertNotIn("journalctl -u \"$service\"", workflow)
        self.assertIn('systemctl show "$service"', workflow)
        self.assertIn('fail "candidate release activation failed; diagnostics summarized"', workflow)
        self.assertIn('"error_class":"activation"', workflow)
        remote_script = supervisor
        self.assertNotIn("platform_build_release.sh", remote_script)
        self.assertNotIn("pip install -r platform/requirements-platform.lock.txt", remote_script)
        validator = (REPO_ROOT / "platform/tools/platform_validate_release_artifact.py").read_text()
        self.assertIn("source_git_commit", workflow)
        self.assertIn("expected source commit", validator)
        recover = (
            REPO_ROOT / ".github/workflows/platform-production-release-recover.yml"
        ).read_text()
        recover_lock = recover.index(
            'lock_helper="$runtime/current/tools/platform_release_lock.sh"'
        )
        recover_supervisor = recover.index(
            '"$lock_helper" --run /bin/bash -s <<\'LOCKED\'', recover_lock
        )
        recover_retained = recover.index(
            'recover \\\n            --retain --state "$state"',
            recover_supervisor,
        )
        recover_restore = recover.index('PLATFORM_ENABLE_SYSTEMD_UNITS=0 "$restore"', recover_retained)
        recover_complete = recover.index(
            'complete-recovery \\\n            --state "$state"',
            recover_restore,
        )
        recover_health = recover.index('nginx -t', recover_restore)
        recover_verify = recover.index('verify --state "$systemd_state"', recover_restore)
        self.assertLess(recover_lock, recover_retained)
        self.assertLess(recover_retained, recover_restore)
        self.assertLess(recover_verify, recover_health)
        self.assertLess(recover_health, recover_complete)
        self.assertLess(recover_supervisor, recover_retained)
        self.assertNotIn("exec 9<", recover)
        self.assertNotIn("flock -n 9", recover)
        self.assertNotIn("PLATFORM_RELEASE_LOCK_FD=9", recover)
        self.assertIn("Retained release transaction disappeared during recovery", recover)
        self.assertNotIn('"$rollback" --recover-pending', recover)
        self.assertIn("capture-transaction", recover)
        self.assertIn('systemd_state="$runtime/shared/.release-systemd-state.json"', recover)
        self.assertIn('--systemd-state "$systemd_state"', recover)
        self.assertIn('PLATFORM_ENABLE_SYSTEMD_UNITS=0 "$restore"', recover)
        self.assertIn('clear --state "$systemd_state"', recover)
        self.assertIn('complete-recovery', recover)
        self.assertIn("Recovered current release does not match retained receipt", recover)
        self.assertIn("Retained active state is invalid", recover)
        self.assertNotIn('for service in deadlock-api deadlock-worker deadlock-web; do', recover)

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

        self.assertNotRegex(workflow, r"^\s{4}paths(?:-ignore)?:")
        self.assertIn("merge_group:", workflow)
        self.assertIn("platform_ci_classifier.py", workflow)
        self.assertIn("classifier-manifest.json", workflow)
        self.assertIn("if: ${{ always() }}", workflow)
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
        self.assertIn("runtime_sensitive", workflow)
        self.assertIn("platform_verify.py release-runtime", workflow)
        self.assertIn("release-runtime-real:", workflow)
        self.assertIn("name: Conditional release runtime fixture", workflow)
        self.assertIn("name: Trusted dev immutable release runtime", workflow)
        self.assertIn("platform_build_release.sh", workflow)
        self.assertIn("platform_release_build_diagnostics.py", workflow)
        self.assertIn("RELEASE_RUNTIME_BUILD_DIAGNOSTIC", workflow)
        self.assertIn("canonical_builder_rc", workflow)
        self.assertIn("parser_rc", workflow)
        self.assertIn("consistency", workflow)
        self.assertIn("diagnostic_sanitizer", workflow)
        real = workflow_job(workflow, "release-runtime-real")
        self.assertIn("builder_started=0", real)
        self.assertIn("local builder_run_ready=0", real)
        self.assertIn(
            'if (( builder_started == 1 )) && [[ -n "$canonical_rc" && -n "$build_log" ]]',
            real,
        )
        self.assertIn(
            "if (( builder_run_ready == 1 && parser_rc != 0 )); then",
            real,
        )
        self.assertIn("reason=builder_not_started", real)
        self.assertNotIn("tee", real)
        self.assertNotIn('cat "$build_log"', real)
        self.assertNotIn("BASH_COMMAND", real)
        self.assertNotIn("actions/upload-artifact@", real)
        self.assertLess(
            real.index("diagnostic_parser"),
            real.index('/bin/rm -rf -- "$release_root"'),
        )
        self.assertEqual(real.count("canonical_builder_rc=$?"), 1)
        self.assertLess(
            real.index("canonical_builder_rc=$?"),
            real.index("archive_candidates"),
        )
        sanitizer_match = re.search(
            r"<<'PY'\n(?P<script>.*?)\n\s*PY\n",
            real,
            re.DOTALL,
        )
        self.assertIsNotNone(sanitizer_match)
        sanitizer = textwrap.dedent(sanitizer_match.group("script"))
        passed_marker = (
            "RELEASE_BUILD_DIAGNOSTIC schema=1 phase=complete status=passed "
            "reason=ok cleanup=passed "
            f"source_sha={'a' * 40} artifact_sha256={'b' * 64}"
        )
        failed_marker = (
            "RELEASE_BUILD_DIAGNOSTIC schema=1 phase=complete status=failed "
            "reason=build_failed cleanup=passed failed_phase=web-build"
        )
        for marker, canonical_rc, expected_rc in (
            (passed_marker, "0", 0),
            (failed_marker, "3", 0),
            (passed_marker, "3", 1),
            (passed_marker, "", 1),
        ):
            with self.subTest(canonical_rc=canonical_rc, marker=marker):
                sanitized = subprocess.run(
                    ["/usr/bin/python3", "-I", "-", marker, canonical_rc, "0"],
                    input=sanitizer,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(sanitized.returncode, expected_rc, sanitized.stderr)
                self.assertIn(
                    f"canonical_builder_rc={canonical_rc or 'unknown'}", sanitized.stdout
                )
        late_validation = subprocess.run(
            ["/usr/bin/python3", "-I", "-", passed_marker, "0", "0"],
            input=sanitizer,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(late_validation.returncode, 0, late_validation.stderr)
        self.assertIn("canonical_builder_rc=0", late_validation.stdout)

        run_start = real.index("        run: |\n") + len("        run: |\n")
        run_lines = []
        for line in real[run_start:].splitlines():
            if line and not line.startswith("          "):
                break
            run_lines.append(line[10:] if line.startswith("          ") else "")
        run_script = "\n".join(run_lines)
        cleanup_start = run_script.index("cleanup() {")
        cleanup_end = run_script.index("\ntrap cleanup EXIT", cleanup_start)
        cleanup_script = run_script[cleanup_start:cleanup_end]
        with tempfile.TemporaryDirectory() as temporary:
            cleanup_fixture = f"""#!/usr/bin/env bash
set -u
runner_temp={temporary!r}
RUNNER_TEMP={temporary!r}
min_free_bytes=1
failure_reason=python_environment_failed
release_root=""
release_root_id=""
artifact_sha256=""
disk_before_bytes=""
disk_after_bytes=""
build_log=""
canonical_builder_rc=""
builder_started=0
diagnostic_parser=/no/such/parser
{cleanup_script}
free_bytes() {{ printf '9999999999\\n'; }}
set +e
false
cleanup
"""
            prebuilder = subprocess.run(
                ["/bin/bash"],
                input=cleanup_fixture,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(prebuilder.returncode, 1, prebuilder.stderr)
        self.assertIn(
            "RELEASE_RUNTIME_BUILD schema=1 status=failed reason=python_environment_failed",
            prebuilder.stdout,
        )
        self.assertNotIn("diagnostic_parser", prebuilder.stdout)
        self.assertNotIn(temporary, prebuilder.stdout)
        with tempfile.TemporaryDirectory() as builder_temporary:
            builder_log = Path(builder_temporary) / "canonical-builder.log"
            builder_log.write_text("builder output is private\n")
            builder_run = cleanup_fixture.replace(temporary, builder_temporary).replace(
                'build_log=""\ncanonical_builder_rc=""\nbuilder_started=0',
                f'build_log={str(builder_log)!r}\ncanonical_builder_rc=0\nbuilder_started=1',
            )
            builder_parser_failure = subprocess.run(
                ["/bin/bash"],
                input=builder_run,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(
                builder_parser_failure.returncode,
                1,
                builder_parser_failure.stderr,
            )
            self.assertIn(
                "RELEASE_RUNTIME_BUILD schema=1 status=failed reason=diagnostic_parser",
                builder_parser_failure.stdout,
            )
        builder = (REPO_ROOT / "platform/tools/platform_build_release.sh").read_text()
        self.assertIn("RELEASE_BUILD_PHASE", builder)
        for phase in (
            "canonical-preflight",
            "node-runtime",
            "source-stage",
            "web-dependencies",
            "live-qa-runtime",
            "python-wheelhouse",
            "dependency-baseline",
            "web-build",
            "release-metadata",
            "artifact-promote",
            "artifact-validate",
            "cleanup",
            "complete",
        ):
            self.assertIn(phase, builder)
        self.assertNotIn("BASH_COMMAND", builder)
        self.assertIn("needs['release-runtime-real'].result", workflow)

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
        remote_dispatch = (
            REPO_ROOT / "platform/tools/platform_workflow_remote_dispatch.py"
        ).read_text()

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("RUN-PRODUCTION-EXTERNAL-LOAD", workflow)
        self.assertIn("external-vote", workflow)
        self.assertIn("platform_load.py", workflow)
        self.assertIn("profile_id", workflow)
        self.assertIn("platform_external_load_observer.py", supervisor)
        self.assertIn(
            'platform_workflow_input_guard.py" email',
            supervisor,
        )
        self.assertIn('EXTERNAL_CONFIRMATION="RUN-PRODUCTION-EXTERNAL-LOAD"', supervisor)
        self.assertIn("External-load fixture requires the dedicated external-load confirmation.", supervisor)
        self.assertIn("supports only the external-vote profile.", supervisor)
        self.assertNotIn('--mode read-mix', supervisor)
        self.assertNotIn('--mode write-burst', supervisor)
        self.assertIn("observer_deadline=$(( $(date +%s) + 10800 ))", supervisor)
        self.assertIn("ControlMaster auto", workflow)
        self.assertIn("ControlPersist 15m", workflow)
        self.assertIn(
            'control_path="/tmp/old-sparky-external-load-ssh-setup-$GITHUB_RUN_ID"',
            workflow,
        )
        self.assertIn(
            'control_path="/tmp/old-sparky-external-load-ssh-finalize-$GITHUB_RUN_ID"',
            workflow,
        )
        self.assertIn("ControlPath %s", workflow)
        self.assertIn("Remove fixture-setup SSH material", workflow)
        self.assertIn("Remove finalizer SSH material", workflow)
        self.assertIn("platform_workflow_remote_dispatch.py", workflow)
        self.assertIn(
            'EXTERNAL_HELPER = ACTIVE_TOOLS_DIR / "platform_production_external_fixture_qa.sh"',
            remote_dispatch,
        )
        self.assertIn(
            'CLEANUP_HELPER = ACTIVE_TOOLS_DIR / "platform_production_retained_load_cleanup_qa.sh"',
            remote_dispatch,
        )
        self.assertIn(
            'DEPLOY_HELPER = ACTIVE_TOOLS_DIR / "platform_production_deploy_supervisor.sh"',
            remote_dispatch,
        )
        self.assertIn('retained-cleanup-exports', remote_dispatch)
        deploy_supervisor = DEPLOY_SUPERVISOR.read_text()
        self.assertIn('case "$deploy_mode" in', deploy_supervisor)
        self.assertIn('case "$runtime_profile" in', deploy_supervisor)
        self.assertIn('[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]]', deploy_supervisor)
        self.assertIn('[[ "$release_slug" =~ ^[A-Za-z0-9]', deploy_supervisor)
        self.assertNotIn('echo \'{"ok":true,"fixture_absent":true}\'', workflow)
        cleanup_supervisor = (
            REPO_ROOT
            / "platform/tools/platform_production_retained_load_cleanup_qa.sh"
        ).read_text()
        self.assertIn('platform_workflow_input_guard.py" email', cleanup_supervisor)
        self.assertIn('PLATFORM_ROOT="$RUNTIME_ROOT/current"', supervisor)
        self.assertIn('PLATFORM_ROOT="$RUNTIME_ROOT/current"', cleanup_supervisor)
        self.assertIn('QA_PYTHON="$RUNTIME_ROOT/shared/venv/bin/python"', supervisor)
        self.assertIn('QA_PYTHON="$RUNTIME_ROOT/shared/venv/bin/python"', cleanup_supervisor)
        self.assertNotIn("TRUSTED_REPO_ROOT", supervisor)
        self.assertNotIn("TRUSTED_REPO_ROOT", cleanup_supervisor)
        self.assertIn(
            "for candidate_profile in read-mix write-burst external-vote",
            cleanup_supervisor,
        )
        self.assertIn(
            '[[ "$recovery_profile" == "external-vote" ]] && (( profile_count == 1 ))',
            cleanup_supervisor,
        )
        self.assertIn(
            'if (( profile_count == 0 )) && [[ ! -e "$run_root/control.json"',
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

    @staticmethod
    def _workflow_step_run(workflow: str, step_name: str) -> str:
        """Extract a workflow step's shell body for ordering contracts."""

        step_start = workflow.index(f"      - name: {step_name}\n")
        next_step = workflow.find("\n      - name:", step_start + 1)
        step = workflow[step_start:] if next_step == -1 else workflow[step_start:next_step]
        run_marker = "        run: |\n"
        if run_marker not in step:
            raise AssertionError(f"workflow step has no literal run block: {step_name}")
        body = step.split(run_marker, 1)[1]
        return "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in body.splitlines()
        )

    def test_production_deploy_rechecks_current_dev_head_before_origin_write(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text()

        upload = self._workflow_step_run(workflow, "Upload verified CI artifact")
        activation = self._workflow_step_run(
            workflow,
            "Run production preflight or deployment",
        )
        # A separate YAML step is not a sufficient boundary: the first host
        # write must stay in the same shell body as the authoritative recheck.
        self.assertNotIn(
            "      - name: Require current dev head before production side effects",
            workflow,
        )
        for body in (upload, activation):
            self.assertIn(
                '"${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/branches/dev"',
                body,
            )
            self.assertIn("curl --fail-with-body --silent --show-error", body)
            self.assertIn('Authorization: Bearer $GH_TOKEN', body)
            self.assertIn("current dev head SHA is malformed", body)
            self.assertIn('payload["commit"]["sha"]', body)
            self.assertIn('test "$dev_sha" = "$TARGET_SHA"', body)

        first_api_read = upload.index("branch_json=")
        first_remote_dispatch = upload.index(
            'production-prepare-artifact < "$input_path"'
        )
        guard_call = upload.index("\nrequire_current_dev_head\n")
        first_ssh = upload.index("ssh -")
        self.assertLess(first_api_read, guard_call)
        self.assertLess(guard_call, first_ssh)
        self.assertLess(first_ssh, first_remote_dispatch)
        self.assertNotIn("bash -s --", upload)
        self.assertNotIn("$DEPLOY_MODE'", upload)
        self.assertNotIn("$RUNTIME_PROFILE'", upload)

        activation_api_read = activation.index("branch_json=")
        activation_ssh = activation.index("ssh -")
        self.assertLess(activation_api_read, activation_ssh)
        self.assertIn("Refusing activation", activation)
        self.assertIn('production-deploy < "$input_path"', activation)
        self.assertNotIn("bash -s --", activation)

        validation_step = self._workflow_step_run(
            workflow, "Validate and create closed deployment handoff"
        )
        self.assertIn("platform_workflow_input_guard.py deployment", validation_step)
        self.assertLess(
            workflow.index("Validate and create closed deployment handoff"),
            workflow.index("curl --fail-with-body --silent --show-error"),
        )

        upload_start = workflow.index("      - name: Upload verified CI artifact")
        upload_next_step = workflow.find("\n      - name:", upload_start + 1)
        upload_step = workflow[upload_start:upload_next_step]
        self.assertIn("inputs.mode == 'deploy'", workflow_job(workflow, "production"))
        self.assertIn("GH_TOKEN: ${{ github.token }}", upload_step)
        self.assertIn("PROD_SSH_HOST: ${{ secrets.PROD_SSH_HOST }}", upload_step)

        permissions = workflow[
            workflow.index("permissions:") : workflow.index("concurrency:")
        ]
        self.assertIn("contents: read", permissions)
        self.assertNotIn("actions: read", permissions)
        self.assertNotIn("statuses: write", permissions)
        self.assertNotIn("id-token: write", permissions)
        self.assertNotIn("attestations: write", permissions)
        self.assertNotIn("contents: write", permissions)

        production_permissions = workflow[
            workflow.index("  production:") : workflow.index(
                "    steps:", workflow.index("  production:")
            )
        ]
        self.assertIn("actions: read", production_permissions)
        self.assertIn("contents: read", production_permissions)
        self.assertIn("statuses: write", production_permissions)
        self.assertNotIn("id-token: write", production_permissions)
        self.assertNotIn("attestations: write", production_permissions)
        build_permissions = workflow[
            workflow.index("  build-release:") : workflow.index(
                "    steps:", workflow.index("  build-release:")
            )
        ]
        self.assertIn("id-token: write", build_permissions)
        self.assertIn("attestations: write", build_permissions)

        # Execute the actual upload shell body against a fake authoritative
        # API that returns B while the pinned target remains A.  The first
        # remote command must not be reached.
        target_sha = "a" * 40
        current_dev_sha = "b" * 40
        self.assertNotEqual(target_sha, current_dev_sha)
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            runner_temp = Path(directory) / "runner-temp"
            runner_temp.mkdir()
            (runner_temp / "platform-production-deploy-input.json").write_text(
                '{"schema":"1"}\n', encoding="utf-8"
            )
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                "#!/bin/sh\nprintf '%s\\n' "
                + json.dumps(json.dumps({"commit": {"sha": current_dev_sha}}))
                + "\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            ssh_called = Path(directory) / "ssh-called"
            fake_ssh = fake_bin / "ssh"
            fake_ssh.write_text(
                "#!/bin/sh\ntouch -- \"$SSH_CALLED\"\n",
                encoding="utf-8",
            )
            fake_ssh.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "GITHUB_API_URL": "https://api.github.invalid",
                    "GITHUB_REPOSITORY": "StrayForest/old_sparky",
                    "GH_TOKEN": "contract-test-token",
                    "TARGET_SHA": target_sha,
                    "RUNNER_TEMP": str(runner_temp),
                    "SSH_CALLED": str(ssh_called),
                }
            )
            result = subprocess.run(
                ["bash", "-c", upload],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(ssh_called.exists())

    def test_external_load_checked_out_client_has_no_ssh_material_or_persisted_creds(
        self,
    ) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-external-load.yml"
        ).read_text()

        # Candidate checkout/evaluation jobs are fresh and secret-free. SSH is
        # deliberately owned only by the setup/finalize environment jobs.
        for job_name in (
            "validate-external-inputs",
            "load-client",
            "evaluate-load",
        ):
            job = workflow_job(workflow, job_name)
            self.assertNotIn("secrets.", job, job_name)
            self.assertNotRegex(
                job,
                r"(?:PROD_SSH_(?:HOST|USER|KEY):|SSH_DIR=|id_ed25519)",
                job_name,
            )
        for job_name in ("fixture-setup", "fixture-finalize"):
            job = workflow_job(workflow, job_name)
            self.assertIn("environment: production", job, job_name)
            self.assertIn("secrets.PROD_SSH_KEY", job, job_name)

        checkout_start = workflow.index("      - name: Checkout reviewed load client")
        checkout_end = workflow.index("      - name:", checkout_start + 1)
        checkout = workflow[checkout_start:checkout_end]
        self.assertIn("persist-credentials: false", checkout)
        self.assertNotIn("persist-credentials: true", checkout)

        client_step_names = (
            "Validate explicit external production load",
            "Run checked-out external HTTP load client",
            "Evaluate checked-out load report",
        )
        for name in client_step_names:
            step_start = workflow.index(f"      - name: {name}")
            next_step = workflow.find("\n      - name:", step_start + 1)
            step = workflow[step_start:] if next_step == -1 else workflow[step_start:next_step]
            self.assertNotIn("PROD_SSH_HOST:", step, name)
            self.assertNotIn("PROD_SSH_USER:", step, name)
            self.assertNotIn("PROD_SSH_KEY:", step, name)
            self.assertNotIn("SSH_DIR=", step, name)
            self.assertNotIn("SSH_CONTROL_PATH=", step, name)
            if name != "Evaluate checked-out load report":
                self.assertIn("run_checked_out_client", step, name)
                self.assertIn("env -i", step, name)
                self.assertIn("SOURCE_GIT_SHA=", step, name)
                self.assertIn('GITHUB_RUN_ID="$GITHUB_RUN_ID"', step, name)
            self.assertNotIn("SSH_AUTH_SOCK", step, name)
            self.assertNotIn("id_ed25519", step, name)
            self.assertNotIn("ssh_dir", step, name)
            self.assertNotIn("control_path", step, name)

        for name in (
            "Prepare external fixture with ephemeral SSH",
            "Signal fixture completion and collect origin evidence",
            "Exact cleanup of external fixture",
        ):
            step_start = workflow.index(f"      - name: {name}")
            next_step = workflow.find("\n      - name:", step_start + 1)
            step = workflow[step_start:] if next_step == -1 else workflow[step_start:next_step]
            self.assertIn("id_ed25519", step, name)

        self.assertIn("id: fixture-setup", workflow)
        self.assertIn("id: external-finalize", workflow)
        self.assertIn("- name: Remove fixture-setup SSH material", workflow)
        self.assertIn("- name: Remove finalizer SSH material", workflow)
        self.assertIn("if: ${{ always() }}", workflow)
        self.assertIn(
            "needs:\n      - validate-external-inputs\n      - fixture-setup\n      - load-client",
            workflow,
        )
        self.assertIn("steps.fixture-setup.outputs.setup_status", workflow)
        self.assertIn("steps.external-finalize.outputs.remote_status", workflow)
        self.assertIn("steps.external-finalize.outputs.observer_ready", workflow)
        self.assertIn("steps.external-finalize.outputs.finalize_status", workflow)
        self.assertIn("steps.cleanup.outputs.cleanup_status", workflow)

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
        self.assertIn("wait_state_counts", qa_source)
        self.assertNotIn("active_query_samples", qa_source)
        self.assertNotIn("activity.query", qa_source)

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
        self.assertIn('"tools/platform_nginx_error_summary.py"', script)
        self.assertIn(
            '"tools/platform_web_runtime_diagnostics_summary.py"', script
        )
        self.assertIn('"tools/platform_storage_evidence_summary.py"', script)
        self.assertIn(
            '"tools/platform_media_migration_diagnostics_summary.py"', script
        )
        self.assertIn("tracked runtime diagnostic helper is missing", script)
        archive_start = script.index(
            'git -C "$REPO_ROOT" archive --format=tar "$SOURCE_GIT_COMMIT" platform'
        )
        helper_check = script.index('"tools/platform_nginx_error_summary.py"')
        prune_start = script.index('rm -rf \\\n  "$STAGING_DIR/.github"')
        self.assertLess(archive_start, helper_check)
        self.assertLess(helper_check, prune_start)
        self.assertIn("/usr/bin/tar --no-same-permissions -xf -", script)
        self.assertIn("status --porcelain=v1 --untracked-files=all -- platform", script)
        self.assertIn('"$PLATFORM_NODE_BIN" "$NPM_CLI" ci', script)
        self.assertIn("Tracked package.json must pin an exact npm version", script)
        self.assertNotIn("rsync", script)
        self.assertNotIn('node_modules/" "$STAGING_DIR', script)
        self.assertIn("rm -rf node_modules .next/cache", script)

    def test_clean_git_archive_preserves_runtime_helper_paths_and_bytes(self) -> None:
        helper_names = (
            "platform_nginx_error_summary.py",
            "platform_web_runtime_diagnostics_summary.py",
            "platform_storage_evidence_summary.py",
            "platform_media_migration_diagnostics_summary.py",
        )
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "source"
            for name in helper_names:
                destination = fixture / "platform/tools" / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((TOOLS_DIR / name).read_bytes())
            (fixture / "platform/README.md").write_text("tracked\n", encoding="utf-8")

            subprocess.run(
                ["git", "init", "--quiet", str(fixture)], check=True
            )
            subprocess.run(
                ["git", "-C", str(fixture), "add", "--", "platform"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(fixture),
                    "-c",
                    "user.name=Platform contract",
                    "-c",
                    "user.email=platform-contract@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "tracked helpers",
                ],
                check=True,
            )
            archive = subprocess.run(
                ["git", "-C", str(fixture), "archive", "--format=tar", "HEAD", "platform"],
                check=True,
                stdout=subprocess.PIPE,
            )

            with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
                for name in helper_names:
                    member = tar.getmember(f"platform/tools/{name}")
                    extracted = tar.extractfile(member)
                    self.assertIsNotNone(extracted)
                    assert extracted is not None
                    self.assertEqual(extracted.read(), (TOOLS_DIR / name).read_bytes())

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
