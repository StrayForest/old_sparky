from __future__ import annotations

import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools import platform_release_build_diagnostics as diagnostics


REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "platform/tools/platform_build_release.sh"


class PlatformReleaseBuildDiagnosticsTests(unittest.TestCase):
    @staticmethod
    def _write_log(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
        path.write_bytes(payload)
        os.chown(path, 0, 0)
        os.chmod(path, mode)

    @staticmethod
    def _run_parser(path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                str(REPO_ROOT / "platform/tools/platform_release_build_diagnostics.py"),
                "--log",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    @staticmethod
    def _run_parser_as_nobody(
        parser: Path,
        path: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "/usr/bin/setpriv",
                "--reuid=65534",
                "--regid=65534",
                "--clear-groups",
                "/usr/bin/python3",
                "-I",
                str(parser),
                "--log",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    @staticmethod
    def _success_markers() -> bytes:
        phases = (
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
        )
        lines = [
            f"RELEASE_BUILD_PHASE schema=1 phase={phase} status=passed reason=ok cleanup=not-run"
            for phase in phases
        ]
        lines.extend(
            (
                "RELEASE_BUILD_PHASE schema=1 phase=cleanup status=passed reason=ok cleanup=passed",
                "RELEASE_BUILD_PHASE schema=1 phase=complete status=passed reason=ok cleanup=passed "
                f"source_sha={'a' * 40} artifact_sha256={'b' * 64}",
            )
        )
        return ("\n".join(lines) + "\n").encode("ascii")

    def test_parser_extracts_success_and_failure_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            success = root / "success.log"
            self._write_log(success, self._success_markers())
            parsed_success = self._run_parser(success)
            self.assertEqual(parsed_success.returncode, 0, parsed_success.stderr)
            self.assertEqual(
                parsed_success.stdout.strip(),
                "RELEASE_BUILD_DIAGNOSTIC schema=1 phase=complete status=passed "
                "reason=ok cleanup=passed "
                f"source_sha={'a' * 40} artifact_sha256={'b' * 64}",
            )

            failure = root / "failure.log"
            self._write_log(
                failure,
                (
                    "RELEASE_BUILD_PHASE schema=1 phase=canonical-preflight "
                    "status=passed reason=ok cleanup=not-run\n"
                    "RELEASE_BUILD_PHASE schema=1 phase=cleanup status=passed "
                    "reason=ok cleanup=passed\n"
                    "RELEASE_BUILD_PHASE schema=1 phase=complete status=failed "
                    "reason=build_failed cleanup=passed failed_phase=canonical-preflight\n"
                ).encode("ascii"),
            )
            parsed_failure = self._run_parser(failure)
            self.assertEqual(parsed_failure.returncode, 0, parsed_failure.stderr)
            self.assertEqual(
                parsed_failure.stdout.strip(),
                "RELEASE_BUILD_DIAGNOSTIC schema=1 phase=complete status=failed "
                "reason=build_failed cleanup=passed failed_phase=canonical-preflight",
            )

    def test_parser_rejects_unsafe_metadata_and_marker_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = self._success_markers()
            cases: dict[str, tuple[str, bytes]] = {
                "control": ("control.log", valid + b"\x01\n"),
                "duplicate-token": (
                    "duplicate-token.log",
                    b"RELEASE_BUILD_PHASE schema=1 phase=canonical-preflight "
                    b"phase=node-runtime status=passed reason=ok cleanup=not-run\n",
                ),
                "unknown-url": (
                    "unknown-url.log",
                    b"RELEASE_BUILD_PHASE schema=1 phase=canonical-preflight "
                    b"status=passed reason=ok cleanup=not-run url=https://example.test/x\n",
                ),
                "unknown-path": (
                    "unknown-path.log",
                    b"RELEASE_BUILD_PHASE schema=1 phase=canonical-preflight "
                    b"status=passed reason=ok cleanup=not-run path=/tmp/private\n",
                ),
                "unknown-reason": (
                    "unknown-reason.log",
                    b"RELEASE_BUILD_PHASE schema=1 phase=canonical-preflight "
                    b"status=failed reason=not-allowlisted cleanup=passed\n",
                ),
                "non-ascii": (
                    "non-ascii.log",
                    "RELEASE_BUILD_PHASE schema=1 phase=canonical-preflight "
                    "status=passed reason=ok cleanup=not-run é\n".encode("utf-8"),
                ),
                "out-of-order": (
                    "out-of-order.log",
                    b"RELEASE_BUILD_PHASE schema=1 phase=node-runtime status=passed reason=ok cleanup=not-run\n"
                    b"RELEASE_BUILD_PHASE schema=1 phase=canonical-preflight status=passed reason=ok cleanup=not-run\n",
                ),
            }
            for name, (filename, payload) in cases.items():
                with self.subTest(case=name):
                    path = root / filename
                    self._write_log(path, payload)
                    parsed = self._run_parser(path)
                    self.assertNotEqual(parsed.returncode, 0)
                    self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure())
                    self.assertEqual(parsed.stderr, "")

            oversized = root / "oversized.log"
            self._write_log(oversized, b"x" * (diagnostics.MAX_LOG_BYTES + 1))
            parsed = self._run_parser(oversized)
            self.assertNotEqual(parsed.returncode, 0)
            self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure())

            target = root / "target.log"
            self._write_log(target, valid)
            symlink = root / "symlink.log"
            symlink.symlink_to(target)
            parsed = self._run_parser(symlink)
            self.assertNotEqual(parsed.returncode, 0)
            self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure())

            hardlink_source = root / "hardlink-source.log"
            self._write_log(hardlink_source, valid)
            hardlink = root / "hardlink.log"
            os.link(hardlink_source, hardlink)
            parsed = self._run_parser(hardlink)
            self.assertNotEqual(parsed.returncode, 0)
            self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure())

            mode = root / "wrong-mode.log"
            self._write_log(mode, valid, mode=0o644)
            parsed = self._run_parser(mode)
            self.assertNotEqual(parsed.returncode, 0)
            self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure())

            missing = root / "missing.log"
            parsed = self._run_parser(missing)
            self.assertNotEqual(parsed.returncode, 0)
            self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure())

            parser_copy = root / "parser.py"
            shutil.copyfile(
                REPO_ROOT / "platform/tools/platform_release_build_diagnostics.py",
                parser_copy,
            )
            os.chmod(parser_copy, 0o755)
            os.chmod(root, 0o755)
            try:
                parsed = self._run_parser_as_nobody(parser_copy, target)
            finally:
                os.chmod(root, 0o700)
            self.assertNotEqual(parsed.returncode, 0)
            self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure())
            self.assertEqual(parsed.stderr, "")

    def test_builder_preserves_lock_failure_rc_and_emits_cleanup_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform = root / "platform"
            tools = platform / "tools"
            venv_bin = platform / ".venv_platform" / "bin"
            tools.mkdir(parents=True)
            venv_bin.mkdir(parents=True)
            shutil.copyfile(BUILD_SCRIPT, tools / BUILD_SCRIPT.name)
            os.chmod(tools / BUILD_SCRIPT.name, 0o755)
            bootstrap = tools / "platform_bootstrap.sh"
            bootstrap.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="ascii")
            os.chmod(bootstrap, 0o755)
            python = venv_bin / "python"
            python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="ascii")
            os.chmod(python, 0o755)

            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "platform"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Platform diagnostics",
                    "-c",
                    "user.email=platform-diagnostics@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "lock failure fixture",
                ],
                check=True,
            )
            output = root / "releases"
            output.mkdir(mode=0o755)
            descriptor = os.open(output, os.O_RDONLY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                completed = subprocess.run(
                    [str(tools / BUILD_SCRIPT.name), "lock-test"],
                    cwd=platform,
                    env={
                        **os.environ,
                        "PLATFORM_RELEASE_OUTPUT_DIR": str(output),
                    },
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            finally:
                os.close(descriptor)

        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertIn(
            "RELEASE_BUILD_PHASE schema=1 phase=cleanup status=passed reason=ok cleanup=passed",
            completed.stdout,
        )
        self.assertIn(
            "RELEASE_BUILD_PHASE schema=1 phase=complete status=failed "
            "reason=build_failed cleanup=passed failed_phase=canonical-preflight",
            completed.stdout,
        )
        self.assertNotIn(str(root), completed.stdout)
