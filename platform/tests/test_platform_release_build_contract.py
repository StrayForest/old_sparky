from __future__ import annotations

import base64
from contextlib import redirect_stdout
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile
import stat
import textwrap
import time
import unittest
from unittest.mock import patch
import zipfile

from tests import platform_chromium_sandbox_fixture as chromium_sandbox_fixture
from tests.test_platform_validate_release_artifact import (
    ArchiveBuilder as ReleaseArtifactFixtureBuilder,
    RELEASE_SLUG,
    VALIDATOR_SCRIPT,
)
from tools import platform_workflow_remote_dispatch
from tools import platform_live_qa_runtime_install
from tools.platform_ci_classifier import (
    CANDIDATE_PACKAGING_FILES,
    CANDIDATE_PACKAGING_REASON,
    RECOVERY_BOOTSTRAP_FILES,
    RECOVERY_BOOTSTRAP_REASON,
    classify,
    manifest_digest,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "platform/tools/platform_build_release.sh"
TOOLS_DIR = REPO_ROOT / "platform/tools"
DEPLOY_SCRIPT = TOOLS_DIR / "platform_release_deploy.sh"
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
    def _candidate_diagnostic_runner_source(supervisor: str) -> str:
        """Return the production runner heredoc for isolated subprocess tests."""

        invocation = supervisor.index('candidate_result="$(')
        heredoc_start = supervisor.index("<<'PY'\n", invocation) + len("<<'PY'\n")
        heredoc_end = supervisor.index("\nPY\n  2>/dev/null\n)", heredoc_start)
        runner = supervisor[heredoc_start:heredoc_end]
        if runner.count('"/var/tmp"') != 1:
            raise AssertionError("runner must have one diagnostic-root test seam")
        return runner

    @staticmethod
    def _deploy_shell_function_source(source: str, name: str) -> str:
        match = re.search(
            rf"^{re.escape(name)}\(\) \{{\n(?P<body>.*?)^\}}",
            source,
            re.MULTILINE | re.DOTALL,
        )
        if match is None:
            raise AssertionError(f"deploy helper is missing: {name}")
        return f"{name}() {{\n{match.group('body')}\n}}"

    @staticmethod
    def _run_candidate_diagnostic_runner(
        runner: str,
        *,
        candidate: Path,
        diagnostic_parent: Path,
        release_slug: str,
        source_sha: str,
        run_id: str,
        attempt: str,
        host_tools_sha: str,
    ) -> subprocess.CompletedProcess[str]:
        """Execute the exact embedded runner with only its root redirected."""

        diagnostic_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(diagnostic_parent, 0, 0)
        os.chmod(diagnostic_parent, 0o1777)
        isolated = runner.replace(
            '"/var/tmp"', json.dumps(str(diagnostic_parent)), 1
        )
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-",
                str(candidate),
                "/fixture/release.tar.gz",
                "/fixture/runtime",
                release_slug,
                source_sha,
                run_id,
                attempt,
                host_tools_sha,
            ],
            input=isolated,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
            timeout=15,
            check=False,
        )

    @staticmethod
    def _candidate_diagnostic_marker_harness(
        supervisor: str, *, result: str, release_slug: str, source_sha: str
    ) -> str:
        """Compose the production result parser with the production marker code."""

        functions_start = supervisor.index("set_failure_context() {")
        functions_end = supervisor.index("\nvalidate_systemctl_binary() {", functions_start)
        functions = supervisor[functions_start:functions_end]
        parser_start = supervisor.index(
            'if [[ "$candidate_result" =~ ^candidate_status=',
            supervisor.index('candidate_result="$('),
        )
        outer_else = supervisor.index(
            '\nelse\n  set_failure_context preflight preflight internal\n'
            '  fail "candidate diagnostic capture could not be started"\nfi',
            parser_start,
        )
        parser_end = outer_else + len(
            '\nelse\n  set_failure_context preflight preflight internal\n'
            '  fail "candidate diagnostic capture could not be started"\nfi'
        )
        # Keep the inner result parser and the state/exit handling below the
        # command-substitution shell branch. The real runner always returns
        # one line here, so its command-substitution outer failure arm cannot
        # be reached by this fixture.
        parser = supervisor[parser_start:outer_else]
        suffix_start = parser_end
        parser += supervisor[suffix_start : supervisor.index(
            "\n# The candidate deploy process has returned", suffix_start
        )]
        return "\n".join(
            (
                "set -Eeuo pipefail",
                f"release_slug={shlex.quote(release_slug)}",
                f"target_sha={shlex.quote(source_sha)}",
                f"candidate_result={shlex.quote(result)}",
                functions,
                parser,
            )
        )

    @staticmethod
    def _install_supervisor_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
        """Install an exact supervisor copy under the production path shape.

        The real supervisor derives its trusted generation from ``BASH_SOURCE``
        and uses absolute production lock paths.  Keep this subprocess fixture
        isolated to one unique host-tools generation and two unique lock files;
        no production helper or shared lock is replaced.
        """

        if os.geteuid() != 0:
            raise AssertionError("supervisor ownership fixture requires root")
        host_tools_root = Path("/opt/oldsparky/platform/shared/host-tools")
        host_tools_root.mkdir(parents=True, exist_ok=True)
        generation = hashlib.sha1(str(root).encode("utf-8")).hexdigest()
        generation_dir = host_tools_root / generation
        if generation_dir.exists() or generation_dir.is_symlink():
            raise AssertionError("supervisor fixture generation unexpectedly exists")
        generation_dir.mkdir(mode=0o555)
        os.chown(generation_dir, 0, 0)
        os.chmod(generation_dir, 0o555)

        release_lock = Path(f"/run/lock/oldsparky-pr115-release-{generation[:16]}.lock")
        retained_lock = Path(f"/run/lock/oldsparky-pr115-retained-{generation[:16]}.lock")
        if (
            release_lock.exists()
            or release_lock.is_symlink()
            or retained_lock.exists()
            or retained_lock.is_symlink()
        ):
            raise AssertionError("supervisor fixture lock unexpectedly exists")

        lock_source = (TOOLS_DIR / "platform_release_lock.sh").read_text(encoding="utf-8")
        lock_source = lock_source.replace(
            'PLATFORM_RELEASE_LOCK_CANONICAL_PATH="/run/lock/oldsparky-platform-release.lock"',
            f'PLATFORM_RELEASE_LOCK_CANONICAL_PATH="{release_lock}"',
            1,
        )
        lock_source = lock_source.replace(
            (
                'PLATFORM_RETAINED_LOAD_LOCK_CANONICAL_PATH='
                '"/run/lock/oldsparky-retained-load-matrix.lock"'
            ),
            f'PLATFORM_RETAINED_LOAD_LOCK_CANONICAL_PATH="{retained_lock}"',
            1,
        )
        lock_source = lock_source.replace(
            "platform_release_lock_open() {",
            "platform_release_lock_open_original() {",
            1,
        )
        lock_source = lock_source.replace(
            "platform_retained_load_lock_open() {",
            "platform_retained_load_lock_open_original() {",
            1,
        )
        lock_source += textwrap.dedent(
            """

            platform_release_lock_open() {
              runtime="${PLATFORM_TEST_RUNTIME:-$runtime}"
              platform_release_lock_open_original
            }

            platform_retained_load_lock_open() {
              runtime="${PLATFORM_TEST_RUNTIME:-$runtime}"
              platform_retained_load_lock_open_original
            }
            """
        )

        host_tool_files = (
            "platform_workflow_remote_dispatch.py",
            "platform_workflow_input_guard.py",
            "platform_prepare_artifact_dir.py",
            "platform_retained_load_export_executor.py",
            "platform_production_deploy_supervisor.sh",
            "platform_release_lock.sh",
            "platform_release_preflight.sh",
            "platform_validate_release_artifact.py",
            "platform_safe_env_exec.py",
            "platform_render_service_envs.py",
            "platform_validate_edge_policy.py",
            "platform_configure_shared_env.py",
            "platform_update_cloudflare_ips.py",
            "platform_storage_evidence_summary.py",
        )
        for name in host_tool_files:
            destination = generation_dir / name
            if name == "platform_release_lock.sh":
                destination.write_text(lock_source, encoding="utf-8")
            else:
                shutil.copyfile(TOOLS_DIR / name, destination)
            os.chown(destination, 0, 0)
            os.chmod(destination, 0o555)

        supervisor = generation_dir / "platform_production_deploy_supervisor.sh"
        return supervisor, release_lock, retained_lock, generation_dir

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
        self.assertIn("--source-only", build_script)
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
        sandbox: bytes | None = None,
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
            "tests/smoke/live-launch.spec.ts",
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

        sandbox = sandbox if sandbox is not None else b"small pinned sandbox fixture\n"
        archive_entries = {
            "chromium-1228": [
                ("chrome-linux64/chrome_sandbox", sandbox, "file"),
                ("chrome-linux64/chrome", b"chromium\n", "file"),
                ("chrome-linux64/resources", b"", "directory"),
                ("chrome-linux64/resources/accessibility", b"", "directory"),
                ("chrome-linux64/resources.pak", b"pak\n", "file"),
                (
                    "chrome-linux64/resources/accessibility/ax",
                    b"ax\n",
                    "file",
                ),
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
        *,
        source_only: bool = False,
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
        command = [
                "/usr/bin/python3",
                "-I",
                str(builder),
                "--platform-root",
                str(platform_root),
                "--node-home",
                str(node_home),
                "--output",
                str(output),
            ]
        if source_only:
            command.append("--source-only")
        return subprocess.run(
            command,
            cwd=decoy,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_staged_live_qa_source_only_keeps_suite_and_references_all_engines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder, platform_root, node_home, output, _sandbox = (
                self._prepare_local_runtime_fixture(
                    root,
                    sandbox=chromium_sandbox_fixture.read_bytes(),
                )
            )
            completed = self._run_staged_live_qa_build(
                builder, platform_root, node_home, output, root, source_only=True
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest = json.loads((output / "runtime-manifest.json").read_text())
            self.assertEqual(manifest["version"], 2)
            self.assertFalse((output / "node").exists())
            self.assertFalse((output / "browsers").exists())
            self.assertFalse((output / "web/node_modules").exists())
            self.assertIn("web/tests/smoke/live-launch.spec.ts", manifest["suite_files"])
            self.assertEqual(
                platform_live_qa_runtime_install._validate_runtime_source(output)["version"],
                2,
            )
            self.assertIn("node/bin/node", manifest["engine_files"])
            self.assertIn(
                "web/node_modules/playwright-core/package.json", manifest["engine_files"]
            )
            for browser in ("chromium-1228", "chromium_headless_shell-1228", "webkit-2311", "ffmpeg-1011"):
                self.assertTrue(
                    any(path.startswith(f"browsers/{browser}/") for path in manifest["engine_files"])
                )

    @staticmethod
    def _manifest_padding_entries(count: int) -> list[tuple[str, bytes, str]]:
        return [
            (
                f"lib/manifest-padding-{index:04d}-{'x' * 210}",
                b"x",
                "file",
            )
            for index in range(count)
        ]

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

    def test_staged_live_qa_builder_output_passes_standalone_artifact_validator(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder, platform_root, node_home, output, _sandbox = (
                self._prepare_local_runtime_fixture(
                    root,
                    sandbox=chromium_sandbox_fixture.read_bytes(),
                )
            )
            completed = self._run_staged_live_qa_build(
                builder, platform_root, node_home, output, root
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest = json.loads((output / "runtime-manifest.json").read_text())
            self.assertIn(
                "browsers/chromium-1228/chrome-linux64/resources.pak",
                manifest["files"],
            )
            self.assertIn(
                "browsers/chromium-1228/chrome-linux64/resources/accessibility/ax",
                manifest["files"],
            )

            artifact = root / f"{RELEASE_SLUG}.tar.gz"
            artifact_builder = ReleaseArtifactFixtureBuilder(artifact)
            artifact_builder.replace_liveqa_runtime(output)
            artifact_builder.write(regenerate_runtime_manifest=False)
            checksum = Path(f"{artifact}.sha256")
            checksum.write_text(
                f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}\n",
                encoding="ascii",
            )
            validated = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-I",
                    str(VALIDATOR_SCRIPT),
                    "--artifact",
                    str(artifact),
                    "--checksum",
                    str(checksum),
                    "--release-slug",
                    RELEASE_SLUG,
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_staged_live_qa_manifest_between_legacy_and_runtime_bounds_is_valid(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder, platform_root, node_home, output, _sandbox = (
                self._prepare_local_runtime_fixture(
                    root,
                    webkit_entries=self._manifest_padding_entries(500),
                )
            )
            completed = self._run_staged_live_qa_build(
                builder, platform_root, node_home, output, root
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest_size = (output / "runtime-manifest.json").stat().st_size
            self.assertGreater(manifest_size, 64 * 1024)
            self.assertLessEqual(manifest_size, 256 * 1024)

    def test_staged_live_qa_manifest_over_bound_reports_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder, platform_root, node_home, output, _sandbox = (
                self._prepare_local_runtime_fixture(
                    root,
                    webkit_entries=self._manifest_padding_entries(900),
                )
            )
            completed = self._run_staged_live_qa_build(
                builder, platform_root, node_home, output, root
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            diagnostic = json.loads(completed.stderr)
            self.assertEqual(diagnostic["phase"], "manifest")
            self.assertEqual(diagnostic["reason"], "size-limit")
            self.assertEqual(diagnostic["cleanup"], "passed")
            self.assertNotIn(str(root), completed.stderr)
            self.assertNotIn("file://", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertFalse(output.exists())

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
        prepare_index = systemd_installer.index(prepare)
        daemon_reload_index = systemd_installer.index(
            "\nrun_systemctl daemon-reload\n", prepare_index
        )
        self.assertLess(
            prepare_index,
            daemon_reload_index,
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

        security_provenance = workflow_job(workflow, "validate-security-provenance")
        production = workflow_job(workflow, "production")

        # The ordinary deploy path deliberately skips baseline-reconcile jobs.
        # Keep those skipped ancestors from triggering GitHub's implicit
        # success() guard here, while explicitly requiring the two jobs that
        # authorize this provenance check to have succeeded.
        self.assertIn("!cancelled()", security_provenance)
        self.assertIn("needs.validate-dispatch.result == 'success'", security_provenance)
        self.assertIn("needs.build-release.result == 'success'", security_provenance)
        self.assertIn("inputs.mode == 'deploy'", security_provenance)
        self.assertIn("!cancelled()", production)
        self.assertIn("needs.validate-security-provenance.result == 'success'", production)
        self.assertIn("needs.build-release.result == 'success'", production)

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

    def test_production_classifier_artifact_reader_is_data_only_and_bounded(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text()
        tool = TOOLS_DIR / "platform_production_classifier_artifact.py"
        self.assertTrue(tool.is_file())
        self.assertIn("platform_production_classifier_artifact.py", workflow)
        self.assertIn("trusted_classifier_default.outputs.sha", workflow)
        self.assertIn("validate-classifier", workflow)
        prerequisite = workflow_job(workflow, "validate-classifier")
        build = workflow_job(workflow, "build-release")
        production = workflow_job(workflow, "production")
        self.assertIn("platform_production_classifier_artifact.py", prerequisite)
        self.assertIn("platform_production_classifier_artifact.py", production)
        self.assertIn("- validate-classifier", build)
        self.assertIn("- validate-classifier", production)
        self.assertIn("needs.validate-classifier.result == 'success'", build)
        self.assertIn("needs.validate-classifier.result == 'success'", production)
        self.assertNotIn("env.TARGET_SHA", prerequisite)
        self.assertNotIn("env.TARGET_SHA", production)
        self.assertNotIn("self-contained bounded extractor", production)
        self.assertNotIn('/usr/bin/python3 "$artifacts_metadata"', workflow)
        self.assertIn("/usr/bin/python3 \"$trusted_tool\" metadata", workflow)
        self.assertIn("/usr/bin/python3 \"$trusted_tool\" manifest", workflow)
        host_build = self._workflow_step_run(
            workflow, "Verify exact host-tools artifact metadata"
        )
        host_preflight = self._workflow_step_run(
            workflow, "Validate host-tools artifact envelope and bundle"
        )
        self.assertIn(
            "actions/runs/${GITHUB_RUN_ID}/attempts/${GITHUB_RUN_ATTEMPT}",
            host_build,
        )
        self.assertIn("platform/tools/platform_host_tools_bundle.py", host_build)
        self.assertIn("verify-artifact-metadata", host_build)
        self.assertIn("--max-filesize 524288", host_build)
        self.assertIn('--archive "$api_zip"', host_build)
        self.assertNotIn("--jq", host_build)
        self.assertNotIn("@tsv", host_build)
        self.assertIn("host-tools workflow attempt metadata request failed", host_build)
        self.assertIn("verify-workflow-attempt", host_build)
        self.assertNotIn(".workflow_run.run_attempt", host_build)
        self.assertIn('"sha256:${HOST_TOOLS_ARTIFACT_DIGEST}"', host_build)
        self.assertIn("sha256sum -c", host_build)
        self.assertNotIn("actions/checkout@", host_preflight)
        self.assertNotIn("platform_host_tools_bundle.py", host_preflight)
        host_tool_source = (TOOLS_DIR / "platform_host_tools_bundle.py").read_text()
        self.assertIn("size_in_bytes", host_tool_source)
        self.assertIn("workflow_run.get(\"head_branch\")", host_tool_source)
        self.assertIn('payload.get("head_branch") != expected_branch', host_tool_source)
        self.assertIn("payload.get(\"digest\")", host_tool_source)
        self.assertIn("object_pairs_hook=_strict_object", host_tool_source)
        self.assertIn("host-tools workflow attempt provenance is invalid", host_tool_source)
        self.assertNotIn('payload.get("ref") != "refs/heads/dev"', host_tool_source)

        target_sha = "a" * 40
        expected_name = "platform-ci-route-123-1"
        selected = {
            "id": 42,
            "name": expected_name,
            "expired": False,
            "workflow_run": {
                "id": 123,
                "head_branch": "dev",
                "head_sha": target_sha,
            },
        }
        expired = {
            "id": 43,
            "name": "platform-backend-aggregate-123-1",
            "expired": True,
            "workflow_run": {
                "id": 123,
                "head_branch": "dev",
                "head_sha": target_sha,
            },
        }

        def run_tool(
            *arguments: str,
            environment: dict[str, str] | None = None,
        ) -> subprocess.CompletedProcess[str]:
            child_environment = os.environ.copy()
            if environment is not None:
                child_environment.update(environment)
            return subprocess.run(
                ["/usr/bin/python3", str(tool), *arguments],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                env=child_environment,
            )

        def write_page(directory: Path, payload: object, *, raw: bytes | None = None) -> None:
            directory.mkdir(mode=0o700, exist_ok=True)
            path = directory / "page-1.json"
            path.write_bytes(
                json.dumps(payload, separators=(",", ":")).encode("utf-8")
                if raw is None
                else raw
            )
            os.chmod(path, 0o600)

        def metadata_tree(
            *,
            first_payload: object,
            second_payload: object | None = None,
            first_raw: bytes | None = None,
            second_raw: bytes | None = None,
        ) -> tuple[Path, Path, tempfile.TemporaryDirectory[str]]:
            temporary = tempfile.TemporaryDirectory()
            root = Path(temporary.name)
            write_page(root / "first", first_payload, raw=first_raw)
            write_page(
                root / "second",
                first_payload if second_payload is None else second_payload,
                raw=second_raw,
            )
            return root / "first", root / "second", temporary

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid_payload = {"total_count": 2, "artifacts": [selected, expired]}
            first_pages, second_pages, owned = metadata_tree(
                first_payload=valid_payload
            )
            try:
                valid = run_tool(
                    "metadata",
                    str(first_pages),
                    str(second_pages),
                    "--expected-name",
                    expected_name,
                    "--run-id",
                    "123",
                    "--run-attempt",
                    "1",
                    "--target-sha",
                    target_sha,
                )
                self.assertEqual(valid.returncode, 0, valid.stderr)
                self.assertEqual(valid.stdout.strip(), "42")
            finally:
                owned.cleanup()

            for invalid_attempt in (False, "1", 1.0, 2):
                invalid_selected = json.loads(json.dumps(selected))
                invalid_selected["workflow_run"]["run_attempt"] = invalid_attempt
                invalid_payload = {"total_count": 2, "artifacts": [invalid_selected, expired]}
                invalid_first, invalid_second, owned = metadata_tree(
                    first_payload=invalid_payload
                )
                try:
                    invalid = run_tool(
                        "metadata",
                        str(invalid_first),
                        str(invalid_second),
                        "--expected-name",
                        expected_name,
                        "--run-id",
                        "123",
                        "--run-attempt",
                        "1",
                        "--target-sha",
                        target_sha,
                    )
                    with self.subTest(invalid_attempt=invalid_attempt):
                        self.assertNotEqual(invalid.returncode, 0)
                        self.assertIn("provenance", invalid.stderr)
                finally:
                    owned.cleanup()

            malformed_first, malformed_second, owned = metadata_tree(
                first_payload=valid_payload,
                first_raw=b"not-json",
            )
            try:
                malformed = run_tool(
                    "metadata",
                    str(malformed_first),
                    str(malformed_second),
                    "--expected-name",
                    expected_name,
                    "--run-id",
                    "123",
                    "--run-attempt",
                    "1",
                    "--target-sha",
                    target_sha,
                )
                self.assertNotEqual(malformed.returncode, 0)
                self.assertIn("json", malformed.stderr)
                self.assertNotIn("Traceback", malformed.stderr)
            finally:
                owned.cleanup()

            duplicate_first, duplicate_second, owned = metadata_tree(
                first_payload=valid_payload,
                first_raw=b'{"total_count":2,"total_count":2,"artifacts":[]}',
            )
            try:
                duplicate = run_tool(
                    "metadata",
                    str(duplicate_first),
                    str(duplicate_second),
                    "--expected-name",
                    expected_name,
                    "--run-id",
                    "123",
                    "--run-attempt",
                    "1",
                    "--target-sha",
                    target_sha,
                )
                self.assertNotEqual(duplicate.returncode, 0)
                self.assertIn("duplicate_keys", duplicate.stderr)
            finally:
                owned.cleanup()

            oversized_first, oversized_second, owned = metadata_tree(
                first_payload=valid_payload,
                first_raw=b"{" + b"x" * (4 * 1024 * 1024),
            )
            try:
                oversized = run_tool(
                    "metadata",
                    str(oversized_first),
                    str(oversized_second),
                    "--expected-name",
                    expected_name,
                    "--run-id",
                    "123",
                    "--run-attempt",
                    "1",
                    "--target-sha",
                    target_sha,
                )
                self.assertNotEqual(oversized.returncode, 0)
                self.assertIn("oversized", oversized.stderr)
            finally:
                owned.cleanup()

            sentinel = root / "metadata-executed"
            malicious = (
                f"__import__('pathlib').Path({str(sentinel)!r})"
                ".write_text('executed')\n"
            ).encode("utf-8")
            malicious_first, malicious_second, owned = metadata_tree(
                first_payload=valid_payload,
                first_raw=malicious,
            )
            try:
                malicious_result = run_tool(
                    "metadata",
                    str(malicious_first),
                    str(malicious_second),
                    "--expected-name",
                    expected_name,
                    "--run-id",
                    "123",
                    "--run-attempt",
                    "1",
                    "--target-sha",
                    target_sha,
                )
                self.assertNotEqual(malicious_result.returncode, 0)
                self.assertFalse(sentinel.exists())
                self.assertNotIn("Traceback", malicious_result.stderr)
            finally:
                owned.cleanup()

            wrong_type_first, wrong_type_second, owned = metadata_tree(
                first_payload={"total_count": True, "artifacts": []}
            )
            try:
                wrong_type = run_tool(
                    "metadata",
                    str(wrong_type_first),
                    str(wrong_type_second),
                    "--expected-name",
                    expected_name,
                    "--run-id",
                    "123",
                    "--run-attempt",
                    "1",
                    "--target-sha",
                    target_sha,
                )
                self.assertNotEqual(wrong_type.returncode, 0)
                self.assertIn("schema", wrong_type.stderr)
            finally:
                owned.cleanup()

            expected_gates = [
                "backend",
                "python-quality",
                "security",
                "migration",
                "docs",
                "web-quality",
                "web-hermetic",
                "verification-contract",
            ]

            def manifest_payload(runtime_sensitive: bool) -> dict[str, object]:
                payload: dict[str, object] = {
                    "schema": 1,
                    "version": 1,
                    "target_sha": target_sha,
                    "event": "push",
                    "class": "full",
                    "expected_gates": expected_gates,
                    "runtime_sensitive": runtime_sensitive,
                    "deployable": True,
                    "fallback": False,
                    "reason": "platform change requires full verification",
                    "files": ["platform/tools/example.py"],
                }
                payload["digest"] = hashlib.sha256(
                    json.dumps(
                        {field: payload[field] for field in (
                            "schema",
                            "version",
                            "target_sha",
                            "event",
                            "class",
                            "expected_gates",
                            "runtime_sensitive",
                            "deployable",
                            "fallback",
                            "reason",
                            "files",
                        )},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                return payload

            def write_archive(raw: bytes) -> Path:
                archive = root / f"manifest-{len(list(root.glob('manifest-*.zip')))}.zip"
                with zipfile.ZipFile(archive, "w") as bundle:
                    bundle.writestr("classifier-manifest.json", raw)
                os.chmod(archive, 0o600)
                return archive

            def write_corrupted_deflate_archive() -> Path:
                archive = root / "corrupted-deflate.zip"
                payload = b"".join(
                    hashlib.sha256(f"corrupt-member-{index}".encode()).digest()
                    for index in range(512)
                )
                with zipfile.ZipFile(
                    archive, "w", compression=zipfile.ZIP_DEFLATED
                ) as bundle:
                    bundle.writestr("classifier-manifest.json", payload)
                    info = bundle.infolist()[0]
                encoded_name = info.filename.encode("utf-8")
                compressed_offset = (
                    info.header_offset + 30 + len(encoded_name) + len(info.extra)
                )
                archive_bytes = bytearray(archive.read_bytes())
                archive_bytes[compressed_offset] ^= 0xFF
                archive.write_bytes(archive_bytes)
                os.chmod(archive, 0o600)
                return archive

            for runtime_sensitive in (False, True):
                with self.subTest(runtime_sensitive=runtime_sensitive):
                    payload = manifest_payload(runtime_sensitive)
                    archive = write_archive(
                        json.dumps(payload, separators=(",", ":")).encode()
                    )
                    result = run_tool(
                        "manifest",
                        str(archive),
                        "--target-sha",
                        target_sha,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("classifier manifest accepted", result.stdout)
                    exported = run_tool(
                        "manifest",
                        str(archive),
                        "--target-sha",
                        target_sha,
                        "--emit-manifest-base64",
                    )
                    self.assertEqual(exported.returncode, 0, exported.stderr)
                    self.assertEqual(
                        json.loads(base64.b64decode(exported.stdout.strip())),
                        payload,
                    )

            classifier_tmp = root / "classifier-tmp"
            classifier_tmp.mkdir(mode=0o700)
            corrupted_result = run_tool(
                "manifest",
                str(write_corrupted_deflate_archive()),
                "--target-sha",
                target_sha,
                environment={"TMPDIR": str(classifier_tmp)},
            )
            self.assertEqual(corrupted_result.returncode, 1)
            self.assertEqual(corrupted_result.stdout, "")
            self.assertEqual(
                corrupted_result.stderr,
                "classifier validation rejected: archive\n",
            )
            self.assertNotIn("Traceback", corrupted_result.stderr)
            self.assertNotIn(str(root), corrupted_result.stderr)
            self.assertEqual(list(classifier_tmp.iterdir()), [])

            wrong_manifest = manifest_payload(False)
            wrong_manifest["target_sha"] = "b" * 40
            wrong_result = run_tool(
                "manifest",
                str(write_archive(json.dumps(wrong_manifest, separators=(",", ":")).encode())),
                "--target-sha",
                target_sha,
            )
            self.assertNotEqual(wrong_result.returncode, 0)
            self.assertIn("provenance", wrong_result.stderr)

            duplicate_manifest = write_archive(
                b'{"schema":1,"schema":1,"version":1}'
            )
            duplicate_manifest_result = run_tool(
                "manifest",
                str(duplicate_manifest),
                "--target-sha",
                target_sha,
            )
            self.assertNotEqual(duplicate_manifest_result.returncode, 0)
            self.assertIn("duplicate_keys", duplicate_manifest_result.stderr)

            manifest_sentinel = root / "manifest-executed"
            malicious_manifest = (
                f"__import__('pathlib').Path({str(manifest_sentinel)!r})"
                ".write_text('executed')\n"
            ).encode("utf-8")
            malicious_manifest_result = run_tool(
                "manifest",
                str(write_archive(malicious_manifest)),
                "--target-sha",
                target_sha,
            )
            self.assertNotEqual(malicious_manifest_result.returncode, 0)
            self.assertFalse(manifest_sentinel.exists())
            self.assertNotIn("Traceback", malicious_manifest_result.stderr)

    def test_reconcile_classifier_manifest_is_explicit_and_family_bound(self) -> None:
        target_sha = "a" * 40
        candidate_path = ".github/workflows/platform-host-tools-candidate.yml"
        shared_path = ".github/workflows/platform-production-autodeploy.yml"
        recovery_only_path = "platform/tools/platform_recovery_bootstrap.py"
        app_path = "platform/tools/platform_build_release.sh"
        self.assertIn(candidate_path, CANDIDATE_PACKAGING_FILES)
        self.assertIn(shared_path, CANDIDATE_PACKAGING_FILES)
        self.assertIn(shared_path, RECOVERY_BOOTSTRAP_FILES)
        self.assertIn(recovery_only_path, RECOVERY_BOOTSTRAP_FILES)

        def write_archive(root: Path, manifest: dict[str, object]) -> Path:
            archive = root / f"manifest-{len(list(root.glob('manifest-*.zip')))}.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(
                    "classifier-manifest.json",
                    json.dumps(manifest, separators=(",", ":")),
                )
            os.chmod(archive, 0o600)
            return archive

        def validate(archive: Path, *flags: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    "/usr/bin/python3",
                    str(TOOLS_DIR / "platform_production_classifier_artifact.py"),
                    "manifest",
                    str(archive),
                    "--target-sha",
                    target_sha,
                    *flags,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = classify(
                [candidate_path, shared_path, "platform/docs/candidate-route.md"],
                event="push",
                branch="dev",
                target_sha=target_sha,
            )
            self.assertEqual(candidate["reason"], CANDIDATE_PACKAGING_REASON)
            self.assertFalse(candidate["deployable"])
            self.assertFalse(candidate["runtime_sensitive"])
            candidate_archive = write_archive(root, candidate)
            self.assertNotEqual(validate(candidate_archive).returncode, 0)
            self.assertNotEqual(
                validate(candidate_archive, "--require-recovery-bootstrap").returncode,
                0,
            )
            accepted_candidate = validate(
                candidate_archive, "--require-reconcile-source"
            )
            self.assertEqual(accepted_candidate.returncode, 0, accepted_candidate.stderr)
            conflicting_flags = validate(
                candidate_archive,
                "--require-reconcile-source",
                "--require-recovery-bootstrap",
            )
            self.assertNotEqual(conflicting_flags.returncode, 0)

            recovery = classify(
                [shared_path, recovery_only_path],
                event="push",
                branch="dev",
                target_sha=target_sha,
            )
            self.assertEqual(recovery["reason"], RECOVERY_BOOTSTRAP_REASON)
            recovery_archive = write_archive(root, recovery)
            self.assertEqual(validate(recovery_archive).returncode, 0)
            self.assertEqual(
                validate(recovery_archive, "--require-recovery-bootstrap").returncode,
                0,
            )
            self.assertEqual(
                validate(recovery_archive, "--require-reconcile-source").returncode,
                0,
            )

            for label, field_value in (
                ("runtime-sensitive candidate", True),
                ("non-boolean candidate runtime flag", "false"),
            ):
                malformed = dict(candidate)
                malformed["runtime_sensitive"] = field_value
                malformed["digest"] = manifest_digest(malformed)
                rejected = validate(
                    write_archive(root, malformed), "--require-reconcile-source"
                )
                with self.subTest(label=label):
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn("manifest", rejected.stderr)

            wrong_reason = dict(candidate)
            wrong_reason["reason"] = RECOVERY_BOOTSTRAP_REASON
            wrong_reason["digest"] = manifest_digest(wrong_reason)
            self.assertNotEqual(
                validate(
                    write_archive(root, wrong_reason),
                    "--require-reconcile-source",
                ).returncode,
                0,
            )

            mixed = classify(
                [candidate_path, recovery_only_path],
                event="push",
                branch="dev",
                target_sha=target_sha,
            )
            self.assertTrue(mixed["deployable"])
            self.assertNotEqual(
                validate(
                    write_archive(root, mixed), "--require-reconcile-source"
                ).returncode,
                0,
            )
            application_range = classify(
                [candidate_path, app_path],
                event="push",
                branch="dev",
                target_sha=target_sha,
            )
            self.assertTrue(application_range["deployable"])
            self.assertTrue(application_range["runtime_sensitive"])
            self.assertNotEqual(
                validate(
                    write_archive(root, application_range),
                    "--require-reconcile-source",
                ).returncode,
                0,
            )

        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text(encoding="utf-8")
        reconcile_validation = workflow_job(workflow, "validate-classifier")
        production = workflow_job(workflow, "production")
        self.assertIn("DEPLOY_MODE: deploy", reconcile_validation)
        self.assertIn(
            "RECONCILE_MODE: ${{ inputs.mode == 'baseline-reconcile' && 'true' || 'false' }}",
            reconcile_validation,
        )
        self.assertIn('if [[ "$RECONCILE_MODE" == "true" ]]', reconcile_validation)
        self.assertNotIn('if [[ "$DEPLOY_MODE" == "baseline-reconcile" ]]', reconcile_validation)
        self.assertIn('if [[ "$RECONCILE_MODE" == "true" ]]', production)
        self.assertEqual(workflow.count("manifest_args+=(--require-reconcile-source)"), 2)

        auto = (
            REPO_ROOT / ".github/workflows/platform-production-autodeploy.yml"
        ).read_text(encoding="utf-8")
        dispatch = workflow_job(auto, "dispatch")
        self.assertIn("candidate_packaging_reason =", dispatch)
        self.assertIn("candidate_packaging_only = bool(", dispatch)
        self.assertIn('manifest["runtime_sensitive"] is not False', dispatch)
        self.assertIn('dispatch_mode=baseline-reconcile', dispatch)
        self.assertIn('ROUTE_CANDIDATE_PACKAGING_ONLY" == "true"', dispatch)
        self.assertIn('ROUTE_RECOVERY_BOOTSTRAP_ONLY" == "false"', dispatch)
        self.assertIn('ROUTE_CANDIDATE_PACKAGING_ONLY" == "false"', dispatch)

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
            'lock_helper="$host_tools_dir/platform_release_lock.sh"'
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
        preflight_start = workflow.index('"$host_tools_dir/platform_release_preflight.sh"')
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
        remote_script = supervisor
        workflow += "\n" + supervisor
        deploy_workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text()
        self.assertLess(
            deploy_workflow.index("validate-classifier:"),
            deploy_workflow.index("build-release:"),
        )
        build_job = workflow_job(deploy_workflow, "build-release")
        self.assertIn("- validate-classifier", build_job)
        self.assertIn("needs.validate-classifier.result == 'success'", build_job)
        self.assertIn(
            "platform_production_classifier_artifact.py",
            deploy_workflow,
        )
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
        self.assertIn(
            'platform_build_release.sh" --release-slug "$release_slug"',
            workflow,
        )
        self.assertIn(
            '-type f -name "${release_slug}.tar.gz"',
            workflow,
        )
        self.assertIn(
            'candidate_release_slug="${RELEASE_SLUG_BASE}-${short_sha}"',
            workflow,
        )
        self.assertIn('sudo chown "$(id -u):$(id -g)"', workflow)
        self.assertIn(
            '(cd "$release_output" && sha256sum -c "$(basename "$release_checksum")")',
            workflow,
        )
        self.assertIn(
            'bootstrap_dir="$(mktemp -d /tmp/old-sparky-release-bootstrap.XXXXXX)"',
            workflow,
        )
        self.assertIn('--extract-bootstrap-to "$bootstrap_dir"', workflow)
        self.assertIn('--expected-source-commit "$target_sha"', workflow)
        bootstrap_validation_start = workflow.index(
            '"$host_tools_dir/platform_validate_release_artifact.py"'
        )
        bootstrap_validation_end = workflow.index(
            'if ! /usr/bin/python3 -I -B - "$artifact_path"',
            bootstrap_validation_start,
        )
        bootstrap_validation = workflow[
            bootstrap_validation_start:bootstrap_validation_end
        ]
        self.assertLess(
            bootstrap_validation.index('--expected-source-commit "$target_sha"'),
            bootstrap_validation.index('--extract-bootstrap-to "$bootstrap_dir"'),
        )
        self.assertIn(
            'candidate_deploy="$bootstrap_dir/$artifact_slug/tools/platform_release_deploy.sh"',
            workflow,
        )
        self.assertNotIn("candidate_activation_failure", workflow)
        self.assertNotIn("storage_summary_tool", workflow)
        self.assertIn('capture_limit = 64 * 1024', remote_script)
        self.assertIn('os.O_EXCL | os.O_NOFOLLOW', remote_script)
        self.assertIn('"candidate.stdout", "candidate.stderr", "candidate.json"', remote_script)
        self.assertIn('>/dev/null 2>/dev/null', remote_script)
        self.assertIn('fail_with_status "$candidate_status"', remote_script)
        self.assertNotIn("summarize_candidate_failure", remote_script)
        self.assertNotIn("df -hT", workflow)
        self.assertNotIn("findmnt", workflow)
        self.assertNotIn("journalctl -u \"$service\"", workflow)
        self.assertNotIn("platform_build_release.sh", remote_script)
        self.assertNotIn("pip install -r platform/requirements-platform.lock.txt", remote_script)
        self.assertIn(
            '/usr/bin/python3 -I -B - "$artifact_path" "$artifact_slug"',
            remote_script,
        )
        self.assertNotIn(
            '/usr/bin/python3 -B - "$artifact_path" "$artifact_slug"',
            remote_script,
        )
        validator = (REPO_ROOT / "platform/tools/platform_validate_release_artifact.py").read_text()
        self.assertIn("source_git_commit", workflow)
        self.assertIn("expected source commit", validator)
        recover = (
            REPO_ROOT / ".github/workflows/platform-production-release-recover.yml"
        ).read_text()
        recover_provenance = recover.index(
            "Validate exact security and recovery provenance before SSH"
        )
        recover_transfer = recover.index("Transfer exact attested recovery bundle")
        recover_install = recover.index('"$bootstrap_tool" install', recover_transfer)
        recover_validate = recover.index("validate-generation", recover_install)
        recover_wrapper = recover.index(
            '"$trusted_generation/platform_recover_pending.sh"', recover_validate
        )
        self.assertLess(recover_provenance, recover_transfer)
        self.assertLess(recover_transfer, recover_install)
        self.assertLess(recover_install, recover_validate)
        self.assertLess(recover_validate, recover_wrapper)
        self.assertNotIn("exec 9<", recover)
        self.assertNotIn("flock -n 9", recover)
        self.assertNotIn("PLATFORM_RELEASE_LOCK_FD=9", recover)
        self.assertIn("--capability recover_pending", recover)
        self.assertIn('generation_name="$bundle_sha"', recover)
        self.assertIn("trusted_generation=\"$runtime/shared/.release-recovery/generations/$generation_name\"", recover)
        self.assertIn("platform_recover_pending.sh", recover)
        self.assertIn("cleanup_remote_upload", recover)
        self.assertNotIn("$runtime/current/tools", recover)
        self.assertNotIn("platform_release_rollback.sh", recover)

    def test_supervisor_provenance_consumer_matches_canonical_ci_schema(self) -> None:
        """Execute the supervisor consumer against the CI-produced provenance.

        The supervisor is a root-side host helper, so this test extracts and
        executes its actual isolated Python consumer rather than reproducing
        the contract in a second test-only implementation.  The provenance
        producer is likewise extracted from the canonical CI builder step.
        """

        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text(encoding="utf-8")
        build_step = self._workflow_step_run(
            workflow, "Build immutable release artifact in CI"
        )
        producer_match = re.search(
            r"""/usr/bin/python3 - "\$release_output/\$\(basename "\$release_archive"\)" "\$TARGET_SHA" <<'PY'\n"""
            r"(?P<script>.*?)\nPY(?:\n|$)",
            build_step,
            re.DOTALL,
        )
        self.assertIsNotNone(producer_match)
        producer = textwrap.dedent(producer_match.group("script"))

        supervisor = DEPLOY_SUPERVISOR.read_text(encoding="utf-8")
        consumer_match = re.search(
            r"""if ! /usr/bin/python3 -I -B - "\$artifact_path" "\$artifact_slug" "\$target_sha" "\$provenance_path" <<'PY'\n"""
            r"(?P<script>.*?)\nPY(?:\n|$)",
            supervisor,
            re.DOTALL,
        )
        if consumer_match is None:
            consumer_match = re.search(
                r"<<'PY'\n(?P<script>.*?)\nPY(?:\n|$)",
                supervisor,
                re.DOTALL,
            )
        self.assertIsNotNone(consumer_match)
        consumer = textwrap.dedent(consumer_match.group("script"))

        target_sha = "a" * 40
        release_slug = "gha-10917370996-1-aaaaaaaaaaaa"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / f"{release_slug}.tar.gz"
            release_payload = json.dumps(
                {
                    "release_slug": release_slug,
                    "source_git_commit": target_sha,
                },
                sort_keys=True,
            ).encode("utf-8")
            with tarfile.open(artifact, mode="w:gz") as archive:
                member = tarfile.TarInfo(f"{release_slug}/RELEASE.json")
                member.size = len(release_payload)
                archive.addfile(member, io.BytesIO(release_payload))

            provenance = root / "RELEASE.provenance.json"
            produced = subprocess.run(
                ["/usr/bin/python3", "-I", "-", str(artifact), target_sha],
                input=producer,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(produced.returncode, 0, produced.stderr)
            # The canonical builder writes next to the archive.  Keep the
            # expected path explicit so this test cannot accidentally consume
            # a hand-written fixture.
            self.assertTrue(provenance.is_file())
            canonical = json.loads(provenance.read_text(encoding="utf-8"))
            self.assertEqual(
                set(canonical),
                {"schema", "artifact_file", "artifact_sha256", "source_git_commit"},
            )

            def consume(
                *,
                slug: str = release_slug,
                source_sha: str = target_sha,
            ) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        "/usr/bin/python3",
                        "-I",
                        "-B",
                        "-",
                        str(artifact),
                        slug,
                        source_sha,
                        str(provenance),
                    ],
                    input=consumer,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )

            accepted = consume()
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            for mutation in (
                "old-artifact-digest",
                "missing-artifact-sha256",
                "artifact-sha256-type",
                "artifact-sha256-non-hex",
                "wrong-artifact-sha256",
                "artifact-file-mismatch",
                "artifact-file-type",
                "source-type",
                "source-non-hex",
                "source-mismatch",
                "additional-field",
                "schema-type",
                "slug-invalid",
                "slug-mismatch",
                "target-sha-invalid",
            ):
                candidate = dict(canonical)
                if mutation == "old-artifact-digest":
                    candidate["artifact_digest"] = candidate.pop("artifact_sha256")
                elif mutation == "missing-artifact-sha256":
                    candidate.pop("artifact_sha256")
                elif mutation == "artifact-sha256-type":
                    candidate["artifact_sha256"] = 123
                elif mutation == "artifact-sha256-non-hex":
                    candidate["artifact_sha256"] = "g" * 64
                elif mutation == "wrong-artifact-sha256":
                    candidate["artifact_sha256"] = "0" * 64
                elif mutation == "artifact-file-mismatch":
                    candidate["artifact_file"] = "other.tar.gz"
                elif mutation == "artifact-file-type":
                    candidate["artifact_file"] = 123
                elif mutation == "source-type":
                    candidate["source_git_commit"] = 123
                elif mutation == "source-non-hex":
                    candidate["source_git_commit"] = "g" * 40
                elif mutation == "source-mismatch":
                    candidate["source_git_commit"] = "b" * 40
                else:
                    if mutation == "additional-field":
                        candidate["unexpected"] = "rejected"
                    elif mutation == "schema-type":
                        candidate["schema"] = "1"
                provenance.write_text(
                    json.dumps(candidate, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.subTest(mutation=mutation):
                    if mutation == "slug-invalid":
                        rejected = consume(slug="bad/slug")
                    elif mutation == "slug-mismatch":
                        rejected = consume(
                            slug="gha-10917370996-1-bbbbbbbbbbbb"
                        )
                    elif mutation == "target-sha-invalid":
                        rejected = consume(source_sha="a" * 39)
                    else:
                        rejected = consume()
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertNotIn("Traceback", rejected.stderr)

    def test_supervisor_failure_marker_is_stable_and_sanitized(self) -> None:
        supervisor = DEPLOY_SUPERVISOR.read_text(encoding="utf-8")
        marker_start = supervisor.index("failure_class=")
        marker_end = supervisor.index("\ncleanup() {", marker_start)
        marker_functions = supervisor[marker_start:marker_end]
        fixture = f"""set -u
target_sha={'a' * 40}
release_slug=gha-10917370996-1-aaaaaaaaaaaa
{marker_functions}
set +e
set_failure_context artifact provenance provenance_invalid
fail 'private stderr must not cross the public channel'
"""
        completed = subprocess.run(
            ["/bin/bash"],
            input=fixture,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            completed.stdout,
            "RELEASE_DEPLOY schema=1 status=failed class=artifact "
            "phase=provenance reason=provenance_invalid "
            f"release_slug=gha-10917370996-1-aaaaaaaaaaaa source_sha={'a' * 40}\n",
        )
        self.assertEqual(completed.stderr, "ERROR: deployment failed\n")
        self.assertNotIn("private stderr", completed.stdout + completed.stderr)

        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("phase=(artifact|provenance|preflight|candidate|readiness)", workflow)
        self.assertIn("reason=(internal|host_tools_invalid|lock|environment", workflow)
        self.assertIn(
            "lock_stage=(helper_metadata|release_supervise|release_open|retained_supervise|retained_open)",
            workflow,
        )

    def test_supervisor_lock_marker_reports_only_closed_boundary_and_clears_stage(self) -> None:
        supervisor = DEPLOY_SUPERVISOR.read_text(encoding="utf-8")
        marker_start = supervisor.index('failure_class="preflight"')
        marker_end = supervisor.index("\nvalidate_systemctl_binary()", marker_start)
        marker_functions = supervisor[marker_start:marker_end]
        fixture = f"""set -u
target_sha={'a' * 40}
release_slug=gha-10917370996-1-aaaaaaaaaaaa
{marker_functions}
set +e
set_lock_failure_context release_open
fail 'private lock detail must not cross the public channel'
"""
        failed_lock = subprocess.run(
            ["/bin/bash"],
            input=fixture,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(failed_lock.returncode, 1)
        self.assertEqual(
            failed_lock.stdout,
            "RELEASE_DEPLOY schema=1 status=failed class=preflight "
            "phase=preflight reason=lock lock_stage=release_open "
            f"release_slug=gha-10917370996-1-aaaaaaaaaaaa source_sha={'a' * 40}\n",
        )
        self.assertEqual(failed_lock.stderr, "ERROR: deployment failed\n")
        self.assertNotIn("private lock detail", failed_lock.stdout + failed_lock.stderr)

        cleared_fixture = fixture.replace(
            "fail 'private lock detail must not cross the public channel'",
            "set_failure_context preflight preflight environment\n"
            "fail 'private environment detail must not cross the public channel'",
        )
        failed_environment = subprocess.run(
            ["/bin/bash"],
            input=cleared_fixture,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(failed_environment.returncode, 1)
        self.assertEqual(
            failed_environment.stdout,
            "RELEASE_DEPLOY schema=1 status=failed class=preflight "
            "phase=preflight reason=environment "
            f"release_slug=gha-10917370996-1-aaaaaaaaaaaa source_sha={'a' * 40}\n",
        )
        self.assertNotIn("lock_stage=", failed_environment.stdout)
        self.assertEqual(failed_environment.stderr, "ERROR: deployment failed\n")

        stages = {
            "helper_metadata": 'if [[ ! -f "$lock_helper"',
            "release_supervise": "platform_release_lock_supervise",
            "release_open": "platform_release_lock_open ||",
            "retained_supervise": "platform_retained_load_lock_supervise",
            "retained_open": "platform_retained_load_lock_open \\",
        }
        for stage, operation in stages.items():
            with self.subTest(lock_stage=stage):
                self.assertLess(
                    supervisor.index(f"set_lock_failure_context {stage}"),
                    supervisor.index(operation),
                )

    def test_runtime_config_summary_cannot_pollute_deployment_success_marker(self) -> None:
        supervisor = DEPLOY_SUPERVISOR.read_text(encoding="utf-8")
        match = re.search(
            r"(?ms)^apply_shared_env_profile\(\) \{.*?^\}",
            supervisor,
        )
        self.assertIsNotNone(match, "runtime profile helper wrapper is missing")
        self.assertEqual(
            supervisor.count("apply_shared_env_profile " + chr(92)),
            16,
        )
        self.assertNotIn(
            '"$runtime/shared/venv/bin/python" -B '
            '"$host_tools_dir/platform_configure_shared_env.py" ' + chr(92),
            supervisor,
        )

        marker = (
            "RELEASE_DEPLOY schema=1 status=passed class=deployment "
            "release_slug=gha-37266469137-1-cf29087ba231 "
            "source_sha=cf29087ba2313ace344db7f2dd52aa55a0a28fad\n"
        )
        helper_summary = (
            "Shared env baseline: mode=apply; changed=1; "
            "credentials_preserved=true.\n"
        )
        self.assertEqual(len(helper_summary.encode()) + len(marker.encode()), 223)
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            fake_python = runtime / "shared/venv/bin/python"
            fake_python.parent.mkdir(parents=True)
            fake_python.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'Shared env baseline: mode=apply; changed=1; "
                "credentials_preserved=true.'\n"
                "if [ \"${PLATFORM_TEST_HELPER_FAIL:-0}\" = 1 ]; then\n"
                "  printf '%s\\n' 'safe helper failure' >&2\n"
                "  exit 23\n"
                "fi\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)

            fixture = (
                "set -eu\n"
                f"runtime={str(runtime)!r}\n"
                "host_tools_dir=/fixed/host-tools\n"
                f"{match.group(0)}\n"
                "apply_shared_env_profile --apply --profile ready-vote-static-8\n"
                f"printf '%s\\n' {marker.rstrip()!r}\n"
            )
            success = subprocess.run(
                ["/bin/bash"],
                input=fixture,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual(success.stdout, marker)
            self.assertEqual(success.stderr, "")

            forwarded = io.StringIO()
            with redirect_stdout(forwarded):
                dispatched = platform_workflow_remote_dispatch._run_bounded_child(
                    ["/bin/bash", "-c", fixture],
                    timeout_seconds=5,
                    expected_release_marker=(
                        "deploy",
                        "gha-37266469137-1-cf29087ba231",
                        "cf29087ba2313ace344db7f2dd52aa55a0a28fad",
                    ),
                )
            self.assertEqual(dispatched, 0)
            self.assertEqual(forwarded.getvalue(), marker)

            failed = subprocess.run(
                ["/bin/bash"],
                input=fixture,
                env={**os.environ, "PLATFORM_TEST_HELPER_FAIL": "1"},
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(failed.returncode, 23)
            self.assertEqual(failed.stdout, "")
            self.assertEqual(failed.stderr, "safe helper failure\n")

    def test_supervisor_cleanup_covers_upload_failures_and_closes_locks(self) -> None:
        supervisor = DEPLOY_SUPERVISOR.read_text(encoding="utf-8")
        cleanup_start = supervisor.index("cleanup() {")
        cleanup_end = supervisor.index("\ntrap cleanup EXIT", cleanup_start)
        cleanup_body = supervisor[cleanup_start:cleanup_end]
        self.assertLess(
            supervisor.index("platform_release_lock_supervise"),
            supervisor.index("trap cleanup EXIT"),
        )
        self.assertLess(
            supervisor.index("\nplatform_retained_load_lock_open"),
            supervisor.index("trap cleanup EXIT"),
        )
        self.assertIn('rm -rf -- "$artifact_dir"', cleanup_body)
        self.assertIn("artifact_cleanup_owned", cleanup_body)
        self.assertIn("artifact_identity_snapshot", cleanup_body)
        self.assertIn(".old-sparky-platform-artifact-owner", supervisor)
        self.assertIn("platform_retained_load_lock_close", cleanup_body)
        self.assertIn("platform_release_lock_close", cleanup_body)
        self.assertIn("trap - EXIT", cleanup_body)
        self.assertNotIn(
            "trap 'platform_retained_load_lock_close; platform_release_lock_close' EXIT",
            supervisor,
        )

        # Exercise the actual supervisor in a root-owned immutable-generation
        # fixture.  Invalid input must return before ownership/trap setup and
        # preserve the pre-existing matching directory byte-for-byte.
        with tempfile.TemporaryDirectory() as temporary:
            fixture_root = Path(temporary)
            fixture, release_lock, retained_lock, generation_dir = self._install_supervisor_fixture(
                fixture_root
            )

            def cleanup_fixture() -> None:
                for path in (release_lock, retained_lock):
                    if path.exists() or path.is_symlink():
                        path.unlink()
                if generation_dir.exists() or generation_dir.is_symlink():
                    shutil.rmtree(generation_dir)

            self.addCleanup(cleanup_fixture)
            target_sha = "a" * 40
            run_id = str(os.getpid())
            artifact = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-1")
            if artifact.exists() or artifact.is_symlink():
                run_id = str(int(run_id) + 1)
                artifact = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-1")
            release_slug = f"gha-{run_id}-1-{target_sha[:12]}"
            artifact.mkdir(mode=0o700)
            os.chown(artifact, 0, 0)
            os.chmod(artifact, 0o700)
            marker = artifact / ".old-sparky-platform-artifact-owner"
            marker.write_text(
                f"platform_prepare_artifact_dir schema=1 "
                f"dev={artifact.stat().st_dev} ino={artifact.stat().st_ino}\n",
                encoding="ascii",
            )
            os.chown(marker, 0, 0)
            os.chmod(marker, 0o600)
            sentinel = artifact / "preexisting-sentinel"
            sentinel.write_bytes(b"must-survive-early-failure\n")
            os.chown(sentinel, 0, 0)
            os.chmod(sentinel, 0o600)

            def cleanup_artifact() -> None:
                if artifact.is_dir() and not artifact.is_symlink():
                    shutil.rmtree(artifact)

            self.addCleanup(cleanup_artifact)

            def run(*arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        str(fixture),
                        target_sha,
                        *arguments,
                        str(artifact),
                        "baseline",
                    ],
                    env={
                        **os.environ,
                        "PLATFORM_TEST_RUNTIME": str(fixture_root / "runtime"),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )

            for invalid in (
                ("bad-release-slug", "deploy"),
                (release_slug, "invalid-mode"),
            ):
                rejected = run(invalid[0], invalid[1])
                self.assertEqual(rejected.returncode, 2, rejected.stderr)
                self.assertTrue(artifact.is_dir())
                self.assertEqual(sentinel.read_bytes(), b"must-survive-early-failure\n")

            rejected_profile = subprocess.run(
                [
                    str(fixture),
                    target_sha,
                    release_slug,
                    "deploy",
                    str(artifact),
                    "invalid-profile",
                ],
                env={
                    **os.environ,
                    "PLATFORM_TEST_RUNTIME": str(fixture_root / "runtime"),
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertEqual(rejected_profile.returncode, 2, rejected_profile.stderr)
            self.assertTrue(artifact.is_dir())
            self.assertEqual(sentinel.read_bytes(), b"must-survive-early-failure\n")

            release_lock.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(release_lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.fchown(lock_fd, 0, 0)
                os.fchmod(lock_fd, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                contended = run(release_slug, "deploy")
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            self.assertNotEqual(contended.returncode, 0)
            self.assertTrue(artifact.is_dir())
            self.assertEqual(sentinel.read_bytes(), b"must-survive-early-failure\n")

            retained_lock_fd = os.open(retained_lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.fchown(retained_lock_fd, 0, 0)
                os.fchmod(retained_lock_fd, 0o600)
                fcntl.flock(retained_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                retained_contended = run(release_slug, "deploy")
            finally:
                fcntl.flock(retained_lock_fd, fcntl.LOCK_UN)
                os.close(retained_lock_fd)
            self.assertNotEqual(retained_contended.returncode, 0)
            self.assertTrue(artifact.is_dir())
            self.assertEqual(sentinel.read_bytes(), b"must-survive-early-failure\n")

            cleaned = run(release_slug, "deploy")
            self.assertNotEqual(cleaned.returncode, 0)
            self.assertFalse(artifact.exists())
            self.assertFalse(artifact.is_symlink())

    def test_host_tools_contract_failure_emits_marker_before_locks(self) -> None:
        """A silent closure-validator failure reaches the closed marker path."""

        if os.geteuid() != 0:
            self.skipTest("immutable supervisor fixture requires root")
        with tempfile.TemporaryDirectory() as temporary:
            fixture_root = Path(temporary)
            supervisor, release_lock, retained_lock, generation_dir = (
                self._install_supervisor_fixture(fixture_root)
            )

            def cleanup_fixture() -> None:
                for path in (release_lock, retained_lock):
                    if path.exists() or path.is_symlink():
                        path.unlink()
                if generation_dir.exists() or generation_dir.is_symlink():
                    shutil.rmtree(generation_dir)

            self.addCleanup(cleanup_fixture)
            target_sha = "a" * 40
            run_id = str(os.getpid())
            artifact = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-1")
            while artifact.exists() or artifact.is_symlink():
                run_id = str(int(run_id) + 1)
                artifact = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-1")
            release_slug = f"gha-{run_id}-1-{target_sha[:12]}"
            runtime = fixture_root / "runtime"
            runtime.mkdir()
            sentinel = runtime / "unchanged"
            sentinel.write_bytes(b"no release lifecycle work\n")
            environment = {"PLATFORM_TEST_RUNTIME": str(runtime)}
            command = [
                str(supervisor),
                target_sha,
                release_slug,
                "preflight",
                str(artifact),
                "baseline",
                generation_dir.name,
                "b" * 64,
                "c" * 64,
            ]
            output = io.StringIO()
            with patch.dict(os.environ, environment, clear=True):
                with redirect_stdout(output):
                    status = platform_workflow_remote_dispatch._run_bounded_child(
                        command,
                        timeout_seconds=20,
                        expected_release_marker=(
                            "preflight",
                            release_slug,
                            target_sha,
                        ),
                    )

            self.assertEqual(status, 1)
            self.assertEqual(
                output.getvalue(),
                "RELEASE_DEPLOY schema=1 status=failed class=preflight "
                "phase=preflight reason=host_tools_invalid "
                f"release_slug={release_slug} source_sha={target_sha}\n",
            )
            self.assertFalse(release_lock.exists())
            self.assertFalse(retained_lock.exists())
            self.assertFalse(artifact.exists())
            self.assertEqual(sentinel.read_bytes(), b"no release lifecycle work\n")

            # A malformed but correctly digest-bound manifest takes the JSON
            # parser's unexpected-exception path. Its traceback must remain
            # private while the supervisor emits only its fixed failure line.
            malformed_manifest = b"{"
            capabilities = b"python_bytecode_disabled\n"
            manifest_path = generation_dir / "manifest.json"
            manifest_path.write_bytes(malformed_manifest)
            os.chown(manifest_path, 0, 0)
            os.chmod(manifest_path, 0o444)
            capabilities_path = generation_dir / "capabilities.txt"
            capabilities_path.write_bytes(capabilities)
            os.chown(capabilities_path, 0, 0)
            os.chmod(capabilities_path, 0o444)
            command[-2] = hashlib.sha256(malformed_manifest).hexdigest()
            command[-1] = hashlib.sha256(capabilities).hexdigest()
            result = subprocess.run(
                command,
                check=False,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stdout,
                "RELEASE_DEPLOY schema=1 status=failed class=preflight "
                "phase=preflight reason=host_tools_invalid "
                f"release_slug={release_slug} source_sha={target_sha}\n",
            )
            self.assertEqual(result.stderr, "ERROR: deployment failed\n")
            self.assertFalse(release_lock.exists())
            self.assertFalse(retained_lock.exists())
            self.assertFalse(artifact.exists())
            self.assertEqual(sentinel.read_bytes(), b"no release lifecycle work\n")

    def test_bounded_child_forwards_only_valid_marker_or_closed_diagnostic(self) -> None:
        slug = "gha-123456789-1-aaaaaaaaaaaa"
        sha = "a" * 40
        expected = ("deploy", slug, sha)
        valid = (
            "RELEASE_DEPLOY schema=1 status=failed class=preflight "
            f"phase=preflight reason=host_tools_invalid release_slug={slug} "
            f"source_sha={sha}\n"
        ).encode("ascii")
        child_code = (
            "import os,sys; "
            "os.write(1, bytes.fromhex(sys.argv[1])); "
            "os.write(2, b'PRIVATE_CHILD_STDERR_SENTINEL'); "
            "raise SystemExit(int(sys.argv[2]))"
        )

        def run_child(stdout: bytes, exit_status: int) -> tuple[int, str]:
            command = [
                sys.executable,
                "-I",
                "-B",
                "-c",
                child_code,
                stdout.hex(),
                str(exit_status),
            ]
            captured = io.StringIO()
            with redirect_stdout(captured):
                status = platform_workflow_remote_dispatch._run_bounded_child(
                    command,
                    timeout_seconds=5,
                    expected_release_marker=expected,
                )
            return status, captured.getvalue()

        status, output = run_child(valid, 1)
        self.assertEqual(status, 1)
        self.assertEqual(output, valid.decode("ascii"))
        self.assertNotIn("PRIVATE", output)

        rejected = (
            (b"", 7, "missing_marker", 0, 7),
            (
                valid.replace(sha.encode("ascii"), ("b" * 40).encode("ascii")),
                1,
                "invalid_marker",
                len(valid),
                1,
            ),
            (b"malformed PRIVATE child output\n", 3, "invalid_marker", 31, 3),
            (valid + valid, 4, "invalid_marker", len(valid) * 2, 4),
            (b"x" * 600, 5, "oversized_marker", 513, 5),
            (
                valid.replace(sha.encode("ascii"), ("b" * 40).encode("ascii")),
                0,
                "invalid_marker",
                len(valid),
                2,
            ),
        )
        for data, child_status, reason, observed_bytes, dispatcher_status in rejected:
            with self.subTest(reason=reason, child_status=child_status):
                status, output = run_child(data, child_status)
                self.assertEqual(status, dispatcher_status)
                self.assertEqual(
                    output,
                    "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed "
                    f"reason={reason} child_exit={child_status} "
                    f"observed_bytes={observed_bytes} "
                    f"dispatcher_exit={dispatcher_status}\n",
                )
                self.assertNotIn("PRIVATE", output)
                self.assertNotIn("malformed", output)

    def test_cloudflare_failed_oneshot_is_quiescent_only_with_empty_cgroup_contract(
        self,
    ) -> None:
        deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        helpers = "\n\n".join(
            self._deploy_shell_function_source(deploy, name)
            for name in (
                "cloudflare_failed_oneshot_is_empty",
                "read_cloudflare_quiescence_state",
            )
        )
        harness = (
            "set -uo pipefail\n"
            + helpers
            + "\nrun_systemctl() {\n"
            '  case "$1" in\n'
            '    is-active) [[ "$2" == "deadlock-cloudflare-ips.service" ]] || '
            'return 91; printf \'%s\\n\' "$FAKE_ACTIVE_OUTPUT"; '
            'return "$FAKE_ACTIVE_STATUS" ;;\n'
            '    show) [[ "$2" == "deadlock-cloudflare-ips.service" && '
            '"$3" == "--property=ActiveState,SubState,Type,RemainAfterExit,'
            'KillMode,MainPID,ControlPID,ControlGroup" ]] || return 92; '
            'printf \'%s\' "$FAKE_SHOW_OUTPUT"; '
            'printf \'called\\n\' >>"$FAKE_SHOW_CALLS"; '
            'return "$FAKE_SHOW_STATUS" ;;\n'
            '    *) return 90 ;;\n'
            '  esac\n'
            "}\n"
            "public_status() { printf 'status=%s reason=%s\\n' \"$1\" \"$2\" >&2; }\n"
            'state=""\n'
            'if state="$(read_cloudflare_quiescence_state)"; then status=0; '
            'else status="$?"; fi\n'
            'printf \'status=%s\\nstate=%s\\n\' "$status" "$state"\n'
        )
        keys = (
            "ActiveState",
            "SubState",
            "Type",
            "RemainAfterExit",
            "KillMode",
            "MainPID",
            "ControlPID",
            "ControlGroup",
        )
        valid_properties = {
            "ActiveState": "failed",
            "SubState": "failed",
            "Type": "oneshot",
            "RemainAfterExit": "no",
            "KillMode": "control-group",
            "MainPID": "0",
            "ControlPID": "0",
            "ControlGroup": "",
        }

        def run_reader(
            *,
            active_output: str,
            active_status: int,
            properties: list[tuple[str, str]],
            show_status: int = 0,
        ) -> tuple[subprocess.CompletedProcess[str], bool]:
            with tempfile.TemporaryDirectory(prefix="cloudflare-quiescence-") as tmp:
                root = Path(tmp)
                script = root / "reader.sh"
                script.write_text(harness, encoding="utf-8")
                calls = root / "show-calls"
                env = os.environ.copy()
                env.update(
                    {
                        "FAKE_ACTIVE_OUTPUT": active_output,
                        "FAKE_ACTIVE_STATUS": str(active_status),
                        "FAKE_SHOW_OUTPUT": "".join(
                            f"{key}={value}\n" for key, value in properties
                        ),
                        "FAKE_SHOW_STATUS": str(show_status),
                        "FAKE_SHOW_CALLS": str(calls),
                    }
                )
                env.pop("BASH_ENV", None)
                env.pop("ENV", None)
                result = subprocess.run(
                    ["/usr/bin/bash", "--noprofile", "--norc", str(script)],
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=5,
                )
                return result, calls.exists()

        valid_lines = list(valid_properties.items())
        accepted = run_reader(
            active_output="failed",
            active_status=3,
            properties=valid_lines,
        )
        self.assertEqual(accepted[0].returncode, 0, accepted[0].stderr)
        self.assertEqual(accepted[0].stdout, "status=0\nstate=inactive\n")
        self.assertEqual(accepted[0].stderr, "")
        self.assertTrue(accepted[1])

        for output, status, expected in (
            ("active", 0, "active"),
            ("inactive", 3, "inactive"),
        ):
            with self.subTest(state=output):
                result, show_calls = run_reader(
                    active_output=output,
                    active_status=status,
                    properties=[],
                    show_status=1,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"status=0\nstate={expected}\n")
                self.assertFalse(show_calls)

        invalid_cases: list[tuple[str, int, list[tuple[str, str]], int]] = []
        for key, invalid_value in (
            ("ActiveState", "active"),
            ("SubState", "dead"),
            ("Type", "simple"),
            ("RemainAfterExit", "yes"),
            ("KillMode", "process"),
            ("MainPID", "12"),
            ("ControlPID", "8"),
            ("ControlGroup", "/system.slice/deadlock-cloudflare-ips.service"),
        ):
            altered = list(valid_lines)
            altered[keys.index(key)] = (key, invalid_value)
            invalid_cases.append(("failed", 3, altered, 0))
        invalid_cases.extend(
            (
                ("failed", 3, valid_lines + [("MainPID", "0")], 0),
                ("failed", 3, valid_lines + [("Unexpected", "value")], 0),
                ("failed", 3, valid_lines[:-1], 0),
                ("failed", 3, valid_lines, 1),
                ("failed", 4, valid_lines, 0),
                ("failed", 0, valid_lines, 0),
            )
        )
        for active_output, active_status, properties, show_status in invalid_cases:
            with self.subTest(properties=properties, show_status=show_status):
                result, _show_calls = run_reader(
                    active_output=active_output,
                    active_status=active_status,
                    properties=properties,
                    show_status=show_status,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "status=1\nstate=\n")
                self.assertEqual(
                    result.stderr,
                    "status=failed reason=service_state\n",
                )

    def test_liveqa_reconcile_stderr_is_available_only_to_private_candidate_capture(
        self,
    ) -> None:
        """A failed runtime reconcile keeps diagnostics for the candidate runner only."""

        source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        match = re.search(
            r"(?ms)^run_live_qa_reconcile\(\) \{\n.*?^\}", source
        )
        self.assertIsNotNone(match)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_python = root / "shared" / "venv" / "bin" / "python"
            fake_python.parent.mkdir(parents=True)
            fake_python.write_text(
                "#!/bin/sh\n"
                "printf 'runtime_stdout_sentinel\\n'\n"
                "printf 'runtime_private_stderr_sentinel\\n' >&2\n"
                "exit 23\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            deadline_assignment = (
                'PLATFORM_CANDIDATE_DEADLINE_MONOTONIC_NS=$(/usr/bin/python3 '
                '-I -B -c "import time; print(time.monotonic_ns() + '
                '1800000000000)")'
            )
            harness = "\n".join(
                (
                    'run_systemd_bounded() { "$@"; }',
                    'SYSTEMCTL_TIMEOUT_BIN=/usr/bin/timeout',
                    'LIVE_QA_RECONCILE_TIMEOUT_SECONDS=600',
                    'LIVE_QA_POST_RECONCILE_RESERVE_SECONDS=600',
                    'LIVE_QA_TIMEOUT_KILL_GRACE_SECONDS=5',
                    deadline_assignment,
                    f"SHARED_VENV={shlex.quote(str(root / 'shared' / 'venv'))}",
                    f"LIVE_QA_RUNTIME_INSTALLER={shlex.quote(str(root / 'installer.py'))}",
                    f"APP_DIR={shlex.quote(str(root / 'app'))}",
                    match.group(0),
                    "run_live_qa_reconcile",
                    "",
                )
            )
            result = subprocess.run(
                [
                    "/usr/bin/env",
                    "-u",
                    "BASH_ENV",
                    "-u",
                    "ENV",
                    "/usr/bin/bash",
                    "--noprofile",
                    "--norc",
                    "-c",
                    harness,
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            "runtime_private_stderr_sentinel\n"
            "LIVE_QA_RECONCILE status=failed outcome=child_exit "
            "budget_seconds=600 child_exit=23\n",
        )

        missing_deadline_harness = harness.replace(deadline_assignment + "\n", "")
        missing_deadline = subprocess.run(
            [
                "/usr/bin/env",
                "-u",
                "BASH_ENV",
                "-u",
                "ENV",
                "/usr/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                missing_deadline_harness,
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        self.assertEqual(missing_deadline.returncode, 1)
        self.assertEqual(missing_deadline.stdout, "")
        self.assertEqual(
            missing_deadline.stderr,
            "LIVE_QA_RECONCILE status=failed outcome=deadline_unavailable\n",
        )

    def test_candidate_capture_runner_is_private_bounded_and_composes_with_dispatcher(
        self,
    ) -> None:
        """The real candidate runner keeps noisy output private and preserves its marker ABI."""

        if os.geteuid() != 0:
            self.skipTest("candidate diagnostic ownership fixture requires root")
        supervisor = DEPLOY_SUPERVISOR.read_text(encoding="utf-8")
        runner = self._candidate_diagnostic_runner_source(supervisor)
        target_sha = "a" * 40
        host_tools_sha = "b" * 40
        run_id = str(os.getpid())
        attempt = "41"
        release_slug = f"gha-{run_id}-{attempt}-{target_sha[:12]}"

        with tempfile.TemporaryDirectory(prefix="candidate-diagnostics-") as tmp:
            root = Path(tmp)
            private_parent = root / "var-tmp"
            artifact = root / "candidate.tar.gz"
            artifact.write_bytes(b"isolated fixture\n")

            def cleanup_pid_file(pid_file: Path) -> None:
                if not pid_file.exists():
                    return
                try:
                    child_pid = int(pid_file.read_text(encoding="ascii"))
                    os.kill(child_pid, 9)
                except (OSError, ValueError):
                    pass

            def assert_pid_stopped(pid_file: Path) -> None:
                child_pid = int(pid_file.read_text(encoding="ascii"))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
                    except (FileNotFoundError, ProcessLookupError):
                        return
                    if state == "Z":
                        return
                    time.sleep(0.02)
                self.fail("candidate runner left a descendant process running")

            noisy_candidate = root / "noisy-candidate"
            noisy_candidate.write_text(
                "#!/usr/bin/python3\n"
                "import os\n"
                "os.write(1, ('CANDIDATE_DEADLINE=' + os.environ.get('PLATFORM_CANDIDATE_DEADLINE_MONOTONIC_NS', '') + '\\n').encode())\n"
                "os.write(1, b'PRIVATE_CANDIDATE_STDOUT_SENTINEL' + b'x' * 70000)\n"
                "os.write(2, b'PRIVATE_CANDIDATE_STDERR_SENTINEL' + b'y' * 70000)\n"
                "raise SystemExit(23)\n",
                encoding="ascii",
            )
            noisy_candidate.chmod(0o700)

            failed = self._run_candidate_diagnostic_runner(
                runner,
                candidate=noisy_candidate,
                diagnostic_parent=private_parent,
                release_slug=release_slug,
                source_sha=target_sha,
                run_id=run_id,
                attempt=attempt,
                host_tools_sha=host_tools_sha,
            )
            self.assertEqual(failed.returncode, 0, failed.stderr)
            self.assertEqual(failed.stderr, "")
            self.assertRegex(
                failed.stdout,
                r"^candidate_status=23 capture_state=truncated\n$",
            )
            run_dir = private_parent / "oldsparky-release-diagnostics" / f"{run_id}-{attempt}"
            self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
            self.assertEqual(run_dir.stat().st_uid, 0)
            self.assertEqual({path.name for path in run_dir.iterdir()}, {
                "candidate.stdout",
                "candidate.stderr",
                "candidate.json",
            })
            for name in ("candidate.stdout", "candidate.stderr", "candidate.json"):
                info = (run_dir / name).stat()
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                self.assertEqual(info.st_uid, 0)
                self.assertEqual(info.st_nlink, 1)
            self.assertEqual((run_dir / "candidate.stdout").stat().st_size, 65536)
            self.assertEqual((run_dir / "candidate.stderr").stat().st_size, 65536)
            captured_stdout = (run_dir / "candidate.stdout").read_text(encoding="ascii")
            self.assertRegex(captured_stdout, r"CANDIDATE_DEADLINE=[0-9]{1,20}\n")
            metadata = json.loads((run_dir / "candidate.json").read_text())
            self.assertEqual(metadata["source_sha"], target_sha)
            self.assertEqual(metadata["host_tools_sha"], host_tools_sha)
            self.assertEqual(metadata["release_slug"], release_slug)
            self.assertEqual(metadata["run_id"], run_id)
            self.assertEqual(metadata["attempt"], attempt)
            self.assertEqual(metadata["phase"], "candidate")
            self.assertEqual(metadata["candidate_started"], True)
            self.assertEqual(metadata["candidate_exit_status"], 23)
            self.assertEqual(metadata["capture_state"], "truncated")
            self.assertEqual(metadata["stdout_observed_bytes"], 65537)
            self.assertEqual(metadata["stderr_observed_bytes"], 65537)
            self.assertNotIn("PRIVATE_CANDIDATE", failed.stdout)

            expected_marker = (
                "RELEASE_DEPLOY schema=1 status=failed class=deployment "
                "phase=candidate reason=activation_failed "
                f"release_slug={release_slug} source_sha={target_sha}\n"
            )
            marker_harness = self._candidate_diagnostic_marker_harness(
                supervisor,
                result=failed.stdout.rstrip("\n"),
                release_slug=release_slug,
                source_sha=target_sha,
            )
            direct = subprocess.run(
                ["/bin/bash", "--noprofile", "--norc", "-c", marker_harness],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin"},
                timeout=5,
                check=False,
            )
            self.assertEqual(direct.returncode, 23, direct.stderr)
            self.assertEqual(direct.stdout, expected_marker)
            dispatcher_output = io.StringIO()
            with redirect_stdout(dispatcher_output):
                status = platform_workflow_remote_dispatch._run_bounded_child(
                    ["/bin/bash", "--noprofile", "--norc", "-c", marker_harness],
                    timeout_seconds=5,
                    expected_release_marker=("deploy", release_slug, target_sha),
                )
            self.assertEqual(status, 23, dispatcher_output.getvalue())
            self.assertEqual(dispatcher_output.getvalue(), expected_marker)
            self.assertNotIn("PRIVATE_CANDIDATE", dispatcher_output.getvalue())

            # A successful candidate follows the same runner but removes only
            # its exact completed capture after writing the metadata durably.
            success_candidate = root / "success-candidate"
            success_candidate.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            success_candidate.chmod(0o700)
            success_run_id = str(int(run_id) + 1)
            success_slug = f"gha-{success_run_id}-{attempt}-{target_sha[:12]}"
            succeeded = self._run_candidate_diagnostic_runner(
                runner,
                candidate=success_candidate,
                diagnostic_parent=private_parent,
                release_slug=success_slug,
                source_sha=target_sha,
                run_id=success_run_id,
                attempt=attempt,
                host_tools_sha=host_tools_sha,
            )
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            self.assertEqual(succeeded.stdout, "candidate_status=0 capture_state=complete\n")
            self.assertFalse(
                (private_parent / "oldsparky-release-diagnostics" / f"{success_run_id}-{attempt}").exists()
            )

            def invoke(
                candidate: Path,
                numeric_run_id: int,
                *,
                runner_source: str = runner,
                run_attempt: str = attempt,
            ) -> tuple[subprocess.CompletedProcess[str], str, Path]:
                bound_run_id = str(numeric_run_id)
                bound_slug = f"gha-{bound_run_id}-{run_attempt}-{target_sha[:12]}"
                completed = self._run_candidate_diagnostic_runner(
                    runner_source,
                    candidate=candidate,
                    diagnostic_parent=private_parent,
                    release_slug=bound_slug,
                    source_sha=target_sha,
                    run_id=bound_run_id,
                    attempt=run_attempt,
                    host_tools_sha=host_tools_sha,
                )
                return (
                    completed,
                    bound_slug,
                    private_parent
                    / "oldsparky-release-diagnostics"
                    / f"{bound_run_id}-{run_attempt}",
                )

            signal_candidate = root / "signal-candidate"
            signal_candidate.write_text(
                "#!/usr/bin/python3\n"
                "import os, signal\n"
                "os.kill(os.getpid(), signal.SIGTERM)\n",
                encoding="ascii",
            )
            signal_candidate.chmod(0o700)
            signal_result, signal_slug, signal_dir = invoke(
                signal_candidate, int(run_id) + 7
            )
            self.assertEqual(
                signal_result.stdout,
                "candidate_status=143 capture_state=complete\n",
            )
            signal_metadata = json.loads((signal_dir / "candidate.json").read_text())
            self.assertEqual(signal_metadata["candidate_exit_status"], 143)
            signal_marker_harness = self._candidate_diagnostic_marker_harness(
                supervisor,
                result=signal_result.stdout.rstrip("\n"),
                release_slug=signal_slug,
                source_sha=target_sha,
            )
            signal_output = io.StringIO()
            with redirect_stdout(signal_output):
                signal_status = platform_workflow_remote_dispatch._run_bounded_child(
                    ["/bin/bash", "--noprofile", "--norc", "-c", signal_marker_harness],
                    timeout_seconds=5,
                    expected_release_marker=("deploy", signal_slug, target_sha),
                )
            self.assertEqual(signal_status, 143)
            self.assertEqual(
                signal_output.getvalue(),
                "RELEASE_DEPLOY schema=1 status=failed class=deployment "
                "phase=candidate reason=activation_failed "
                f"release_slug={signal_slug} source_sha={target_sha}\n",
            )

            # Binding or exclusive-directory failure is rejected before the
            # fake candidate can leave its start sentinel or overwrite an
            # existing same-run diagnostic directory.
            start_sentinel = root / "candidate-started"
            setup_candidate = root / "setup-candidate"
            setup_candidate.write_text(
                "#!/bin/sh\nprintf started > "
                + shlex.quote(str(start_sentinel))
                + "\n",
                encoding="ascii",
            )
            setup_candidate.chmod(0o700)
            bad_binding = self._run_candidate_diagnostic_runner(
                runner,
                candidate=setup_candidate,
                diagnostic_parent=private_parent,
                release_slug="wrong-release-binding",
                source_sha=target_sha,
                run_id=str(int(run_id) + 2),
                attempt=attempt,
                host_tools_sha=host_tools_sha,
            )
            self.assertEqual(
                bad_binding.stdout,
                "candidate_status=none capture_state=setup_failed\n",
            )
            self.assertFalse(start_sentinel.exists())
            bad_slug = f"gha-{int(run_id) + 2}-{attempt}-{target_sha[:12]}"
            setup_marker_harness = self._candidate_diagnostic_marker_harness(
                supervisor,
                result=bad_binding.stdout.rstrip("\n"),
                release_slug=bad_slug,
                source_sha=target_sha,
            )
            setup_output = io.StringIO()
            with redirect_stdout(setup_output):
                setup_status = platform_workflow_remote_dispatch._run_bounded_child(
                    ["/bin/bash", "--noprofile", "--norc", "-c", setup_marker_harness],
                    timeout_seconds=5,
                    expected_release_marker=("deploy", bad_slug, target_sha),
                )
            self.assertEqual(setup_status, 1)
            self.assertEqual(
                setup_output.getvalue(),
                "RELEASE_DEPLOY schema=1 status=failed class=preflight "
                f"phase=preflight reason=internal release_slug={bad_slug} "
                f"source_sha={target_sha}\n",
            )

            diagnostics_root = private_parent / "oldsparky-release-diagnostics"
            collision_run_id = str(int(run_id) + 3)
            collision_name = f"{collision_run_id}-{attempt}"
            collision_dir = diagnostics_root / collision_name
            collision_dir.mkdir(mode=0o700)
            os.chown(collision_dir, 0, 0)
            os.chmod(collision_dir, 0o700)
            collision_sentinel = collision_dir / "owner-sentinel"
            collision_sentinel.write_bytes(b"must remain untouched\n")
            os.chown(collision_sentinel, 0, 0)
            os.chmod(collision_sentinel, 0o600)
            collided, _, _ = invoke(setup_candidate, int(collision_run_id))
            self.assertEqual(
                collided.stdout,
                "candidate_status=none capture_state=setup_failed\n",
            )
            self.assertFalse(start_sentinel.exists())
            self.assertEqual(collision_sentinel.read_bytes(), b"must remain untouched\n")

            symlink_parent = root / "symlink-parent"
            symlink_parent.mkdir(mode=0o700)
            os.chown(symlink_parent, 0, 0)
            os.chmod(symlink_parent, 0o1777)
            symlink_target = root / "symlink-target"
            symlink_target.mkdir(mode=0o700)
            symlink_sentinel = symlink_target / "must-survive"
            symlink_sentinel.write_bytes(b"trusted target remains untouched\n")
            os.chown(symlink_target, 0, 0)
            os.chmod(symlink_target, 0o700)
            os.chown(symlink_sentinel, 0, 0)
            os.chmod(symlink_sentinel, 0o600)
            (symlink_parent / "oldsparky-release-diagnostics").symlink_to(
                symlink_target,
                target_is_directory=True,
            )
            symlink_result = self._run_candidate_diagnostic_runner(
                runner,
                candidate=setup_candidate,
                diagnostic_parent=symlink_parent,
                release_slug=f"gha-{int(run_id) + 8}-{attempt}-{target_sha[:12]}",
                source_sha=target_sha,
                run_id=str(int(run_id) + 8),
                attempt=attempt,
                host_tools_sha=host_tools_sha,
            )
            self.assertEqual(
                symlink_result.stdout,
                "candidate_status=none capture_state=setup_failed\n",
            )
            self.assertFalse(start_sentinel.exists())
            self.assertEqual(symlink_sentinel.read_bytes(), b"trusted target remains untouched\n")

            missing_candidate = root / "does-not-exist"
            spawn_failed, spawn_slug, spawn_dir = invoke(
                missing_candidate, int(run_id) + 4
            )
            self.assertEqual(
                spawn_failed.stdout,
                "candidate_status=none capture_state=spawn_failed\n",
            )
            self.assertTrue(spawn_dir.is_dir())
            spawn_metadata = json.loads((spawn_dir / "candidate.json").read_text())
            self.assertFalse(spawn_metadata["candidate_started"])
            self.assertIsNone(spawn_metadata["candidate_exit_status"])
            spawn_marker_harness = self._candidate_diagnostic_marker_harness(
                supervisor,
                result=spawn_failed.stdout.rstrip("\n"),
                release_slug=spawn_slug,
                source_sha=target_sha,
            )
            spawn_output = io.StringIO()
            with redirect_stdout(spawn_output):
                spawn_status = platform_workflow_remote_dispatch._run_bounded_child(
                    ["/bin/bash", "--noprofile", "--norc", "-c", spawn_marker_harness],
                    timeout_seconds=5,
                    expected_release_marker=("deploy", spawn_slug, target_sha),
                )
            self.assertEqual(spawn_status, 1)
            self.assertEqual(
                spawn_output.getvalue(),
                "RELEASE_DEPLOY schema=1 status=failed class=deployment "
                "phase=candidate reason=candidate_missing "
                f"release_slug={spawn_slug} source_sha={target_sha}\n",
            )

            timeout_candidate = root / "timeout-candidate"
            timeout_pid_file = root / "timeout-descendant.pid"
            self.addCleanup(cleanup_pid_file, timeout_pid_file)
            timeout_candidate.write_text(
                "#!/usr/bin/python3\n"
                "import os, time\n"
                "pid = os.fork()\n"
                "if pid == 0:\n"
                "    time.sleep(30)\n"
                "    os._exit(0)\n"
                f"open({str(timeout_pid_file)!r}, 'w').write(str(pid))\n"
                "time.sleep(30)\n",
                encoding="ascii",
            )
            timeout_candidate.chmod(0o700)
            fast_timeout_runner = runner.replace(
                "capture_timeout_seconds = 1800.0",
                "capture_timeout_seconds = 0.3",
                1,
            ).replace(
                "termination_grace_seconds = 2.0",
                "termination_grace_seconds = 0.2",
                1,
            )
            self.assertNotEqual(fast_timeout_runner, runner)
            timed_out, timeout_slug, timeout_dir = invoke(
                timeout_candidate,
                int(run_id) + 5,
                runner_source=fast_timeout_runner,
            )
            self.assertRegex(
                timed_out.stdout,
                r"^candidate_status=(1[2-9][0-9]|[2-9][0-9]{2}) capture_state=timeout\n$",
            )
            timeout_metadata = json.loads((timeout_dir / "candidate.json").read_text())
            self.assertEqual(timeout_metadata["capture_state"], "timeout")
            self.assertTrue(timeout_metadata["candidate_started"])
            assert_pid_stopped(timeout_pid_file)
            timeout_marker = self._candidate_diagnostic_marker_harness(
                supervisor,
                result=timed_out.stdout.rstrip("\n"),
                release_slug=timeout_slug,
                source_sha=target_sha,
            )
            timeout_output = io.StringIO()
            with redirect_stdout(timeout_output):
                timeout_status = platform_workflow_remote_dispatch._run_bounded_child(
                    ["/bin/bash", "--noprofile", "--norc", "-c", timeout_marker],
                    timeout_seconds=5,
                    expected_release_marker=("deploy", timeout_slug, target_sha),
                )
            self.assertEqual(timeout_status, 1)
            self.assertEqual(
                timeout_output.getvalue(),
                "RELEASE_DEPLOY schema=1 status=failed class=deployment "
                "phase=candidate reason=activation_failed "
                f"release_slug={timeout_slug} source_sha={target_sha}\n",
            )

            stuck_candidate = root / "stuck-candidate"
            stuck_candidate.write_text(
                "#!/usr/bin/python3\n"
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "while True: time.sleep(1)\n",
                encoding="ascii",
            )
            stuck_candidate.chmod(0o700)
            bounded_reap_runner = runner.replace(
                "capture_timeout_seconds = 1800.0",
                "capture_timeout_seconds = 0.3",
                1,
            ).replace(
                "termination_grace_seconds = 2.0",
                "termination_grace_seconds = 0.1",
                1,
            )
            killpg_marker = "def signal_group(signum):\n"
            self.assertIn(killpg_marker, bounded_reap_runner)
            bounded_reap_runner = bounded_reap_runner.replace(
                killpg_marker,
                "real_killpg = os.killpg\n"
                "kill_seen = False\n"
                "def injected_killpg(pgid, signum):\n"
                "    global kill_seen\n"
                "    if signum == signal.SIGKILL:\n"
                "        kill_seen = True\n"
                "    return real_killpg(pgid, signum)\n"
                "class FaultWaitPopen(subprocess.Popen):\n"
                "    def wait(self, timeout=None):\n"
                "        if kill_seen:\n"
                "            raise subprocess.TimeoutExpired(self.args, timeout)\n"
                "        return super().wait(timeout=timeout)\n"
                "subprocess.Popen = FaultWaitPopen\n"
                "os.killpg = injected_killpg\n"
                + killpg_marker,
                1,
            )
            unreaped, unreaped_slug, unreaped_dir = invoke(
                stuck_candidate,
                int(run_id) + 9,
                runner_source=bounded_reap_runner,
            )
            self.assertEqual(unreaped.returncode, 0, unreaped.stderr)
            self.assertEqual(
                unreaped.stdout,
                "candidate_status=none capture_state=cleanup_unreaped\n",
            )
            unreaped_metadata = json.loads((unreaped_dir / "candidate.json").read_text())
            self.assertTrue(unreaped_metadata["candidate_started"])
            self.assertIsNone(unreaped_metadata["candidate_exit_status"])
            self.assertFalse(unreaped_metadata["candidate_child_reaped"])
            self.assertTrue(unreaped_metadata["candidate_child_reap_failed"])
            self.assertEqual(unreaped_metadata["capture_state"], "cleanup_unreaped")
            self.assertTrue((unreaped_dir / "candidate.stdout").exists())
            self.assertTrue((unreaped_dir / "candidate.stderr").exists())
            self.assertTrue((unreaped_dir / "candidate.json").exists())
            unreaped_marker = self._candidate_diagnostic_marker_harness(
                supervisor,
                result=unreaped.stdout.rstrip("\n"),
                release_slug=unreaped_slug,
                source_sha=target_sha,
            )
            unreaped_output = io.StringIO()
            with redirect_stdout(unreaped_output):
                unreaped_status = platform_workflow_remote_dispatch._run_bounded_child(
                    ["/bin/bash", "--noprofile", "--norc", "-c", unreaped_marker],
                    timeout_seconds=5,
                    expected_release_marker=("deploy", unreaped_slug, target_sha),
                )
            self.assertEqual(unreaped_status, 1)
            self.assertNotIn("status=passed", unreaped_output.getvalue())
            self.assertIn("status=failed", unreaped_output.getvalue())

            inherited_candidate = root / "inherited-pipe-candidate"
            inherited_pid_file = root / "inherited-descendant.pid"
            self.addCleanup(cleanup_pid_file, inherited_pid_file)
            inherited_candidate.write_text(
                "#!/usr/bin/python3\n"
                "import os, time\n"
                "pid = os.fork()\n"
                "if pid == 0:\n"
                "    time.sleep(30)\n"
                "    os._exit(0)\n"
                f"open({str(inherited_pid_file)!r}, 'w').write(str(pid))\n"
                "os._exit(0)\n",
                encoding="ascii",
            )
            inherited_candidate.chmod(0o700)
            fast_pipe_runner = runner.replace(
                "pipe_eof_grace_seconds = 1.0",
                "pipe_eof_grace_seconds = 0.2",
                1,
            ).replace(
                "capture_timeout_seconds = 1800.0",
                "capture_timeout_seconds = 2.0",
                1,
            ).replace(
                "termination_grace_seconds = 2.0",
                "termination_grace_seconds = 0.2",
                1,
            )
            inherited, inherited_slug, inherited_dir = invoke(
                inherited_candidate,
                int(run_id) + 6,
                runner_source=fast_pipe_runner,
            )
            self.assertEqual(
                inherited.stdout,
                "candidate_status=0 capture_state=inherited_pipe_open\n",
            )
            inherited_metadata = json.loads((inherited_dir / "candidate.json").read_text())
            self.assertEqual(inherited_metadata["capture_state"], "inherited_pipe_open")
            assert_pid_stopped(inherited_pid_file)
            inherited_marker = self._candidate_diagnostic_marker_harness(
                supervisor,
                result=inherited.stdout.rstrip("\n"),
                release_slug=inherited_slug,
                source_sha=target_sha,
            )
            inherited_output = io.StringIO()
            with redirect_stdout(inherited_output):
                inherited_status = platform_workflow_remote_dispatch._run_bounded_child(
                    ["/bin/bash", "--noprofile", "--norc", "-c", inherited_marker],
                    timeout_seconds=5,
                    expected_release_marker=("deploy", inherited_slug, target_sha),
                )
            self.assertEqual(inherited_status, 1)
            self.assertEqual(
                inherited_output.getvalue(),
                "RELEASE_DEPLOY schema=1 status=failed class=deployment "
                "phase=candidate reason=activation_failed "
                f"release_slug={inherited_slug} source_sha={target_sha}\n",
            )

    def test_deploy_only_artifact_preparation_errors_emit_markers_without_app_writes(
        self,
    ) -> None:
        if os.geteuid() != 0:
            self.skipTest("immutable supervisor fixture requires root")

        failure_cases = (
            ("find_count", 17, "artifact_count_invalid"),
            ("find_path", 19, "artifact_missing"),
            ("mktemp", 21, "validation_failed"),
            ("chmod", 23, "validation_failed"),
        )
        with tempfile.TemporaryDirectory(prefix="deploy-artifact-errors-") as tmp:
            fixture_root = Path(tmp)
            supervisor, release_lock, retained_lock, generation_dir = (
                self._install_supervisor_fixture(fixture_root)
            )

            def cleanup_fixture() -> None:
                for path in (release_lock, retained_lock):
                    if path.exists() or path.is_symlink():
                        path.unlink()
                if generation_dir.exists() or generation_dir.is_symlink():
                    shutil.rmtree(generation_dir)

            self.addCleanup(cleanup_fixture)
            source = supervisor.read_text(encoding="utf-8")
            source = source.replace(
                "export PATH=/usr/sbin:/usr/bin:/sbin:/bin",
                'export PATH="$PLATFORM_TEST_PATH:/usr/sbin:/usr/bin:/sbin:/bin"',
                1,
            )
            source = source.replace(
                "runtime=/opt/oldsparky/platform\ncurrent=\"$runtime/current\"",
                'runtime="$PLATFORM_TEST_RUNTIME"\ncurrent="$runtime/current"',
                1,
            )
            self.assertIn('export PATH="$PLATFORM_TEST_PATH:', source)
            self.assertIn('runtime="$PLATFORM_TEST_RUNTIME"', source)
            supervisor.chmod(0o700)
            supervisor.write_text(source, encoding="utf-8")
            os.chown(supervisor, 0, 0)
            supervisor.chmod(0o555)

            runtime = fixture_root / "runtime"
            runtime.mkdir()
            runtime_sentinel = runtime / "unchanged-sentinel"
            runtime_sentinel.write_bytes(b"no application lifecycle write\n")
            preflight = generation_dir / "platform_release_preflight.sh"
            preflight.chmod(0o700)
            preflight.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            os.chown(preflight, 0, 0)
            preflight.chmod(0o555)

            fake_bin = fixture_root / "bin"
            fake_bin.mkdir()
            fake_find = fake_bin / "find"
            fake_find.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'PRIVATE_FIND_STDERR_SENTINEL' >&2\n"
                "case \"$PLATFORM_TEST_FAILURE:$*\" in\n"
                "  find_count:*) exit 17 ;;\n"
                "  find_path:*' -printf '*) printf 'x\\n'; exit 0 ;;\n"
                "  find_path:*) exit 19 ;;\n"
                "  *) exec /usr/bin/find \"$@\" ;;\n"
                "esac\n",
                encoding="ascii",
            )
            fake_mktemp = fake_bin / "mktemp"
            fake_mktemp.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'PRIVATE_MKTEMP_STDERR_SENTINEL' >&2\n"
                "if [ \"$PLATFORM_TEST_FAILURE\" = mktemp ]; then exit 21; fi\n"
                "result=$(/usr/bin/mktemp \"$@\") || exit $?\n"
                "printf '%s\\n' \"$result\" > \"$PLATFORM_TEST_BOOTSTRAP_RECORD\"\n"
                "printf '%s\\n' \"$result\"\n",
                encoding="ascii",
            )
            fake_chmod = fake_bin / "chmod"
            fake_chmod.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'PRIVATE_CHMOD_STDERR_SENTINEL' >&2\n"
                "if [ \"$PLATFORM_TEST_FAILURE\" = chmod ]; then exit 23; fi\n"
                "exec /usr/bin/chmod \"$@\"\n",
                encoding="ascii",
            )
            for command in (fake_find, fake_mktemp, fake_chmod):
                command.chmod(0o755)

            target_sha = "a" * 40
            run_id = str(os.getpid())
            artifact_dir = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-1")
            while artifact_dir.exists() or artifact_dir.is_symlink():
                run_id = str(int(run_id) + 1)
                artifact_dir = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-1")
            release_slug = f"gha-{run_id}-1-{target_sha[:12]}"

            def prepare_artifact() -> None:
                artifact_dir.mkdir(mode=0o700)
                os.chown(artifact_dir, 0, 0)
                os.chmod(artifact_dir, 0o700)
                marker = artifact_dir / ".old-sparky-platform-artifact-owner"
                marker.write_text(
                    "platform_prepare_artifact_dir schema=1 "
                    f"dev={artifact_dir.stat().st_dev} ino={artifact_dir.stat().st_ino}\n",
                    encoding="ascii",
                )
                os.chown(marker, 0, 0)
                os.chmod(marker, 0o600)
                artifact = artifact_dir / f"{release_slug}.tar.gz"
                artifact.write_bytes(b"not extracted before injected failure\n")
                os.chown(artifact, 0, 0)
                os.chmod(artifact, 0o600)
                digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
                checksum = artifact_dir / f"{release_slug}.tar.gz.sha256"
                checksum.write_text(f"{digest}  {artifact.name}\n", encoding="ascii")
                os.chown(checksum, 0, 0)
                os.chmod(checksum, 0o600)
                provenance = artifact_dir / "RELEASE.provenance.json"
                provenance.write_text("{}\n", encoding="ascii")
                os.chown(provenance, 0, 0)
                os.chmod(provenance, 0o600)

            def cleanup_artifact() -> None:
                if artifact_dir.is_dir() and not artifact_dir.is_symlink():
                    shutil.rmtree(artifact_dir)

            self.addCleanup(cleanup_artifact)
            bootstrap_record = fixture_root / "bootstrap-path"

            for failure, child_status, reason in failure_cases:
                with self.subTest(failure=failure):
                    prepare_artifact()
                    bootstrap_record.unlink(missing_ok=True)
                    command = [
                        str(supervisor),
                        target_sha,
                        release_slug,
                        "deploy",
                        str(artifact_dir),
                        "baseline",
                    ]
                    expected_marker = (
                        "RELEASE_DEPLOY schema=1 status=failed class=artifact "
                        f"phase=artifact reason={reason} release_slug={release_slug} "
                        f"source_sha={target_sha}\n"
                    )
                    captured = io.StringIO()
                    with patch.dict(
                        os.environ,
                        {
                            "PLATFORM_TEST_FAILURE": failure,
                            "PLATFORM_TEST_PATH": str(fake_bin),
                            "PLATFORM_TEST_RUNTIME": str(runtime),
                            "PLATFORM_TEST_BOOTSTRAP_RECORD": str(bootstrap_record),
                        },
                        clear=True,
                    ):
                        with redirect_stdout(captured):
                            status = platform_workflow_remote_dispatch._run_bounded_child(
                                command,
                                timeout_seconds=20,
                                expected_release_marker=(
                                    "deploy",
                                    release_slug,
                                    target_sha,
                                ),
                            )
                    self.assertEqual(status, child_status)
                    self.assertEqual(captured.getvalue(), expected_marker)
                    self.assertNotIn("PRIVATE_", captured.getvalue())
                    self.assertFalse(artifact_dir.exists())
                    self.assertEqual(
                        runtime_sentinel.read_bytes(),
                        b"no application lifecycle write\n",
                    )
                    self.assertEqual(
                        {p.name for p in runtime.iterdir()},
                        {runtime_sentinel.name},
                    )
                    if failure == "chmod":
                        bootstrap = Path(bootstrap_record.read_text().strip())
                        self.assertFalse(bootstrap.exists())
                    else:
                        self.assertFalse(bootstrap_record.exists())

    def test_retained_lock_supervisor_preserves_callback_failure_marker(self) -> None:
        """Nested flock callbacks preserve valid markers and fail closed."""

        with tempfile.TemporaryDirectory() as temporary:
            fixture_root = Path(temporary)
            supervisor, release_lock, retained_lock, generation_dir = (
                self._install_supervisor_fixture(fixture_root)
            )

            def cleanup_fixture() -> None:
                for path in (release_lock, retained_lock):
                    if path.exists() or path.is_symlink():
                        path.unlink()
                if generation_dir.exists() or generation_dir.is_symlink():
                    shutil.rmtree(generation_dir)

            self.addCleanup(cleanup_fixture)
            target_sha = "a" * 40
            run_id = str(os.getpid())
            artifact = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-1")
            while artifact.exists() or artifact.is_symlink():
                run_id = str(int(run_id) + 1)
                artifact = Path(f"/tmp/old-sparky-platform-artifact-{run_id}-1")
            release_slug = f"gha-{run_id}-1-{target_sha[:12]}"
            environment = {
                **os.environ,
                "PLATFORM_TEST_RUNTIME": str(fixture_root / "runtime"),
            }
            for variable in (
                "PLATFORM_RELEASE_LOCK_FD",
                "PLATFORM_RELEASE_LOCK_SUPERVISED",
                "PLATFORM_RETAINED_LOAD_LOCK_FD",
                "PLATFORM_RETAINED_LOAD_LOCK_SUPERVISED",
            ):
                environment.pop(variable, None)

            preflight_tool = generation_dir / "platform_release_preflight.sh"
            preflight_tool.chmod(0o755)
            preflight_tool.write_text("#!/bin/sh\nexit 17\n", encoding="utf-8")
            preflight_tool.chmod(0o555)

            def dispatch() -> tuple[int, str]:
                output = io.StringIO()
                with redirect_stdout(output):
                    status = platform_workflow_remote_dispatch._run_bounded_child(
                        [
                            str(supervisor),
                            target_sha,
                            release_slug,
                            "preflight",
                            str(artifact),
                            "baseline",
                        ],
                        timeout_seconds=20,
                        expected_release_marker=("preflight", release_slug, target_sha),
                    )
                return status, output.getvalue()

            expected_preflight_failure = (
                "RELEASE_DEPLOY schema=1 status=failed class=preflight "
                "phase=preflight reason=preflight_failed "
                f"release_slug={release_slug} source_sha={target_sha}\n"
            )
            with patch.dict(os.environ, environment, clear=True):
                preflight_status, preflight_output = dispatch()
            self.assertEqual(preflight_status, 1)
            self.assertEqual(preflight_output, expected_preflight_failure)
            self.assertNotIn("lock_stage=", preflight_output)

            supervisor_source = supervisor.read_text(encoding="utf-8")
            self.assertEqual(
                supervisor_source.count(
                    '|| fail "production preflight failed"'
                ),
                2,
            )

            original = supervisor.read_text(encoding="utf-8")

            def inject_after_both_locks(body: str) -> None:
                injected = original.replace(
                    "trap cleanup EXIT\n", f"trap cleanup EXIT\n{body}\n", 1
                )
                self.assertNotEqual(injected, original)
                supervisor.chmod(0o755)
                supervisor.write_text(injected, encoding="utf-8")
                supervisor.chmod(0o555)

            # An unhandled callback exit has no trusted marker. The dispatcher
            # must suppress child output while emitting its fixed diagnostic
            # and preserving the nonzero child status.
            inject_after_both_locks("exit 7")
            with patch.dict(os.environ, environment, clear=True):
                missing_status, missing_output = dispatch()
            self.assertEqual(missing_status, 7)
            self.assertEqual(
                missing_output,
                "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed "
                "reason=missing_marker child_exit=7 observed_bytes=0 "
                "dispatcher_exit=7\n",
            )

            # flock's reserved 73 can also be a callback status. The accepted
            # lock_stage identifies only the call boundary, not a proven holder.
            inject_after_both_locks("exit 73")
            with patch.dict(os.environ, environment, clear=True):
                ambiguous_status, ambiguous_output = dispatch()
            self.assertEqual(ambiguous_status, 1)
            self.assertIn("reason=lock lock_stage=retained_supervise", ambiguous_output)
            self.assertNotIn("held", ambiguous_output)

    def test_candidate_revision_guard_follows_operational_preflight_and_quiesce(self) -> None:
        preflight = (TOOLS_DIR / "platform_release_preflight.sh").read_text(
            encoding="utf-8"
        )
        defer_block = preflight.index(
            'if [[ "$DEFER_ACTIVE_ALEMBIC_REVISION_CHECK" -eq 0 ]]; then'
        )
        current_lookup = preflight.index('ALEMBIC_CURRENT="$(', defer_block)
        self.assertLess(
            preflight.index('DB_CHECK_OUTPUT="$('),
            defer_block,
            "operational DB readiness remains required before revision handling",
        )
        self.assertLess(defer_block, current_lookup)
        self.assertIn(
            '[[ "$ALEMBIC_CURRENT" == "$ALEMBIC_HEAD" ]] || fail',
            preflight[current_lookup:],
            "ordinary preflight must retain strict active-graph parity",
        )

        deploy = (TOOLS_DIR / "platform_release_deploy.sh").read_text(
            encoding="utf-8"
        )
        first_deferred = deploy.index(
            "run_release_preflight_quiet --defer-active-alembic-revision-check"
        )
        first_quiesce = deploy.index("quiesce_runtime_writers", first_deferred)
        stage = deploy.index('"$INSTALL_TOOL" --stage-only', first_quiesce)
        self.assertLess(first_deferred, first_quiesce)
        self.assertLess(first_quiesce, stage)
        second_deferred = deploy.index(
            "release_preflight --defer-active-alembic-revision-check", stage
        )
        self.assertLess(stage, second_deferred)
        post_activation_preflight = deploy.rfind("release_preflight")
        self.assertGreater(post_activation_preflight, second_deferred)
        self.assertNotIn(
            "--defer-active-alembic-revision-check",
            deploy[post_activation_preflight:],
            "post-activation preflight must check the active candidate graph",
        )

        supervisor = (TOOLS_DIR / "platform_production_deploy_supervisor.sh").read_text(
            encoding="utf-8"
        )
        mode_preflight_return = supervisor.index('if [[ "$deploy_mode" == "preflight" ]]')
        mode_deploy_check = supervisor.index('[[ "$deploy_mode" == "deploy" ]] || fail')
        first_host_preflight = supervisor.index('"$host_tools_dir/platform_release_preflight.sh"')
        defer_mode_check = supervisor.index('if [[ "$deploy_mode" == "deploy" ]]; then')
        second_host_preflight = supervisor.index(
            '"$host_tools_dir/platform_release_preflight.sh"',
            first_host_preflight + 1,
        )
        self.assertLess(defer_mode_check, first_host_preflight)
        self.assertLess(first_host_preflight, mode_preflight_return)
        self.assertLess(mode_preflight_return, mode_deploy_check)
        self.assertLess(mode_deploy_check, second_host_preflight)
        self.assertIn(
            'active_revision_preflight_flag=(--defer-active-alembic-revision-check)',
            supervisor,
        )
        self.assertIn(
            '"${active_revision_preflight_flag[@]}"',
            supervisor[ first_host_preflight : mode_preflight_return ],
        )
        self.assertIn(
            "--defer-active-alembic-revision-check",
            supervisor[second_host_preflight:],
        )

        alembic = (TOOLS_DIR / "platform_run_alembic.sh").read_text(
            encoding="utf-8"
        )
        candidate_transaction_check = alembic.index(
            'candidate_root="$(readlink -f -- "$PLATFORM_ROOT_DIR"'
        )
        deferred_preflight = alembic.index(
            "--defer-active-alembic-revision-check"
        )
        stop = alembic.index(
            "run_systemctl stop deadlock-api deadlock-worker deadlock-web"
        )
        guard = alembic.index("platform_release_migration_guard.py")
        recovery = alembic.index('"$PLATFORM_PYTHON_BIN" "$recovery_tool"', guard)
        migration = alembic.index('exec "$PLATFORM_PYTHON_BIN" -m alembic', recovery)
        self.assertLess(deferred_preflight, stop)
        self.assertLess(candidate_transaction_check, guard)
        self.assertLess(stop, guard)
        self.assertLess(guard, recovery)
        self.assertLess(recovery, migration)
        self.assertIn('migration_guard_args+=(--allow-empty-database)', alembic)

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
        self.assertIn("platform_release_phase_telemetry.py", workflow)
        self.assertIn('PLATFORM_RELEASE_PHASE_LOG=\"$marker_log\"', workflow)
        real = workflow_job(workflow, "release-runtime-real")
        self.assertIn("builder_started=0", real)
        self.assertIn("local builder_run_ready=0", real)
        self.assertIn(
            'if (( builder_started == 1 )) && [[ -n "$canonical_rc" ]]',
            real,
        )
        self.assertIn('--marker-log "$marker_log"', real)
        self.assertNotIn('diagnostic_parser --marker-log "$build_log"', real)
        self.assertNotIn("--" + "log", real)
        self.assertIn('/usr/bin/install -o root -g root -m 0600 /dev/null "$marker_log"', real)
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
        sanitizer_scripts = re.findall(
            r"<<'PY'\n(?P<script>.*?)\n\s*PY\n",
            real,
            re.DOTALL,
        )
        sanitizer = next(
            (script for script in sanitizer_scripts if "RELEASE_BUILD_DIAGNOSTIC" in script),
            None,
        )
        self.assertIsNotNone(sanitizer)
        sanitizer = textwrap.dedent(sanitizer)
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
        for name, source_sha, expected_rc in (
            ("source-64", "c" * 64, 0),
            ("source-39", "a" * 39, 1),
            ("source-41", "a" * 41, 1),
            ("source-63", "a" * 63, 1),
            ("source-65", "a" * 65, 1),
            ("source-uppercase", "A" * 40, 1),
        ):
            with self.subTest(source_sha=name):
                marker = passed_marker.replace(
                    "source_sha=" + "a" * 40,
                    "source_sha=" + source_sha,
                )
                sanitized = subprocess.run(
                    ["/usr/bin/python3", "-I", "-", marker, "0", "0"],
                    input=sanitizer,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(sanitized.returncode, expected_rc, sanitized.stderr)
                self.assertIn(
                    "phase=complete" if expected_rc == 0 else "phase=unknown",
                    sanitized.stdout,
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

        for reject_reason, expected_rc in (("sequence", 1), ("https://example.invalid", 1)):
            with self.subTest(reject_reason=reject_reason):
                parser_rejection = subprocess.run(
                    [
                        "/usr/bin/python3",
                        "-I",
                        "-",
                        "RELEASE_BUILD_DIAGNOSTIC schema=1 phase=unknown status=failed "
                        f"reason=build_failed cleanup=unknown reject_reason={reject_reason}",
                        "2",
                        "1",
                    ],
                    input=sanitizer,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(parser_rejection.returncode, expected_rc)
                self.assertIn("reject_reason=", parser_rejection.stdout)
                self.assertNotIn("https://example.invalid", parser_rejection.stdout)

        run_scripts = []
        for run_match in re.finditer(r"^        run: \|\n", real, re.MULTILINE):
            run_lines = []
            for line in real[run_match.end():].splitlines():
                if line and not line.startswith("          "):
                    break
                run_lines.append(line[10:] if line.startswith("          ") else "")
            run_scripts.append("\n".join(run_lines))
        run_script = next(
            (script for script in run_scripts if "cleanup() {" in script),
            None,
        )
        self.assertIsNotNone(run_script)
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
marker_log=""
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
                'build_log=""\nmarker_log=""\ncanonical_builder_rc=""\nbuilder_started=0',
                f'build_log={str(builder_log)!r}\n'
                f'marker_log={str(Path(builder_temporary) / "missing-phase.log")!r}\n'
                'canonical_builder_rc=0\nbuilder_started=1',
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
        self.assertIn("export PYTHONDONTWRITEBYTECODE=1", supervisor)
        self.assertIn(
            '"$QA_PYTHON" -B "$TOOLS_DIR/platform_prepare_external_vote_fixture.py"',
            supervisor,
        )
        self.assertIn('"$QA_PYTHON" -B "${observer_args[@]}"', supervisor)
        self.assertNotIn(
            '"$QA_PYTHON" "$TOOLS_DIR/platform_prepare_external_vote_fixture.py"',
            supervisor,
        )
        self.assertNotIn('"$QA_PYTHON" "${observer_args[@]}"', supervisor)
        self.assertIn(
            'platform_workflow_input_guard.py" \\\n  control-email-json-stdin)',
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
        self.assertIn(
            '[[ "$release_slug" =~ ^gha-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}-[0-9a-f]{12}$ ]]',
            deploy_supervisor,
        )
        self.assertNotIn('echo \'{"ok":true,"fixture_absent":true}\'', workflow)
        cleanup_supervisor = (
            REPO_ROOT
            / "platform/tools/platform_production_retained_load_cleanup_qa.sh"
        ).read_text()
        self.assertIn(
            'platform_workflow_input_guard.py" \\\n  control-email-json-stdin)',
            cleanup_supervisor,
        )
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

    def test_remote_deploy_marker_consumer_matches_closed_c4_shapes(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text(encoding="utf-8")
        activation = self._workflow_step_run(
            workflow, "Run production preflight or deployment"
        )
        preflight = self._workflow_step_run(workflow, "Run production preflight")
        self.assertIn("remote_diagnostic_pattern=", activation)
        self.assertNotIn("remote_diagnostic_pattern=", preflight)
        self.assertIn('stdout_bytes="$(wc -c < "$remote_log"', preflight)
        self.assertIn('stderr_bytes="$(wc -c < "$remote_error"', preflight)
        marker_patterns = []
        for step_name in ("Run production preflight", "Run production preflight or deployment"):
            body = self._workflow_step_run(workflow, step_name)
            patterns = re.findall(r"^\s*marker_pattern='([^']+)'$", body, re.MULTILINE)
            self.assertEqual(len(patterns), 1, step_name)
            marker_patterns.append(patterns[0])
        self.assertEqual(marker_patterns[0], marker_patterns[1])
        marker_pattern = marker_patterns[0]

        slug = "gha-123456789-1-aaaaaaaaaaaa"
        sha = "a" * 40
        accepted = (
            f"RELEASE_DEPLOY schema=1 status=failed class=preflight phase=preflight reason=baseline_changed release_slug={slug} source_sha={sha}",
            f"RELEASE_DEPLOY schema=1 status=failed class=preflight phase=preflight reason=lock lock_stage=release_open release_slug={slug} source_sha={sha}",
            f"RELEASE_DEPLOY schema=1 status=failed class=preflight phase=preflight reason=lock release_slug={slug} source_sha={sha}",
        )
        rejected = (
            f"RELEASE_DEPLOY schema=1 status=failed class=artifact phase=preflight reason=baseline_changed release_slug={slug} source_sha={sha}",
            f"RELEASE_DEPLOY schema=1 status=failed class=preflight phase=preflight reason=lock lock_stage=unknown release_slug={slug} source_sha={sha}",
            f"RELEASE_DEPLOY schema=1 status=failed class=preflight phase=preflight reason=lock lock_stage=release_open extra=x release_slug={slug} source_sha={sha}",
            f"RELEASE_DEPLOY schema=1 status=passed class=preflight phase=preflight reason=lock lock_stage=release_open release_slug={slug} source_sha={sha}",
        )
        for marker in accepted:
            with self.subTest(marker=marker):
                result = subprocess.run(
                    ["grep", "-E", marker_pattern],
                    input=marker + "\n",
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, marker + "\n")
        for marker in rejected:
            with self.subTest(marker=marker):
                result = subprocess.run(
                    ["grep", "-E", marker_pattern],
                    input=marker + "\n",
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")

        # Exercise the real extracted activation shell block. The stubs emit
        # private sentinels into the same capture files used by SSH; only the
        # fixed public fallback and bounded metadata may reach stdout.
        with tempfile.TemporaryDirectory(prefix="release-marker-contract-") as tmp:
            root = Path(tmp)
            runner_temp = root / "runner"
            fake_bin = root / "bin"
            (runner_temp / "deploy-input").mkdir(parents=True)
            fake_bin.mkdir()
            (runner_temp / "deploy-input/platform-production-deploy-input.json").write_text(
                "{}\n", encoding="utf-8"
            )
            curl = fake_bin / "curl"
            curl.write_text(
                "#!/bin/sh\nprintf '%s\\n' '{\"commit\":{\"sha\":\""
                + sha
                + "\"}}'\n",
                encoding="utf-8",
            )
            timeout = fake_bin / "timeout"
            timeout.write_text("#!/bin/sh\nshift 2\nexec \"$@\"\n", encoding="utf-8")
            ssh = fake_bin / "ssh"
            ssh.write_text(
                "#!/bin/sh\ncat \"$TEST_REMOTE_STDOUT\"\n"
                "cat \"$TEST_REMOTE_STDERR\" >&2\n"
                "exit \"$TEST_REMOTE_STATUS\"\n",
                encoding="utf-8",
            )
            for script in (curl, timeout, ssh):
                script.chmod(0o755)

            private_stdout = root / "remote.stdout"
            private_stderr = root / "remote.stderr"
            private_stdout.write_text("private stdout payload\n", encoding="utf-8")
            private_stderr.write_text("private stderr payload\n", encoding="utf-8")
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "RUNNER_TEMP": str(runner_temp),
                "TARGET_SHA": sha,
                "GITHUB_API_URL": "https://api.github.com",
                "GITHUB_REPOSITORY": "example/oldsparky",
                "GH_TOKEN": "test-token-not-used-by-stub",
                "SSH_DIR": str(root / "ssh-config"),
                "HOST_TOOLS_DISPATCHER": "/unused/host-tools-dispatcher.py",
                "PROD_SSH_HOST": "production.invalid",
                "PROD_SSH_USER": "deploy",
                "TEST_REMOTE_STDOUT": str(private_stdout),
                "TEST_REMOTE_STDERR": str(private_stderr),
                "TEST_REMOTE_STATUS": "255",
            }
            fallback = subprocess.run(
                ["/bin/bash", "-c", activation],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(fallback.returncode, 255, fallback.stderr)
            self.assertEqual(
                fallback.stdout,
                "RELEASE_DEPLOY schema=1 status=failed class=remote_or_transport "
                "release_slug=unavailable source_sha=unavailable\n"
                "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed "
                "reason=ssh_or_remote_255 remote_exit=255 stdout_bytes=23 stderr_bytes=23\n",
            )
            self.assertNotIn("private", fallback.stdout + fallback.stderr)

            def run_outer_consumer(
                marker: str,
                *,
                status: int,
                remote_stderr: str = "private stderr sentinel\n",
            ) -> subprocess.CompletedProcess[str]:
                private_stdout.write_text(marker + "\n", encoding="utf-8")
                private_stderr.write_text(remote_stderr, encoding="utf-8")
                run_environment = {
                    **environment,
                    "TEST_REMOTE_STATUS": str(status),
                }
                return subprocess.run(
                    ["/bin/bash", "-c", activation],
                    env=run_environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )

            # Drive every closed failure shape from the dispatcher's actual
            # authoritative table through the extracted production shell
            # consumer, rather than maintaining a test-only reason allowlist.
            failure_markers = []
            for (marker_class, phase), reasons in (
                platform_workflow_remote_dispatch.RELEASE_FAILURE_REASONS.items()
            ):
                for reason in sorted(reasons):
                    failure_markers.append(
                        "RELEASE_DEPLOY schema=1 status=failed "
                        f"class={marker_class} phase={phase} reason={reason} "
                        f"release_slug={slug} source_sha={sha}"
                    )
            failure_markers.extend(
                "RELEASE_DEPLOY schema=1 status=failed class=preflight "
                f"phase=preflight reason=lock lock_stage={stage} "
                f"release_slug={slug} source_sha={sha}"
                for stage in (
                    "helper_metadata",
                    "release_supervise",
                    "release_open",
                    "retained_supervise",
                    "retained_open",
                )
            )
            for marker in failure_markers:
                with self.subTest(marker=marker):
                    result = run_outer_consumer(marker, status=1)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stdout, marker + "\n")
                    self.assertNotIn("private", result.stdout + result.stderr)

            for marker_class in ("preflight", "deployment"):
                passed_marker = (
                    "RELEASE_DEPLOY schema=1 status=passed "
                    f"class={marker_class} release_slug={slug} source_sha={sha}"
                )
                with self.subTest(marker=passed_marker):
                    result = run_outer_consumer(passed_marker, status=0)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, passed_marker + "\n")
                    self.assertNotIn("private", result.stdout + result.stderr)

            diagnostic_cases = (
                ("missing_marker", 7, 0, 7, 7),
                ("invalid_marker", 1, 1, 1, 1),
                ("invalid_marker", 1, 31, 1, 1),
                ("invalid_marker", 1, 512, 1, 1),
                ("oversized_marker", 5, 513, 5, 5),
                ("invalid_marker", 0, 1, 2, 2),
                ("invalid_marker", -9, 1, 247, 247),
            )
            for reason, child_exit, observed_bytes, dispatcher_exit, remote_status in diagnostic_cases:
                diagnostic = (
                    "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed "
                    f"reason={reason} child_exit={child_exit} "
                    f"observed_bytes={observed_bytes} "
                    f"dispatcher_exit={dispatcher_exit}"
                )
                with self.subTest(remote_diagnostic=diagnostic):
                    result = run_outer_consumer(
                        diagnostic, status=remote_status, remote_stderr=""
                    )
                    self.assertEqual(result.returncode, remote_status, result.stderr)
                    self.assertEqual(result.stdout, diagnostic + "\n")
                    self.assertEqual(result.stderr, "")

            rejected_diagnostics = (
                ("invalid_marker", 1, 0, 1, 1),
                ("invalid_marker", 1, 513, 1, 1),
                ("oversized_marker", 5, 512, 5, 5),
                ("oversized_marker", 5, 514, 5, 5),
                ("missing_marker", 7, 1, 7, 7),
                ("unknown_reason", 7, 1, 7, 7),
                ("invalid_marker", 1, 1, 2, 1),
                ("invalid_marker", 1, 1, 1, 0),
            )
            for reason, child_exit, observed_bytes, dispatcher_exit, remote_status in rejected_diagnostics:
                diagnostic = (
                    "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed "
                    f"reason={reason} child_exit={child_exit} "
                    f"observed_bytes={observed_bytes} "
                    f"dispatcher_exit={dispatcher_exit}"
                )
                with self.subTest(rejected_remote_diagnostic=diagnostic):
                    result = run_outer_consumer(
                        diagnostic, status=remote_status, remote_stderr=""
                    )
                    self.assertEqual(result.returncode, remote_status or 1)
                    self.assertIn(
                        "RELEASE_DEPLOY schema=1 status=failed class=remote_or_transport",
                        result.stdout,
                    )
                    self.assertNotIn(diagnostic, result.stdout)
                    self.assertNotIn("private", result.stdout + result.stderr)

            for malformed_stdout, remote_stderr in (
                (
                    "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed reason=missing_marker "
                    "child_exit=7 observed_bytes=0 dispatcher_exit=7\nextra line",
                    "",
                ),
                (
                    "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed reason=missing_marker "
                    "child_exit=7 observed_bytes=0 dispatcher_exit=7",
                    "private stderr sentinel\n",
                ),
            ):
                with self.subTest(remote_diagnostic_extra=malformed_stdout):
                    result = run_outer_consumer(
                        malformed_stdout,
                        status=7,
                        remote_stderr=remote_stderr,
                    )
                    self.assertEqual(result.returncode, 7)
                    self.assertIn("class=remote_or_transport", result.stdout)
                    self.assertNotIn("reason=missing_marker child_exit=7", result.stdout)
                    self.assertNotIn("private", result.stdout + result.stderr)

            malformed_markers = (
                f"RELEASE_DEPLOY schema=1 status=failed class=artifact phase=preflight reason=baseline_changed release_slug={slug} source_sha={sha}",
                f"RELEASE_DEPLOY schema=1 status=failed class=preflight phase=candidate reason=baseline_changed release_slug={slug} source_sha={sha}",
                f"RELEASE_DEPLOY schema=1 status=failed class=preflight phase=preflight reason=lock lock_stage=unknown release_slug={slug} source_sha={sha}",
                f"RELEASE_DEPLOY schema=1 status=failed class=preflight phase=preflight reason=lock lock_stage=release_open lock_stage=retained_open release_slug={slug} source_sha={sha}",
                f"RELEASE_DEPLOY schema=1 status=failed class=preflight phase=preflight reason=lock lock_stage=release_open extra=x release_slug={slug} source_sha={sha}",
                f"RELEASE_DEPLOY schema=1 status=failed class=preflight phase=preflight reason=lock lock_stage=release_open release_slug=bad/slug source_sha={sha}",
                f"RELEASE_DEPLOY schema=1 status=failed class=preflight phase=preflight reason=lock lock_stage=release_open release_slug={slug} source_sha=bad",
            )
            for marker in malformed_markers:
                with self.subTest(malformed_marker=marker):
                    result = run_outer_consumer(marker, status=1)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(
                        "RELEASE_DEPLOY schema=1 status=failed class=remote_or_transport",
                        result.stdout,
                    )
                    self.assertIn(
                        "reason=unrecognized_stdout remote_exit=1",
                        result.stdout,
                    )
                    self.assertNotIn(marker, result.stdout)
                    self.assertNotIn("private", result.stdout + result.stderr)

    def test_preflight_remote_empty_output_fallback_keeps_closed_diagnostics(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text(encoding="utf-8")
        preflight = self._workflow_step_run(workflow, "Run production preflight")

        with tempfile.TemporaryDirectory(prefix="preflight-empty-output-") as tmp:
            root = Path(tmp)
            runner_temp = root / "runner"
            fake_bin = root / "bin"
            deploy_input = runner_temp / "deploy-input"
            deploy_input.mkdir(parents=True)
            fake_bin.mkdir()
            (deploy_input / "platform-production-deploy-input.json").write_text(
                "{}\n", encoding="utf-8"
            )
            timeout = fake_bin / "timeout"
            timeout.write_text("#!/bin/sh\nshift 2\nexec \"$@\"\n", encoding="ascii")
            ssh = fake_bin / "ssh"
            ssh.write_text("#!/bin/sh\nexit 7\n", encoding="ascii")
            timeout.chmod(0o755)
            ssh.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "RUNNER_TEMP": str(runner_temp),
                "PREFLIGHT_SSH_DIR": str(root / "ssh-config"),
                "HOST_TOOLS_DISPATCHER": "/unused/host-tools-dispatcher.py",
                "PROD_SSH_HOST": "production.invalid",
                "PROD_SSH_USER": "deploy",
            }
            result = subprocess.run(
                ["/bin/bash", "-c", preflight],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(
            result.stdout,
            "RELEASE_DEPLOY schema=1 status=failed class=remote_or_transport "
            "release_slug=unavailable source_sha=unavailable\n"
            "RELEASE_REMOTE_DIAGNOSTIC schema=1 status=failed reason=empty_output "
            "remote_exit=7 stdout_bytes=0 stderr_bytes=0\n",
        )
        self.assertNotIn("unbound variable", result.stderr)

    def test_production_host_tools_handoff_allows_nonroot_runner_owner(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text(encoding="utf-8")
        handoff = self._workflow_step_run(
            workflow, "Create closed host-tools handoff after final verification"
        )
        owner_check_lines = [
            line.strip()
            for line in handoff.splitlines()
            if line.strip().startswith("expected_owner=")
            or line.strip().startswith('test "$(stat -c')
        ]
        self.assertEqual(
            owner_check_lines,
            [
                'expected_owner="$(id -u)"',
                'test "$(stat -c \'%F:%u:%h:%a\' -- "$handoff")" = '
                '"regular file:${expected_owner}:1:600"',
            ],
        )
        owner_check = "\n".join(owner_check_lines)

        with tempfile.TemporaryDirectory(prefix="host-handoff-owner-") as temporary:
            root = Path(temporary)
            root.chmod(0o711)
            handoff_dir = root / "runner-owned"
            handoff_dir.mkdir()
            drop_privileges = None
            if os.geteuid() == 0:
                import pwd

                unprivileged = pwd.getpwnam("nobody")

                def drop_privileges() -> None:
                    os.setgroups([])
                    os.setgid(unprivileged.pw_gid)
                    os.setuid(unprivileged.pw_uid)

                expected_uid = unprivileged.pw_uid
                os.chown(handoff_dir, unprivileged.pw_uid, unprivileged.pw_gid)
            else:
                expected_uid = os.geteuid()
            handoff_dir.chmod(0o700)
            handoff_path = handoff_dir / "handoff.json"

            unprivileged_setup = (
                "if os.geteuid() == 0:\n"
                "    import pwd\n"
                "    user = pwd.getpwnam('nobody')\n"
                "    os.setgroups([])\n"
                "    os.setgid(user.pw_gid)\n"
                "    os.setuid(user.pw_uid)\n"
            )
            writer = (
                "import os\nimport sys\n"
                f"sys.path.insert(0, {str(REPO_ROOT / 'platform')!r})\n"
                "from tools.platform_workflow_input_guard import main\n"
                + unprivileged_setup
                + "raise SystemExit(main(['host-tools', '--output', sys.argv[1], "
                "'--target-sha', 'a' * 40, '--host-tools-sha', 'b' * 40, "
                "'--artifact-id', '123456', '--artifact-name', "
                "'platform-host-tools-bundle-123456-2', '--artifact-size', '4096', "
                "'--artifact-digest', 'c' * 64, '--bundle-sha256', 'd' * 64, "
                "'--manifest-sha256', 'e' * 64, '--capabilities-sha256', 'f' * 64, "
                "'--files-contract-sha256', '0' * 64, '--modes-contract-sha256', "
                "'1' * 64, '--signer-workflow', "
                "'StrayForest/old_sparky/.github/workflows/platform-production-deploy.yml', "
                "'--source-ref', 'refs/heads/dev', '--source-digest', 'a' * 40, "
                "'--attestation-run-id', '123456', '--attestation-run-attempt', '2', "
                "'--attestation-job-id', '654321']))"
            )
            write_result = subprocess.run(
                ["/usr/bin/python3", "-c", writer, str(handoff_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(write_result.returncode, 0, write_result.stderr)
            metadata = handoff_path.lstat()
            self.assertEqual(metadata.st_uid, expected_uid)
            self.assertEqual(metadata.st_mode & 0o777, 0o600)
            self.assertEqual(metadata.st_nlink, 1)

            roundtrip = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-c",
                    "import os\nimport sys\n"
                    f"sys.path.insert(0, {str(REPO_ROOT / 'platform')!r})\n"
                    "from tools.platform_workflow_input_guard import main\n"
                    + unprivileged_setup
                    + "raise SystemExit(main(['host-tools', '--input', sys.argv[1]]))",
                    str(handoff_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(roundtrip.returncode, 0, roundtrip.stderr)

            def check_handoff(path: Path) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["bash", "-c", owner_check],
                    env={**os.environ, "handoff": str(path)},
                    capture_output=True,
                    text=True,
                    preexec_fn=drop_privileges,
                    check=False,
                )

            old_root_check = subprocess.run(
                [
                    "bash",
                    "-c",
                    'test "$(stat -c \'%F:%u:%h:%a\' -- "$handoff")" = '
                    '"regular file:0:1:600"',
                ],
                env={**os.environ, "handoff": str(handoff_path)},
                capture_output=True,
                text=True,
                preexec_fn=drop_privileges,
                check=False,
            )
            self.assertNotEqual(old_root_check.returncode, 0)
            self.assertEqual(check_handoff(handoff_path).returncode, 0)

            handoff_path.chmod(0o640)
            self.assertNotEqual(check_handoff(handoff_path).returncode, 0)
            handoff_path.chmod(0o600)
            hard_link = handoff_dir / "handoff-hard-link.json"
            os.link(handoff_path, hard_link)
            self.assertNotEqual(check_handoff(handoff_path).returncode, 0)
            hard_link.unlink()
            symlink = handoff_dir / "handoff-symlink.json"
            symlink.symlink_to(handoff_path)
            self.assertNotEqual(check_handoff(symlink).returncode, 0)

            if os.geteuid() == 0:
                root_owned = root / "root-owned.json"
                root_owned.write_text("{}\n", encoding="ascii")
                root_owned.chmod(0o600)
                self.assertNotEqual(check_handoff(root_owned).returncode, 0)

    def test_host_capability_probe_matches_pinned_dispatcher_contract(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text(encoding="utf-8")
        dispatcher = (
            TOOLS_DIR / "platform_workflow_remote_dispatch.py"
        ).read_text(encoding="utf-8")
        probe = self._workflow_step_run(
            workflow, "Probe immutable host dispatcher capabilities"
        )
        expected_match = re.search(r'expected_output="([^"]+)"', probe)
        dispatcher_match = re.search(r'"dispatcher=[^"]+"', dispatcher)
        self.assertIsNotNone(expected_match)
        self.assertIsNotNone(dispatcher_match)
        assert expected_match is not None
        assert dispatcher_match is not None
        self.assertIn(dispatcher_match.group(0)[1:-1], expected_match.group(1))
        self.assertIn("release_baseline=1", expected_match.group(1))

    def test_host_tools_handoff_digest_matches_action_and_api_formats(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-deploy.yml"
        ).read_text(encoding="utf-8")
        deploy = self._workflow_step_run(
            workflow, "Revalidate host-tools contract before production side effects"
        )
        self.assertIn(
            "host_tools_handoff_artifact_digest: ${{ steps.publish-host-tools-handoff.outputs.artifact-digest }}",
            workflow,
        )
        action_digest_check = re.search(
            r'^\[\[ "\$HOST_TOOLS_HANDOFF_ARTIFACT_DIGEST" =~ (?P<pattern>.+?) \]\]$',
            deploy,
            re.MULTILINE,
        )
        self.assertIsNotNone(action_digest_check)
        self.assertEqual(action_digest_check.group("pattern"), r"^[0-9a-f]{64}$")

        with tempfile.TemporaryDirectory(prefix="host-tools-action-digest-") as temporary:
            handoff_path = Path(temporary) / "handoff.json"
            from tools.platform_workflow_input_guard import main as input_guard_main

            artifact_id = "123456"
            run_id = artifact_id
            attempt = "2"
            target_sha = "a" * 40
            raw_digest = "c" * 64
            self.assertEqual(
                input_guard_main(
                    [
                        "host-tools",
                        "--output",
                        str(handoff_path),
                        "--target-sha",
                        target_sha,
                        "--host-tools-sha",
                        "b" * 40,
                        "--artifact-id",
                        artifact_id,
                        "--artifact-name",
                        f"platform-host-tools-bundle-{run_id}-{attempt}",
                        "--artifact-size",
                        "4096",
                        "--artifact-digest",
                        raw_digest,
                        "--bundle-sha256",
                        "d" * 64,
                        "--manifest-sha256",
                        "e" * 64,
                        "--capabilities-sha256",
                        "f" * 64,
                        "--files-contract-sha256",
                        "0" * 64,
                        "--modes-contract-sha256",
                        "1" * 64,
                        "--signer-workflow",
                        "StrayForest/old_sparky/.github/workflows/platform-production-deploy.yml",
                        "--source-ref",
                        "refs/heads/dev",
                        "--source-digest",
                        target_sha,
                        "--attestation-run-id",
                        run_id,
                        "--attestation-run-attempt",
                        attempt,
                        "--attestation-job-id",
                        "654321",
                    ]
                ),
                0,
            )
            self.assertEqual(json.loads(handoff_path.read_text())["artifact_digest"], raw_digest)

            shell_check = (
                '[[ "$HOST_TOOLS_HANDOFF_ARTIFACT_DIGEST" =~ '
                f'{action_digest_check.group("pattern")} ]]'
            )
            accepted_action_digest = subprocess.run(
                ["bash", "-c", shell_check],
                env={**os.environ, "HOST_TOOLS_HANDOFF_ARTIFACT_DIGEST": raw_digest},
                check=False,
            )
            self.assertEqual(accepted_action_digest.returncode, 0)
            prefixed_action_digest = subprocess.run(
                ["bash", "-c", shell_check],
                env={
                    **os.environ,
                    "HOST_TOOLS_HANDOFF_ARTIFACT_DIGEST": f"sha256:{raw_digest}",
                },
                check=False,
            )
            self.assertNotEqual(prefixed_action_digest.returncode, 0)

            parser_match = re.search(
                r'/usr/bin/python3 - "\$handoff_metadata".*?<<\'PY\'\n(?P<script>.*?)\nPY\n',
                deploy,
                re.DOTALL,
            )
            self.assertIsNotNone(parser_match)
            parser = textwrap.dedent(parser_match.group("script"))
            metadata_path = Path(temporary) / "api-artifact.json"
            metadata = {
                "id": int(artifact_id),
                "name": f"platform-host-tools-handoff-{run_id}-{attempt}",
                "expired": False,
                "digest": f"sha256:{raw_digest}",
                "workflow_run": {
                    "id": int(run_id),
                    "run_attempt": int(attempt),
                    "head_sha": target_sha,
                    "head_branch": "dev",
                },
            }

            def validate_api_metadata() -> subprocess.CompletedProcess[str]:
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                return subprocess.run(
                    [
                        "/usr/bin/python3",
                        "-c",
                        parser,
                        str(metadata_path),
                        artifact_id,
                        raw_digest,
                        run_id,
                        attempt,
                        target_sha,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )

            accepted_api_digest = validate_api_metadata()
            self.assertEqual(accepted_api_digest.returncode, 0, accepted_api_digest.stderr)
            metadata["digest"] = "sha256:" + "9" * 64
            rejected_api_digest = validate_api_metadata()
            self.assertNotEqual(rejected_api_digest.returncode, 0)
            metadata["digest"] = raw_digest
            rejected_unprefixed_api_digest = validate_api_metadata()
            self.assertNotEqual(rejected_unprefixed_api_digest.returncode, 0)

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

    def test_dispatch_release_slug_is_exact_and_source_bound(self) -> None:
        script = BUILD_SCRIPT.read_text()
        self.assertIn("--release-slug", script)
        self.assertIn("RELEASE_SLUG_OVERRIDE", script)
        self.assertIn(
            '"$RELEASE_SLUG_OVERRIDE" != *"-${SOURCE_GIT_COMMIT:0:12}"',
            script,
        )
        unsafe_slugs = (
            "gha-123456-2",
            "gha-123456-2-aaaaaaaaaaa",
            "gha-123456-2-AAAAAAAAAAAA",
            "gha-0-2-aaaaaaaaaaaa",
            "gha-123456-0-aaaaaaaaaaaa",
            "gha-123456-2-aaaaaaaaaaaa/escape",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            for release_slug in unsafe_slugs:
                with self.subTest(release_slug=release_slug):
                    result = subprocess.run(
                        [str(BUILD_SCRIPT), "--release-slug", release_slug],
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
                    self.assertIn("Release slug", result.stderr)
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
        self.assertIn(
            'run_isolated_python "$venv_dir/bin/python" -I -B -m pip check',
            install,
        )
        self.assertIn("/usr/bin/env -i", install)
        self.assertIn("PIP_CONFIG_FILE=/dev/null", install)
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

    def test_deployment_handoff_accepts_only_closed_baseline_tuple(self) -> None:
        from tools.platform_workflow_input_guard import WorkflowInputError
        from tools.platform_workflow_input_guard import validate_deployment_payload

        target_sha = "a" * 40
        host_tools = {
            "schema": "1",
            "target_sha": target_sha,
            "host_tools_sha": "b" * 40,
            "artifact_id": "123456",
            "artifact_name": "platform-host-tools-bundle-123456-2",
            "artifact_size": "4096",
            "artifact_digest": "c" * 64,
            "bundle_sha256": "d" * 64,
            "manifest_sha256": "e" * 64,
            "capabilities_sha256": "f" * 64,
            "files_contract_sha256": "0" * 64,
            "modes_contract_sha256": "1" * 64,
            "signer_workflow": "StrayForest/old_sparky/.github/workflows/platform-production-deploy.yml",
            "source_ref": "refs/heads/dev",
            "source_digest": target_sha,
            "attestation_run_id": "123456",
            "attestation_run_attempt": "2",
            "attestation_job_id": "123457",
        }
        baseline = {
            "schema": 1,
            "source_sha": "87547df2abd4aa06a07f4dd4b4f730e9912707e1",
            "release_slug": "gha-35511236041-1-87547df2abd4",
            "release_json_sha256": "2" * 64,
            "current_link_dev": 7,
            "current_link_ino": 8,
            "release_dev": 7,
            "release_ino": 9,
            "pending_operation": False,
        }
        payload = {
            "schema": 3,
            "mode": "deploy",
            "runtime_profile": "baseline",
            "release_slug": "gha-37120000000-1-aaaaaaaaaaaa",
            "target_sha": target_sha,
            "artifact_remote_dir": "/tmp/old-sparky-platform-artifact-37120000000-1",
            "classifier_run_id": "37120000000",
            "classifier_run_attempt": "1",
            "web_compression": "enabled",
            "host_tools": host_tools,
            "baseline_identity": baseline,
        }
        validated = validate_deployment_payload(payload)
        self.assertEqual(validated["schema"], "3")
        self.assertEqual(validated["baseline_identity"], baseline)

        for field, invalid_value in (
            ("pending_operation", True),
            ("source_sha", "not-a-source-sha"),
            ("current_link_ino", True),
        ):
            with self.subTest(field=field):
                invalid = dict(payload)
                invalid["baseline_identity"] = {**baseline, field: invalid_value}
                with self.assertRaises(WorkflowInputError):
                    validate_deployment_payload(invalid)
        extra = dict(payload)
        extra["baseline_identity"] = {**baseline, "caller_claim": "ignored"}
        with self.assertRaises(WorkflowInputError):
            validate_deployment_payload(extra)
        preflight = dict(payload)
        preflight["mode"] = "preflight"
        with self.assertRaises(WorkflowInputError):
            validate_deployment_payload(preflight)

    def test_supervisor_rechecks_active_baseline_under_both_release_locks(self) -> None:
        supervisor = DEPLOY_SUPERVISOR.read_text(encoding="utf-8")
        retained_lock = supervisor.index("platform_retained_load_lock_open \\")
        baseline_query = supervisor.index("host-release-baseline-match")
        candidate_artifact = supervisor.index('find "$artifact_dir" -maxdepth 1')
        self.assertLess(retained_lock, baseline_query)
        self.assertLess(baseline_query, candidate_artifact)
        self.assertIn("platform_release_lock_open ||", supervisor)
        self.assertIn("platform_retained_load_lock_open \\\n  || fail", supervisor)
        self.assertIn("baseline_identity_b64", supervisor)

    def test_baseline_reconcile_build_and_deploy_require_cumulative_authorization(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        baseline = workflow_job(workflow, "validate-active-baseline")
        dispatch = workflow_job(workflow, "dispatch-baseline-runtime")
        proof = workflow_job(workflow, "validate-baseline-runtime-proof")
        build = workflow_job(workflow, "build-release")
        production = workflow_job(workflow, "production")

        self.assertIn("name: Dispatch exact-target baseline runtime proof", dispatch)
        self.assertIn("actions: write", dispatch)
        self.assertIn("needs.validate-active-baseline.outputs.runtime_required == 'true'", dispatch)
        self.assertIn("platform-security.yml/dispatches", dispatch)
        self.assertIn("validate_security_marker(", baseline)
        self.assertIn("validate_autodeploy_dispatch(", baseline)
        self.assertIn("classify_cumulative_baseline(", baseline)
        self.assertIn("validate_cumulative_reconcile_route(cumulative_result)", baseline)
        self.assertIn('runtime_required = reconcile_route["runtime_required"]', baseline)
        self.assertNotIn(
            "recovery target is not a runtime-sensitive cumulative no-op route",
            baseline,
        )
        self.assertIn("platform_baseline_runtime_proof.py", proof)
        self.assertIn("needs.dispatch-baseline-runtime.result == 'success'", proof)
        self.assertIn("needs.validate-baseline-runtime-proof", build)
        self.assertIn("needs.validate-active-baseline.result == 'success'", build)
        self.assertIn("needs.validate-active-baseline.outputs.cumulative_no_op == 'false'", build)
        self.assertIn("needs.validate-baseline-runtime-proof.result == 'success'", build)
        self.assertIn("needs.validate-baseline-runtime-proof", production)
        self.assertIn("needs.validate-active-baseline.result == 'success'", production)
        self.assertIn("needs.validate-active-baseline.outputs.cumulative_no_op == 'false'", production)
        self.assertIn("needs.validate-baseline-runtime-proof.result == 'success'", production)
        self.assertIn("inputs.mode == 'deploy'", production)
        dispatch_validator = workflow_job(workflow, "validate-dispatch")
        self.assertIn("DEPLOY_MODE: ${{ inputs.mode }}", dispatch_validator)
        self.assertIn(
            '[[ "$handoff_mode" != "baseline-reconcile" && "$handoff_mode" != "recovery-deploy" ]] || handoff_mode=deploy',
            dispatch_validator,
        )
        self.assertIn("DEPLOY_MODE: deploy", build)
        self.assertNotIn("DEPLOY_MODE: ${{ inputs.mode }}", build)


if __name__ == "__main__":
    unittest.main()
