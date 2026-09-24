from __future__ import annotations

import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from tools import platform_release_build_diagnostics as diagnostics
from tools import platform_release_phase_telemetry as telemetry


REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "platform/tools/platform_build_release.sh"


class PlatformReleaseBuildDiagnosticsTests(unittest.TestCase):
    @staticmethod
    def _write_log(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
        path.write_bytes(payload)
        os.chown(path, 0, 0)
        os.chmod(path, mode)

    @staticmethod
    def _run_parser(
        path: Path, *, option: str = "--marker-log"
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                str(REPO_ROOT / "platform/tools/platform_release_build_diagnostics.py"),
                option,
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
    def _success_markers(source_sha: str = "a" * 40) -> bytes:
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
                f"source_sha={source_sha} artifact_sha256={'b' * 64}",
            )
        )
        return ("\n".join(lines) + "\n").encode("ascii")

    @staticmethod
    def _failure_markers(
        failed_phase: str,
        *,
        include_failed_phase: bool = False,
        cleanup_failed: bool = False,
    ) -> bytes:
        failed_index = diagnostics.PHASE_INDEX[failed_phase]
        prefix_end = failed_index + 1 if include_failed_phase else failed_index
        lines = [
            f"RELEASE_BUILD_PHASE schema=1 phase={phase} status=passed reason=ok cleanup=not-run"
            for phase in diagnostics.PHASES[:prefix_end]
        ]
        if cleanup_failed:
            lines.append(
                "RELEASE_BUILD_PHASE schema=1 phase=cleanup status=failed "
                f"reason=cleanup_failed cleanup=failed failed_phase={failed_phase}"
            )
            complete_reason = "cleanup_failed"
            complete_cleanup = "failed"
        else:
            lines.append(
                "RELEASE_BUILD_PHASE schema=1 phase=cleanup status=passed "
                "reason=ok cleanup=passed"
            )
            complete_reason = "build_failed"
            complete_cleanup = "passed"
        lines.append(
            "RELEASE_BUILD_PHASE schema=1 phase=complete status=failed "
            f"reason={complete_reason} cleanup={complete_cleanup} failed_phase={failed_phase}"
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
            self._write_log(failure, self._failure_markers("canonical-preflight"))
            parsed_failure = self._run_parser(failure)
            self.assertEqual(parsed_failure.returncode, 0, parsed_failure.stderr)
            self.assertEqual(
                parsed_failure.stdout.strip(),
                "RELEASE_BUILD_DIAGNOSTIC schema=1 phase=complete status=failed "
                "reason=build_failed cleanup=passed failed_phase=canonical-preflight",
            )

            success_64 = root / "success-64.log"
            self._write_log(success_64, self._success_markers(source_sha="c" * 64))
            parsed_success_64 = self._run_parser(success_64)
            self.assertEqual(parsed_success_64.returncode, 0, parsed_success_64.stderr)
            self.assertIn(f"source_sha={'c' * 64}", parsed_success_64.stdout)

            for failed_phase in ("canonical-preflight", "web-build", "artifact-validate"):
                with self.subTest(failed_phase=failed_phase):
                    prefix = root / f"failure-{failed_phase}.log"
                    self._write_log(prefix, self._failure_markers(failed_phase))
                    parsed_prefix = self._run_parser(prefix)
                    self.assertEqual(parsed_prefix.returncode, 0, parsed_prefix.stderr)
                    self.assertIn(
                        f"failed_phase={failed_phase}", parsed_prefix.stdout
                    )

            cleanup_failure = root / "cleanup-failure.log"
            self._write_log(
                cleanup_failure,
                self._failure_markers(
                    "artifact-validate",
                    include_failed_phase=True,
                    cleanup_failed=True,
                ),
            )
            parsed_cleanup_failure = self._run_parser(cleanup_failure)
            self.assertEqual(
                parsed_cleanup_failure.returncode,
                0,
                parsed_cleanup_failure.stderr,
            )
            self.assertIn("reason=cleanup_failed", parsed_cleanup_failure.stdout)

            raw_log = root / "raw-build.log"
            self._write_log(raw_log, b"x" * (4 * 1024 * 1024 + 1))
            marker_stream = root / "marker-stream.log"
            self._write_log(marker_stream, self._success_markers())
            parsed_marker_stream = self._run_parser(marker_stream)
            self.assertEqual(parsed_marker_stream.returncode, 0, parsed_marker_stream.stderr)
            self.assertIn("phase=complete", parsed_marker_stream.stdout)

    def test_parser_rejects_incomplete_or_out_of_prefix_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = {
                "single-complete": (
                    "RELEASE_BUILD_PHASE schema=1 phase=complete status=passed "
                    "reason=ok cleanup=passed source_sha="
                    + "a" * 40
                    + " artifact_sha256="
                    + "b" * 64
                    + "\n"
                ).encode("ascii"),
                "missing-middle": b"\n".join(
                    line
                    for line in self._success_markers().splitlines()
                    if b"phase=web-dependencies" not in line
                )
                + b"\n",
            }
            out_of_prefix = self._failure_markers("web-build").decode("ascii").splitlines()
            out_of_prefix.insert(
                -2,
                "RELEASE_BUILD_PHASE schema=1 phase=web-build status=failed "
                "reason=build_failed cleanup=passed failed_phase=web-build",
            )
            cases["out-of-prefix-failure"] = (
                "\n".join(out_of_prefix) + "\n"
            ).encode("ascii")

            for name, payload in cases.items():
                with self.subTest(case=name):
                    path = root / f"{name}.log"
                    self._write_log(path, payload)
                    parsed = self._run_parser(path)
                    self.assertNotEqual(parsed.returncode, 0)
                    self.assertEqual(
                        parsed.stdout.strip(), diagnostics._safe_failure("sequence")
                    )
                    self.assertEqual(parsed.stderr, "")

    def test_parser_rejects_unsafe_metadata_and_marker_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = self._success_markers()
            cases: dict[str, tuple[str, bytes]] = {
                "control": ("control.log", valid + b"\x01\n"),
                "non-marker": (
                    "non-marker.log",
                    b"private raw builder output\n" + valid,
                ),
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
                "missing-failed-phase": (
                    "missing-failed-phase.log",
                    b"RELEASE_BUILD_PHASE schema=1 phase=complete status=failed "
                    b"reason=build_failed cleanup=passed\n",
                ),
                "missing-cleanup-failed-phase": (
                    "missing-cleanup-failed-phase.log",
                    b"RELEASE_BUILD_PHASE schema=1 phase=cleanup status=failed "
                    b"reason=cleanup_failed cleanup=failed\n",
                ),
                "non-ascii": (
                    "non-ascii.log",
                    "RELEASE_BUILD_PHASE schema=1 phase=canonical-preflight "
                    "status=passed reason=ok cleanup=not-run é\n".encode("utf-8"),
                ),
                "invalid-encoding": ("invalid-encoding.log", valid + b"\xff\n"),
                "out-of-order": (
                    "out-of-order.log",
                    b"RELEASE_BUILD_PHASE schema=1 phase=node-runtime status=passed reason=ok cleanup=not-run\n"
                    b"RELEASE_BUILD_PHASE schema=1 phase=canonical-preflight status=passed reason=ok cleanup=not-run\n",
                ),
                "short-success-source": (
                    "short-success-source.log",
                    (
                        "RELEASE_BUILD_PHASE schema=1 phase=complete status=passed "
                        "reason=ok cleanup=passed source_sha="
                        + "a" * 39
                        + " artifact_sha256="
                        + "b" * 64
                        + "\n"
                    ).encode("ascii"),
                ),
                "short-success-artifact": (
                    "short-success-artifact.log",
                    (
                        "RELEASE_BUILD_PHASE schema=1 phase=complete status=passed "
                        "reason=ok cleanup=passed source_sha="
                        + "a" * 40
                        + " artifact_sha256="
                        + "b" * 63
                        + "\n"
                    ).encode("ascii"),
                ),
            }
            for name, (filename, payload) in cases.items():
                with self.subTest(case=name):
                    path = root / filename
                    self._write_log(path, payload)
                    parsed = self._run_parser(path)
                    self.assertNotEqual(parsed.returncode, 0)
                    expected_reason = (
                        "control"
                        if name == "control"
                        else "encoding"
                        if name == "invalid-encoding"
                        else "sequence"
                        if name == "out-of-order"
                        else "marker"
                    )
                    self.assertEqual(
                        parsed.stdout.strip(), diagnostics._safe_failure(expected_reason)
                    )
                    self.assertEqual(parsed.stderr, "")

            for name, source_sha in (
                ("source-39", "a" * 39),
                ("source-41", "a" * 41),
                ("source-63", "a" * 63),
                ("source-65", "a" * 65),
                ("source-uppercase", "A" * 40),
            ):
                with self.subTest(case=name):
                    path = root / f"{name}.log"
                    self._write_log(path, self._success_markers(source_sha=source_sha))
                    parsed = self._run_parser(path)
                    self.assertNotEqual(parsed.returncode, 0)
                    self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure("marker"))
                    self.assertEqual(parsed.stderr, "")

            oversized = root / "oversized.log"
            self._write_log(oversized, b"x" * (diagnostics.MAX_MARKER_STREAM_BYTES + 1))
            parsed = self._run_parser(oversized)
            self.assertNotEqual(parsed.returncode, 0)
            self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure("oversized"))

            target = root / "target.log"
            self._write_log(target, valid)
            symlink = root / "symlink.log"
            symlink.symlink_to(target)
            parsed = self._run_parser(symlink)
            self.assertNotEqual(parsed.returncode, 0)
            self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure("metadata"))

            hardlink_source = root / "hardlink-source.log"
            self._write_log(hardlink_source, valid)
            hardlink = root / "hardlink.log"
            os.link(hardlink_source, hardlink)
            parsed = self._run_parser(hardlink)
            self.assertNotEqual(parsed.returncode, 0)
            self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure("metadata"))

            mode = root / "wrong-mode.log"
            self._write_log(mode, valid, mode=0o644)
            parsed = self._run_parser(mode)
            self.assertNotEqual(parsed.returncode, 0)
            self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure("metadata"))

            gid = root / "wrong-gid.log"
            self._write_log(gid, valid)
            os.chown(gid, 0, 1)
            parsed = self._run_parser(gid)
            self.assertNotEqual(parsed.returncode, 0)
            self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure("metadata"))

            missing = root / "missing.log"
            parsed = self._run_parser(missing)
            self.assertNotEqual(parsed.returncode, 0)
            self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure("missing"))

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
            self.assertEqual(parsed.stdout.strip(), diagnostics._safe_failure("metadata"))
            self.assertEqual(parsed.stderr, "")

            writer = REPO_ROOT / "platform/tools/platform_release_phase_telemetry.py"
            phase_root = root / "phase-root"
            phase_root.mkdir(mode=0o700)
            os.chmod(phase_root, 0o700)
            phase_log = phase_root / "phase.log"
            self._write_log(phase_log, b"")
            marker = self._success_markers().splitlines()[0].decode("ascii")

            def run_writer(*arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["/usr/bin/python3", "-I", str(writer), *arguments],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

            checked = run_writer("--check", "--log", str(phase_log))
            self.assertEqual(checked.returncode, 0, checked.stderr)
            appended = run_writer(
                "--append", "--log", str(phase_log), "--marker", marker
            )
            self.assertEqual(appended.returncode, 0, appended.stderr)
            self.assertEqual(phase_log.read_bytes(), f"{marker}\n".encode("ascii"))

            malformed = phase_root / "malformed.log"
            self._write_log(malformed, b"")
            rejected_marker = run_writer(
                "--append",
                "--log",
                str(malformed),
                "--marker",
                marker.replace("reason=ok", "reason=unexpected"),
            )
            self.assertNotEqual(rejected_marker.returncode, 0)
            rejected_control = run_writer(
                "--append",
                "--log",
                str(malformed),
                "--marker",
                f"{marker}\x01",
            )
            self.assertNotEqual(rejected_control.returncode, 0)

            oversized_stream = phase_root / "oversized-stream.log"
            self._write_log(oversized_stream, b"x" * diagnostics.MAX_MARKER_STREAM_BYTES)
            rejected_oversized = run_writer(
                "--append", "--log", str(oversized_stream), "--marker", marker
            )
            self.assertNotEqual(rejected_oversized.returncode, 0)

            symlink_stream = phase_root / "symlink-stream.log"
            symlink_stream.symlink_to(phase_log)
            self.assertNotEqual(
                run_writer("--check", "--log", str(symlink_stream)).returncode, 0
            )
            wrong_mode = phase_root / "wrong-mode.log"
            self._write_log(wrong_mode, b"", mode=0o644)
            self.assertNotEqual(
                run_writer("--check", "--log", str(wrong_mode)).returncode, 0
            )
            missing_writer_log = phase_root / "missing.log"
            self.assertNotEqual(
                run_writer("--check", "--log", str(missing_writer_log)).returncode, 0
            )

            before = phase_log.stat()
            changed = list(before)
            changed[6] += 1
            with mock.patch.object(
                diagnostics.os, "fstat", side_effect=[before, os.stat_result(changed)]
            ):
                with self.assertRaises(diagnostics.DiagnosticError) as raised:
                    diagnostics._read_marker_stream(phase_log)
            self.assertEqual(raised.exception.reject_reason, "metadata")

            writer_before = phase_log.stat()
            writer_changed = list(writer_before)
            writer_changed[6] += 1
            with mock.patch.object(
                telemetry.os,
                "fstat",
                side_effect=[writer_before, os.stat_result(writer_changed)],
            ):
                with self.assertRaises(telemetry.TelemetryError):
                    telemetry.append(phase_log, marker)

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
            phase_writer = tools / "platform_release_phase_telemetry.py"
            shutil.copyfile(
                REPO_ROOT / "platform/tools/platform_release_phase_telemetry.py",
                phase_writer,
            )
            os.chmod(phase_writer, 0o755)
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
            phase_root = root / "phase-root"
            phase_root.mkdir(mode=0o700)
            os.chmod(phase_root, 0o700)
            phase_log = phase_root / "phase.log"
            self._write_log(phase_log, b"")
            descriptor = os.open(output, os.O_RDONLY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                completed = subprocess.run(
                    [str(tools / BUILD_SCRIPT.name), "lock-test"],
                    cwd=platform,
                    env={
                        **os.environ,
                        "PLATFORM_RELEASE_OUTPUT_DIR": str(output),
                        "PLATFORM_RELEASE_PHASE_LOG": str(phase_log),
                    },
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            finally:
                os.close(descriptor)

            missing_phase_log = subprocess.run(
                [str(tools / BUILD_SCRIPT.name), "missing-telemetry"],
                cwd=platform,
                env={
                    **os.environ,
                    "PLATFORM_RELEASE_OUTPUT_DIR": str(output),
                    "PLATFORM_RELEASE_PHASE_LOG": str(phase_root / "missing.log"),
                },
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(missing_phase_log.returncode, 1, missing_phase_log.stderr)
            self.assertNotIn(str(root), missing_phase_log.stderr)

            collision_ref = "identity-test"
            current_timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            next_timestamp = time.strftime(
                "%Y%m%dT%H%M%SZ", time.gmtime(time.time() + 1)
            )
            for timestamp in {current_timestamp, next_timestamp}:
                (output / f"{collision_ref}-{timestamp}.tar.gz").write_bytes(
                    b"fixture"
                )
            collided = subprocess.run(
                [str(tools / BUILD_SCRIPT.name), collision_ref],
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
            self.assertEqual(collided.returncode, 1, collided.stderr)
            self.assertIn(
                "Release output already exists for slug: identity-test-",
                collided.stderr,
            )
            parsed_marker_stream = self._run_parser(phase_log)
            self.assertEqual(parsed_marker_stream.returncode, 0, parsed_marker_stream.stderr)
            self.assertIn("failed_phase=canonical-preflight", parsed_marker_stream.stdout)

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
